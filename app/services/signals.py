from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any

from app.domain import ChangeType, ReviewRequirement, Severity
from app.domain.changes import DetectedChange
from app.domain.signals import Signal, SignalRuleCode

SIGNAL_RULE_VERSION = "2026.08.1"


def _read(obj: Any, name: str, default: Any = None) -> Any:
    return obj.get(name, default) if isinstance(obj, Mapping) else getattr(obj, name, default)


def _change_type(change: Any) -> ChangeType:
    value = _read(change, "change_type")
    return value if isinstance(value, ChangeType) else ChangeType(str(value))


def _change_id(change: Any) -> str:
    return str(_read(change, "change_id") or _read(change, "id") or _read(change, "deduplication_key"))


def _confidence(change: Any) -> tuple[float | None, dict[str, Any]]:
    dimensions = dict(_read(change, "confidence_dimensions", {}) or {})
    value = dimensions.get("change_confidence", _read(change, "change_confidence"))
    try:
        return (float(value), dimensions) if value is not None else (None, dimensions)
    except (TypeError, ValueError):
        return None, dimensions


def _signal_dedup(project_id: str, code: SignalRuleCode, change_ids: tuple[str, ...]) -> str:
    payload = json.dumps(
        {"project_id": project_id, "rule": code.value, "changes": sorted(change_ids)},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class DeterministicSignalEngine:
    def _build(
        self,
        code: SignalRuleCode,
        change: Any,
        *,
        supporting: tuple[Any, ...] | None = None,
        severity: Severity | None = None,
        review: ReviewRequirement | None = None,
        limitations: tuple[str, ...] = (),
    ) -> Signal:
        changes = supporting or (change,)
        change_ids = tuple(_change_id(item) for item in changes)
        project_id = str(_read(change, "project_id"))
        confidence, dimensions = _confidence(change)
        observed = {
            "field_key": _read(change, "field_key"),
            "old_value": _read(change, "old_canonical_value", _read(change, "old_value")),
            "new_value": _read(change, "new_canonical_value", _read(change, "new_value")),
            "magnitude": _read(change, "magnitude", _read(change, "magnitude_days")),
            "reason_codes": list(_read(change, "reason_codes", ()) or ()),
        }
        signal_severity = severity or Severity(str(_read(change, "severity", Severity.MEDIUM.value)))
        required = review or (
            ReviewRequirement.REVIEWER
            if bool(_read(change, "requires_review", False))
            else ReviewRequirement.NONE
        )
        return Signal(
            project_id=project_id,
            rule_code=code,
            rule_version=SIGNAL_RULE_VERSION,
            supporting_change_ids=change_ids,
            observed_facts=observed,
            severity=signal_severity,
            confidence=confidence,
            confidence_dimensions=dimensions,
            limitations=limitations or tuple(_read(change, "limitations", ()) or ()),
            required_review_level=required,
            deduplication_key=_signal_dedup(project_id, code, change_ids),
            primary_detected_change_id=change_ids[0] if change_ids else None,
        )

    def generate(
        self,
        changes: Iterable[DetectedChange | Any],
        *,
        historical_changes: Iterable[DetectedChange | Any] = (),
    ) -> tuple[Signal, ...]:
        current = tuple(changes)
        history = tuple(historical_changes)
        prior_extensions = Counter(
            str(_read(change, "project_id"))
            for change in history
            if _change_type(change) == ChangeType.DATE_EXTENDED
            and str(_read(change, "field_key"))
            in {"proposed_completion_date", "revised_completion_date", "completion_date", "possession_date"}
        )
        results: list[Signal] = []
        for change in current:
            change_type = _change_type(change)
            field = str(_read(change, "field_key"))
            reasons = {str(item) for item in (_read(change, "reason_codes", ()) or ())}
            project_id = str(_read(change, "project_id"))
            if change_type == ChangeType.DATE_EXTENDED and field in {
                "proposed_completion_date",
                "revised_completion_date",
                "completion_date",
                "possession_date",
            }:
                results.append(self._build(SignalRuleCode.COMPLETION_DATE_EXTENDED, change))
                if prior_extensions[project_id] >= 1:
                    results.append(
                        self._build(
                            SignalRuleCode.REPEATED_EXTENSION,
                            change,
                            severity=Severity.HIGH,
                            review=ReviewRequirement.REVIEWER,
                            limitations=("Repeated means at least one earlier captured extension in monitored history.",),
                        )
                    )
            elif change_type == ChangeType.DATE_MOVED_EARLIER and field in {
                "proposed_completion_date",
                "revised_completion_date",
                "completion_date",
                "possession_date",
            }:
                results.append(self._build(SignalRuleCode.COMPLETION_DATE_MOVED_EARLIER, change))
            if field == "lapsed_flag" and change_type in {ChangeType.FLAG_ADDED, ChangeType.VALUE_ADDED}:
                if _read(change, "new_canonical_value", _read(change, "new_value")) is True:
                    results.append(self._build(SignalRuleCode.PROJECT_MARKED_LAPSED, change))
            if field in {"current_status", "project_status", "status"} and change_type == ChangeType.STATUS_CHANGED:
                results.append(self._build(SignalRuleCode.PROJECT_STATUS_CHANGED, change))
            if field in {"qpr_missing_flag", "QPR_missing_flag"}:
                if change_type in {ChangeType.FLAG_ADDED, ChangeType.VALUE_ADDED}:
                    if _read(
                        change,
                        "new_canonical_value",
                        _read(change, "new_value"),
                    ) is True:
                        results.append(
                            self._build(SignalRuleCode.QPR_FLAG_OBSERVED, change)
                        )
                elif change_type in {ChangeType.FLAG_REMOVED, ChangeType.VALUE_REMOVED}:
                    results.append(self._build(SignalRuleCode.QPR_FLAG_CLEARED, change))
            if change_type == ChangeType.SOURCE_DEGRADED:
                results.append(
                    self._build(
                        SignalRuleCode.SOURCE_MONITORING_DEGRADED,
                        change,
                        severity=Severity.HIGH,
                        review=ReviewRequirement.REVIEWER,
                        limitations=("Monitoring is incomplete; last verified facts remain visible.",),
                    )
                )
                unavailable_markers = {
                    "source_unavailable",
                    "network",
                    "timeout",
                    "rate_limited",
                    "captcha",
                    "access_restricted",
                    "authentication_required",
                    "not_found",
                }
                if reasons & unavailable_markers:
                    results.append(
                        self._build(
                            SignalRuleCode.SOURCE_UNAVAILABLE,
                            change,
                            severity=Severity.HIGH,
                            review=ReviewRequirement.REVIEWER,
                        )
                    )
            if change_type == ChangeType.VALUE_REMOVED and field in {
                "registration_number",
                "project_name",
                "promoter_or_developer",
                "proposed_completion_date",
                "revised_completion_date",
                "current_status",
                "lapsed_flag",
                "qpr_missing_flag",
                "phase",
            }:
                results.append(self._build(SignalRuleCode.IMPORTANT_FIELD_REMOVED, change))
            if change_type == ChangeType.VALUE_ADDED and field in {"registration_number", "phase"}:
                results.append(self._build(SignalRuleCode.NEW_PHASE_OR_REGISTRATION, change))
            if change_type == ChangeType.DOCUMENT_LINK_ADDED and field in {"official_notices", "notices"}:
                results.append(self._build(SignalRuleCode.EXACT_LINKED_NOTICE_ADDED, change))
            if change_type == ChangeType.VALUE_REVERTED:
                results.append(self._build(SignalRuleCode.VALUE_REVERSED, change))
            if change_type == ChangeType.SOURCE_CONFLICT:
                code = (
                    SignalRuleCode.OFFICIAL_VS_DEVELOPER_CONFLICT
                    if "official_vs_developer" in reasons
                    else SignalRuleCode.OFFICIAL_VS_MARKETING_CONFLICT
                )
                results.append(self._build(code, change))
        deduplicated = {signal.deduplication_key: signal for signal in results}
        return tuple(sorted(deduplicated.values(), key=lambda item: (item.rule_code.value, item.deduplication_key)))


SignalEngine = DeterministicSignalEngine


def generate_signals(
    changes: Iterable[DetectedChange | Any],
    *,
    historical_changes: Iterable[DetectedChange | Any] = (),
) -> tuple[Signal, ...]:
    return DeterministicSignalEngine().generate(changes, historical_changes=historical_changes)


def persist_signals(
    session: Any,
    *,
    organization_id: str,
    signals: Iterable[Signal],
) -> tuple[Any, ...]:
    """Persist generated rule outputs idempotently without committing."""

    from sqlalchemy import select

    from app.models import DetectedChange as DetectedChangeModel
    from app.models import Signal as SignalModel

    rows: list[Any] = []
    for signal in signals:
        existing = session.scalar(
            select(SignalModel).where(
                SignalModel.organization_id == organization_id,
                SignalModel.deduplication_key == signal.deduplication_key,
            )
        )
        if existing:
            rows.append(existing)
            continue
        primary_change_id = signal.primary_detected_change_id
        if primary_change_id and session.get(DetectedChangeModel, primary_change_id) is None:
            primary_change_id = None
        row = SignalModel(
            id=signal.signal_id,
            organization_id=organization_id,
            project_id=signal.project_id,
            primary_detected_change_id=primary_change_id,
            rule_code=signal.rule_code.value,
            rule_version=signal.rule_version,
            supporting_change_ids=list(signal.supporting_change_ids),
            observed_facts=dict(signal.observed_facts),
            severity=signal.severity.value,
            confidence=signal.confidence,
            confidence_dimensions=dict(signal.confidence_dimensions),
            limitations=list(signal.limitations),
            required_review_level=signal.required_review_level.value,
            state="active",
            detected_at=signal.detected_at,
            deduplication_key=signal.deduplication_key,
        )
        session.add(row)
        session.flush()
        rows.append(row)
    return tuple(rows)
