from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from app.domain import (
    AccessCompleteness,
    ChangeType,
    ExtractedField,
    FactValue,
    FetchResult,
    ReviewState,
    Severity,
    ValidationStatus,
)
from app.domain.changes import DetectedChange
from app.services.normalization import parse_boolean, parse_date

DATE_FIELDS = frozenset(
    {
        "proposed_completion_date",
        "revised_completion_date",
        "completion_date",
        "possession_date",
        "registration_date",
        "source_last_updated_date",
    }
)
COMPLETION_DATE_FIELDS = frozenset(
    {"proposed_completion_date", "revised_completion_date", "completion_date", "possession_date"}
)
STATUS_FIELDS = frozenset({"current_status", "project_status", "status"})
IMPORTANT_FIELDS = frozenset(
    {
        "registration_number",
        "project_name",
        "promoter_or_developer",
        "proposed_completion_date",
        "revised_completion_date",
        "current_status",
        "lapsed_flag",
        "qpr_missing_flag",
        "phase",
    }
)
DOCUMENT_FIELDS = frozenset({"document_links", "official_notices", "notices"})
IGNORED_DIFF_FIELDS = frozenset({"source_last_updated_date"})


@dataclass(frozen=True, slots=True)
class ComparableSnapshot:
    snapshot_id: str
    project_id: str
    source_id: str | None
    parser_name: str
    parser_version: str
    schema_version: str
    access_completeness: AccessCompleteness
    validation_status: ValidationStatus
    facts: Mapping[str, FactValue]


def _value_token(value: Any) -> str:
    if isinstance(value, (date, datetime)):
        value = value.isoformat()
    elif isinstance(value, Decimal):
        value = format(value, "f")
    elif isinstance(value, Mapping):
        value = {str(key): _value_token(item) for key, item in value.items()}
    elif isinstance(value, (set, frozenset, tuple, list)):
        value = sorted(_value_token(item) for item in value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _read(obj: Any, *names: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        for name in names:
            if name in obj:
                return obj[name]
        return default
    for name in names:
        if hasattr(obj, name):
            return getattr(obj, name)
    return default


def _coerce_completeness(value: Any) -> AccessCompleteness:
    try:
        return value if isinstance(value, AccessCompleteness) else AccessCompleteness(str(value))
    except ValueError:
        return AccessCompleteness.UNAVAILABLE


def _coerce_validation(value: Any) -> ValidationStatus:
    try:
        return value if isinstance(value, ValidationStatus) else ValidationStatus(str(value))
    except ValueError:
        return ValidationStatus.NEEDS_REVIEW


def _fact_map(snapshot: Any) -> dict[str, FactValue]:
    content_hash = _read(snapshot, "content_hash")
    fields = _read(snapshot, "fields", "snapshot_fields", default=()) or ()
    result: dict[str, FactValue] = {}
    for field in fields:
        if isinstance(field, ExtractedField):
            fact = FactValue.from_extracted(field, content_hash)
        else:
            key = _read(field, "field_key", "canonical_field_key")
            if not key:
                continue
            fact = FactValue(
                field_key=str(key),
                canonical_value=_read(field, "canonical_value", "normalized_value"),
                raw_value=_read(field, "raw_value"),
                raw_label=_read(field, "raw_label"),
                extraction_confidence=float(_read(field, "extraction_confidence", default=1.0) or 0),
                extraction_method=str(_read(field, "extraction_method", default="deterministic")),
                unit=_read(field, "unit"),
            )
        current = result.get(fact.field_key)
        if current is None or fact.extraction_confidence > current.extraction_confidence:
            result[fact.field_key] = fact
    if result:
        return result
    structured = _read(snapshot, "structured_facts", "facts", default={}) or {}
    if callable(structured):
        structured = structured()
    if not isinstance(structured, Mapping):
        return result
    for key, value in structured.items():
        if isinstance(value, FactValue):
            result[str(key)] = value
        elif isinstance(value, Mapping) and ("canonical_value" in value or "normalized_value" in value):
            result[str(key)] = FactValue(
                field_key=str(key),
                canonical_value=value.get("canonical_value", value.get("normalized_value")),
                raw_value=value.get("raw_value"),
                raw_label=value.get("raw_label"),
                extraction_confidence=float(value.get("extraction_confidence", 1.0)),
                extraction_method=str(value.get("extraction_method", "deterministic")),
                unit=value.get("unit"),
            )
        else:
            result[str(key)] = FactValue(str(key), value, raw_value=None)
    return result


def comparable_snapshot(snapshot: Any) -> ComparableSnapshot:
    snapshot_id = _read(snapshot, "snapshot_id", "id")
    project_id = _read(snapshot, "project_id")
    if snapshot_id is None or project_id is None:
        raise ValueError("Snapshots must expose snapshot/id and project_id")
    return ComparableSnapshot(
        snapshot_id=str(snapshot_id),
        project_id=str(project_id),
        source_id=(str(value) if (value := _read(snapshot, "source_id")) is not None else None),
        parser_name=str(_read(snapshot, "parser_name", default="unknown")),
        parser_version=str(_read(snapshot, "parser_version", default="0")),
        schema_version=str(_read(snapshot, "schema_version", default="1.0")),
        access_completeness=_coerce_completeness(
            _read(snapshot, "access_completeness", "completeness", default=AccessCompleteness.COMPLETE)
        ),
        validation_status=_coerce_validation(
            _read(snapshot, "validation_status", default=ValidationStatus.VALID)
        ),
        facts=_fact_map(snapshot),
    )


def parser_versions_compatible(old: ComparableSnapshot, new: ComparableSnapshot) -> bool:
    if old.parser_name != new.parser_name:
        return False

    def major(version: str) -> str:
        match = re.match(r"^(\d+)", version)
        return match.group(1) if match else version

    return major(old.parser_version) == major(new.parser_version) and major(old.schema_version) == major(new.schema_version)


def _severity(field_key: str, change_type: ChangeType) -> Severity:
    if change_type in {
        ChangeType.PARSER_VERSION_INCOMPATIBLE,
        ChangeType.SOURCE_DEGRADED,
        ChangeType.SOURCE_CONFLICT,
    }:
        return Severity.HIGH
    if field_key in COMPLETION_DATE_FIELDS or field_key in STATUS_FIELDS:
        return Severity.HIGH
    if field_key.endswith("_flag") or change_type == ChangeType.VALUE_REMOVED:
        return Severity.HIGH
    if field_key in {"registration_number", "phase"} or field_key in DOCUMENT_FIELDS:
        return Severity.MEDIUM
    return Severity.MEDIUM


def _requires_review(field_key: str, change_type: ChangeType, old: FactValue | None, new: FactValue | None) -> bool:
    if change_type in {
        ChangeType.VALUE_REMOVED,
        ChangeType.DATE_EXTENDED,
        ChangeType.DATE_MOVED_EARLIER,
        ChangeType.STATUS_CHANGED,
        ChangeType.PARSER_VERSION_INCOMPATIBLE,
        ChangeType.SOURCE_CONFLICT,
        ChangeType.SOURCE_DEGRADED,
        ChangeType.VALUE_REVERTED,
    }:
        return True
    if field_key in COMPLETION_DATE_FIELDS or field_key in STATUS_FIELDS or field_key in {"official_notices"}:
        return True
    if field_key in {"lapsed_flag", "qpr_missing_flag"}:
        return True
    if change_type == ChangeType.VALUE_ADDED and field_key in {"registration_number", "phase"}:
        return True
    return any(
        fact is not None and (fact.extraction_method.startswith("ocr") or fact.extraction_confidence < 0.8)
        for fact in (old, new)
    )


def _confidence(old: FactValue | None, new: FactValue | None, *, compatible: bool = True) -> dict[str, Any]:
    values = [fact.extraction_confidence for fact in (old, new) if fact is not None]
    extraction = min(values, default=0.0)
    return {
        "source_reliability": "high",
        "source_access_completeness": "complete",
        "identity_confidence": "confirmed",
        "extraction_confidence": round(extraction, 4),
        "change_confidence": round(extraction if compatible else 0.0, 4),
        "freshness": "current",
        "conflict_state": "none",
    }


def _dedup(
    project_id: str,
    field_key: str,
    old_id: str,
    new_id: str,
    change_type: ChangeType,
    old_value: Any,
    new_value: Any,
) -> str:
    payload = "|".join(
        (
            project_id,
            field_key,
            old_id,
            new_id,
            change_type.value,
            _value_token(old_value),
            _value_token(new_value),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _make_change(
    old_snapshot: ComparableSnapshot,
    new_snapshot: ComparableSnapshot,
    field_key: str,
    old: FactValue | None,
    new: FactValue | None,
    change_type: ChangeType,
    *,
    magnitude: int | float | None = None,
    reason_codes: Sequence[str] = (),
    limitations: Sequence[str] = (),
) -> DetectedChange:
    old_value = old.canonical_value if old else None
    new_value = new.canonical_value if new else None
    requires_review = _requires_review(field_key, change_type, old, new)
    confidence = _confidence(
        old, new, compatible=change_type != ChangeType.PARSER_VERSION_INCOMPATIBLE
    )
    confidence["source_access_completeness"] = new_snapshot.access_completeness.value
    token = _dedup(
        old_snapshot.project_id,
        field_key,
        old_snapshot.snapshot_id,
        new_snapshot.snapshot_id,
        change_type,
        old_value,
        new_value,
    )
    return DetectedChange(
        project_id=old_snapshot.project_id,
        field_key=field_key,
        old_snapshot_id=old_snapshot.snapshot_id,
        new_snapshot_id=new_snapshot.snapshot_id,
        old_raw_value=old.raw_value if old else None,
        old_canonical_value=old_value,
        new_raw_value=new.raw_value if new else None,
        new_canonical_value=new_value,
        change_type=change_type,
        magnitude=magnitude,
        confidence_dimensions=confidence,
        severity=_severity(field_key, change_type),
        reason_codes=tuple(reason_codes) or (change_type.value,),
        requires_review=requires_review,
        deduplication_key=token,
        review_state=ReviewState.PENDING if requires_review else ReviewState.NOT_REQUIRED,
        change_id=token[:32],
        source_id=new_snapshot.source_id,
        limitations=tuple(limitations),
    )


def _sequence(value: Any) -> tuple[Any, ...]:
    if value is None:
        return ()
    if isinstance(value, (list, tuple, set, frozenset)):
        return tuple(value)
    return (value,)


class DeterministicDiffEngine:
    def compare(
        self,
        old_snapshot: Any,
        new_snapshot: Any,
        *,
        historical_snapshots: Iterable[Any] = (),
    ) -> tuple[DetectedChange, ...]:
        old = comparable_snapshot(old_snapshot)
        new = comparable_snapshot(new_snapshot)
        if old.project_id != new.project_id:
            raise ValueError("Cannot compare snapshots from different projects")
        changes: list[DetectedChange] = []
        if not parser_versions_compatible(old, new):
            changes.append(
                _make_change(
                    old,
                    new,
                    "__parser__",
                    FactValue("__parser__", f"{old.parser_name}@{old.parser_version}"),
                    FactValue("__parser__", f"{new.parser_name}@{new.parser_version}"),
                    ChangeType.PARSER_VERSION_INCOMPATIBLE,
                    reason_codes=("parser_name_or_major_version_changed",),
                    limitations=("Factual field comparison was suppressed pending parser review.",),
                )
            )
            return tuple(changes)
        degraded = (
            new.access_completeness != AccessCompleteness.COMPLETE
            or new.validation_status != ValidationStatus.VALID
        )
        if degraded:
            changes.append(
                _make_change(
                    old,
                    new,
                    "__source__",
                    FactValue("__source__", old.access_completeness.value),
                    FactValue("__source__", new.access_completeness.value),
                    ChangeType.SOURCE_DEGRADED,
                    reason_codes=(
                        f"access_{new.access_completeness.value}",
                        f"validation_{new.validation_status.value}",
                    ),
                    limitations=("Incomplete source access cannot establish that omitted facts were removed.",),
                )
            )
        if new.validation_status not in {ValidationStatus.VALID, ValidationStatus.PARTIAL}:
            return tuple(changes)
        history = tuple(comparable_snapshot(item) for item in historical_snapshots)
        completion_keys_to_skip: set[str] = set()

        def effective_completion(snapshot: ComparableSnapshot) -> tuple[str | None, FactValue | None]:
            for candidate_key in ("revised_completion_date", "proposed_completion_date", "completion_date", "possession_date"):
                candidate = snapshot.facts.get(candidate_key)
                if candidate is not None and candidate.canonical_value is not None:
                    return candidate_key, candidate
            return None, None

        old_completion_key, old_completion = effective_completion(old)
        new_completion_key, new_completion = effective_completion(new)
        if old_completion is not None or new_completion is not None:
            completion_keys_to_skip.update(COMPLETION_DATE_FIELDS)
            old_completion_value = old_completion.canonical_value if old_completion else None
            new_completion_value = new_completion.canonical_value if new_completion else None
            display_key = new_completion_key or old_completion_key or "completion_date"
            if old_completion is None and new_completion is not None:
                changes.append(
                    _make_change(old, new, display_key, None, new_completion, ChangeType.VALUE_ADDED)
                )
            elif old_completion is not None and new_completion is None:
                if not degraded:
                    changes.append(
                        _make_change(
                            old,
                            new,
                            display_key,
                            old_completion,
                            None,
                            ChangeType.VALUE_REMOVED,
                            reason_codes=("previous_completion_date_no_longer_present",),
                            limitations=("Removal may reflect a source presentation change and requires review.",),
                        )
                    )
            elif _value_token(old_completion_value) != _value_token(new_completion_value):
                old_date = parse_date(old_completion_value)
                new_date = parse_date(new_completion_value)
                magnitude = (new_date - old_date).days if old_date and new_date else None
                base_type = (
                    ChangeType.DATE_EXTENDED
                    if magnitude is not None and magnitude > 0
                    else ChangeType.DATE_MOVED_EARLIER
                    if magnitude is not None
                    else ChangeType.VALUE_CHANGED
                )
                historical_match = False
                for prior in history:
                    _, prior_fact = effective_completion(prior)
                    if prior_fact and _value_token(prior_fact.canonical_value) == _value_token(new_completion_value):
                        historical_match = True
                        break
                final_type = ChangeType.VALUE_REVERTED if historical_match else base_type
                reasons = [base_type.value]
                if old_completion_key != new_completion_key:
                    reasons.append("effective_completion_field_changed")
                if historical_match:
                    reasons.append("matches_earlier_historical_value")
                changes.append(
                    _make_change(
                        old,
                        new,
                        display_key,
                        old_completion,
                        new_completion,
                        final_type,
                        magnitude=magnitude,
                        reason_codes=tuple(reasons),
                    )
                )
        all_keys = sorted(
            (set(old.facts) | set(new.facts)) - completion_keys_to_skip - IGNORED_DIFF_FIELDS
        )
        for field_key in all_keys:
            old_fact = old.facts.get(field_key)
            new_fact = new.facts.get(field_key)
            old_value = old_fact.canonical_value if old_fact else None
            new_value = new_fact.canonical_value if new_fact else None
            if field_key in DOCUMENT_FIELDS:
                old_items = {_value_token(item): item for item in _sequence(old_value)}
                new_items = {_value_token(item): item for item in _sequence(new_value)}
                for token in sorted(new_items.keys() - old_items.keys()):
                    item = new_items[token]
                    changes.append(
                        _make_change(
                            old,
                            new,
                            field_key,
                            None,
                            FactValue(field_key, item, raw_value=str(item)),
                            ChangeType.DOCUMENT_LINK_ADDED,
                            reason_codes=("document_link_added",),
                        )
                    )
                if not degraded:
                    for token in sorted(old_items.keys() - new_items.keys()):
                        item = old_items[token]
                        changes.append(
                            _make_change(
                                old,
                                new,
                                field_key,
                                FactValue(field_key, item, raw_value=str(item)),
                                None,
                                ChangeType.DOCUMENT_LINK_REMOVED,
                                reason_codes=("document_link_removed",),
                            )
                        )
                continue
            if old_fact is None or old_value is None:
                if new_fact is not None and new_value is not None:
                    changes.append(
                        _make_change(old, new, field_key, old_fact, new_fact, ChangeType.VALUE_ADDED)
                    )
                continue
            if new_fact is None or new_value is None:
                if not degraded:
                    changes.append(
                        _make_change(
                            old,
                            new,
                            field_key,
                            old_fact,
                            new_fact,
                            ChangeType.VALUE_REMOVED,
                            reason_codes=("previous_value_no_longer_present",),
                            limitations=("Removal may reflect a source presentation change and requires review.",),
                        )
                    )
                continue
            if _value_token(old_value) == _value_token(new_value):
                continue
            change_type = ChangeType.VALUE_CHANGED
            magnitude: int | None = None
            reasons: list[str] = []
            if field_key in DATE_FIELDS:
                old_date = parse_date(old_value)
                new_date = parse_date(new_value)
                if old_date is not None and new_date is not None:
                    magnitude = (new_date - old_date).days
                    change_type = (
                        ChangeType.DATE_EXTENDED if magnitude > 0 else ChangeType.DATE_MOVED_EARLIER
                    )
                    reasons.append("date_extended_later" if magnitude > 0 else "date_moved_earlier")
            elif field_key in STATUS_FIELDS:
                change_type = ChangeType.STATUS_CHANGED
                reasons.append("status_value_changed")
            elif field_key.endswith("_flag"):
                old_flag = parse_boolean(old_value)
                new_flag = parse_boolean(new_value)
                if old_flag is not None and new_flag is not None:
                    change_type = ChangeType.FLAG_ADDED if new_flag else ChangeType.FLAG_REMOVED
                    reasons.append("flag_observed" if new_flag else "flag_cleared")
            reverted = any(
                field_key in prior.facts
                and _value_token(prior.facts[field_key].canonical_value) == _value_token(new_value)
                and prior.snapshot_id != old.snapshot_id
                for prior in history
            )
            if reverted:
                reasons.extend((change_type.value, "matches_earlier_historical_value"))
                change_type = ChangeType.VALUE_REVERTED
            changes.append(
                _make_change(
                    old,
                    new,
                    field_key,
                    old_fact,
                    new_fact,
                    change_type,
                    magnitude=magnitude,
                    reason_codes=tuple(reasons) or (change_type.value,),
                )
            )
        return tuple(changes)

    def source_degradation(
        self,
        previous_snapshot: Any,
        failed_fetch: FetchResult,
        *,
        monitoring_reference: str | None = None,
    ) -> DetectedChange:
        old = comparable_snapshot(previous_snapshot)
        new_reference = monitoring_reference or f"fetch:{failed_fetch.fetched_at.isoformat()}"
        pseudo = ComparableSnapshot(
            snapshot_id=new_reference,
            project_id=old.project_id,
            source_id=failed_fetch.source_id,
            parser_name=old.parser_name,
            parser_version=old.parser_version,
            schema_version=old.schema_version,
            access_completeness=failed_fetch.completeness,
            validation_status=ValidationStatus.NEEDS_REVIEW,
            facts={},
        )
        category = failed_fetch.error_category.value if failed_fetch.error_category else "source_unavailable"
        return _make_change(
            old,
            pseudo,
            "__source__",
            FactValue("__source__", old.access_completeness.value),
            FactValue("__source__", failed_fetch.access_status.value),
            ChangeType.SOURCE_DEGRADED,
            reason_codes=(category, "last_verified_facts_preserved"),
            limitations=("Monitoring incomplete; this is not evidence of no change.",),
        )

    def source_conflicts(self, observations: Iterable[Any]) -> tuple[DetectedChange, ...]:
        """Convert current cross-source disagreements into evidence-linkable changes."""

        from app.services.conflicts import detect_source_conflicts

        records = detect_source_conflicts(observations)
        changes: list[DetectedChange] = []
        for record in records:
            primary = record.display_precedence
            if primary is None:
                continue
            primary_token = _value_token(primary.value)
            for other in record.observations:
                if other is primary or _value_token(other.value) == primary_token:
                    continue
                old = ComparableSnapshot(
                    snapshot_id=primary.snapshot_id or f"source:{primary.source_id}",
                    project_id=record.project_id,
                    source_id=primary.source_id,
                    parser_name="cross_source",
                    parser_version="1.0.0",
                    schema_version="1.0",
                    access_completeness=AccessCompleteness.COMPLETE,
                    validation_status=ValidationStatus.VALID,
                    facts={
                        record.field_key: FactValue(
                            record.field_key,
                            primary.value,
                            raw_value=primary.raw_value,
                            extraction_confidence=primary.extraction_confidence or 0.0,
                        )
                    },
                )
                new = ComparableSnapshot(
                    snapshot_id=other.snapshot_id or f"source:{other.source_id}",
                    project_id=record.project_id,
                    source_id=other.source_id,
                    parser_name="cross_source",
                    parser_version="1.0.0",
                    schema_version="1.0",
                    access_completeness=AccessCompleteness.COMPLETE,
                    validation_status=ValidationStatus.VALID,
                    facts={
                        record.field_key: FactValue(
                            record.field_key,
                            other.value,
                            raw_value=other.raw_value,
                            extraction_confidence=other.extraction_confidence or 0.0,
                        )
                    },
                )
                change = _make_change(
                    old,
                    new,
                    record.field_key,
                    old.facts[record.field_key],
                    new.facts[record.field_key],
                    ChangeType.SOURCE_CONFLICT,
                    reason_codes=(record.reason_code, "both_source_values_preserved"),
                    limitations=("Source precedence is for display only; it does not resolve the conflict.",),
                )
                dimensions = dict(change.confidence_dimensions)
                dimensions["conflict_state"] = "conflicting_sources"
                dimensions["change_confidence"] = 1.0
                changes.append(replace(change, confidence_dimensions=dimensions))
        deduplicated = {change.deduplication_key: change for change in changes}
        return tuple(sorted(deduplicated.values(), key=lambda change: change.deduplication_key))


DiffEngine = DeterministicDiffEngine


def detect_changes(old_snapshot: Any, new_snapshot: Any, *, history: Iterable[Any] = ()) -> tuple[DetectedChange, ...]:
    return DeterministicDiffEngine().compare(
        old_snapshot,
        new_snapshot,
        historical_snapshots=history,
    )


def _stored_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (dict, list, tuple, set, frozenset)):
        return json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))
    return str(value)


def _numeric_confidence(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return max(0.0, min(1.0, float(value)))
    if value is None:
        return None
    return {
        "confirmed": 1.0,
        "high": 0.9,
        "high confidence": 0.9,
        "probable": 0.7,
        "needs review": 0.4,
        "unverified": 0.0,
    }.get(str(value).lower())


def persist_detected_changes(
    session: Any,
    *,
    organization_id: str,
    changes: Iterable[DetectedChange],
) -> tuple[Any, ...]:
    """Persist deterministic changes idempotently; caller owns the transaction."""

    from sqlalchemy import select

    from app.models import DetectedChange as DetectedChangeModel

    stored: list[Any] = []
    for change in changes:
        if change.change_type == ChangeType.SOURCE_DEGRADED and change.new_snapshot_id.startswith(("run:", "fetch:")):
            # Failed access has a monitoring-run reference, not a factual source
            # snapshot. Persist its transparent signal/run event instead of
            # violating the snapshot foreign key.
            continue
        existing = session.scalar(
            select(DetectedChangeModel).where(
                DetectedChangeModel.organization_id == organization_id,
                DetectedChangeModel.deduplication_key == change.deduplication_key,
            )
        )
        if existing:
            stored.append(existing)
            continue
        dimensions = dict(change.confidence_dimensions)
        is_day_change = change.field_key in DATE_FIELDS and isinstance(change.magnitude, int)
        row = DetectedChangeModel(
            id=change.change_id,
            organization_id=organization_id,
            project_id=change.project_id,
            source_id=change.source_id,
            old_snapshot_id=change.old_snapshot_id,
            new_snapshot_id=change.new_snapshot_id,
            field_key=change.field_key,
            old_raw_value=change.old_raw_value,
            old_canonical_value=_stored_text(change.old_canonical_value),
            new_raw_value=change.new_raw_value,
            new_canonical_value=_stored_text(change.new_canonical_value),
            change_type=change.change_type.value,
            magnitude=change.magnitude,
            magnitude_unit="days" if is_day_change else None,
            magnitude_days=int(change.magnitude) if is_day_change else None,
            source_reliability=_numeric_confidence(dimensions.get("source_reliability")),
            source_access_completeness=dimensions.get("source_access_completeness"),
            identity_confidence=_numeric_confidence(dimensions.get("identity_confidence")),
            extraction_confidence=_numeric_confidence(dimensions.get("extraction_confidence")),
            change_confidence=_numeric_confidence(dimensions.get("change_confidence")),
            confidence_dimensions=dimensions,
            severity=change.severity.value,
            reason_codes=list(change.reason_codes),
            requires_review=change.requires_review,
            review_state=change.review_state.value,
            parser_compatible=change.change_type != ChangeType.PARSER_VERSION_INCOMPATIBLE,
            detected_at=change.detected_at,
            deduplication_key=change.deduplication_key,
        )
        session.add(row)
        session.flush()
        stored.append(row)
    return tuple(stored)


__all__ = [
    "ComparableSnapshot",
    "DetectedChange",
    "DeterministicDiffEngine",
    "DiffEngine",
    "comparable_snapshot",
    "detect_changes",
    "parser_versions_compatible",
    "persist_detected_changes",
]
