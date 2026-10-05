from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.domain import ComparisonState, ConflictState, SourceClass
from app.domain.source import utc_now
from app.services.normalization import normalize_field_value

MATERIAL_FIELDS = frozenset(
    {
        "registration_number",
        "promoter_or_developer",
        "proposed_completion_date",
        "revised_completion_date",
        "current_status",
        "lapsed_flag",
        "qpr_missing_flag",
        "building_count",
        "unit_count",
        "land_or_project_area",
    }
)


def _token(value: Any) -> str:
    if hasattr(value, "isoformat"):
        value = value.isoformat()
    if isinstance(value, (tuple, list, set, frozenset)):
        value = sorted(_token(item) for item in value)
    return json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class FactObservation:
    project_id: str
    field_key: str
    value: Any
    source_id: str
    source_class: SourceClass
    snapshot_id: str | None = None
    captured_at: datetime | None = None
    source_date: datetime | None = None
    extraction_confidence: float | None = None
    evidence_reference: str | None = None
    raw_value: str | None = None


@dataclass(frozen=True, slots=True)
class SourceConflictRecord:
    project_id: str
    field_key: str
    observations: tuple[FactObservation, ...]
    comparison_state: ComparisonState
    is_material: bool
    state: ConflictState
    deduplication_key: str
    detected_at: datetime = field(default_factory=utc_now)
    reason_code: str = "source_values_disagree"

    @property
    def conflict_id(self) -> str:
        return hashlib.sha256(f"conflict|{self.deduplication_key}".encode()).hexdigest()[:32]

    @property
    def display_precedence(self) -> FactObservation | None:
        order = {
            SourceClass.OFFICIAL_REGULATOR: 0,
            SourceClass.REVIEWED_CORRECTION: 1,
            SourceClass.OFFICIAL_DEVELOPER: 2,
            SourceClass.USER_AUTHORIZED: 3,
            SourceClass.MARKETING_BROCHURE: 4,
            SourceClass.FIXTURE: 5,
            SourceClass.UNKNOWN: 6,
        }
        return min(self.observations, key=lambda item: order[item.source_class], default=None)


@dataclass(frozen=True, slots=True)
class ClaimComparison:
    field_key: str
    state: ComparisonState
    official_value: Any
    claim_value: Any
    document_id: str | None
    document_version: str | None
    page: int | None
    evidence_text: str | None
    requires_review: bool
    neutral_description: str


def detect_source_conflicts(observations: Iterable[FactObservation]) -> tuple[SourceConflictRecord, ...]:
    grouped: dict[tuple[str, str], list[FactObservation]] = {}
    for observation in observations:
        if observation.value is None:
            continue
        grouped.setdefault((observation.project_id, observation.field_key), []).append(observation)
    conflicts: list[SourceConflictRecord] = []
    for (project_id, field_key), items in sorted(grouped.items()):
        by_value: dict[str, list[FactObservation]] = {}
        for item in items:
            by_value.setdefault(_token(item.value), []).append(item)
        if len(by_value) <= 1:
            continue
        ordered = tuple(sorted(items, key=lambda item: (item.source_class.value, item.source_id, item.snapshot_id or "")))
        source_classes = {item.source_class for item in ordered}
        if SourceClass.OFFICIAL_REGULATOR in source_classes and SourceClass.OFFICIAL_DEVELOPER in source_classes:
            reason = "official_vs_developer"
        elif SourceClass.OFFICIAL_REGULATOR in source_classes and (
            SourceClass.MARKETING_BROCHURE in source_classes or SourceClass.USER_AUTHORIZED in source_classes
        ):
            reason = "official_vs_marketing"
        else:
            reason = "source_values_disagree"
        payload = {
            "project": project_id,
            "field": field_key,
            "observations": [
                (item.source_id, item.snapshot_id, item.source_class.value, _token(item.value)) for item in ordered
            ],
        }
        dedup = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        conflicts.append(
            SourceConflictRecord(
                project_id=project_id,
                field_key=field_key,
                observations=ordered,
                comparison_state=ComparisonState.CONFLICTING,
                is_material=field_key in MATERIAL_FIELDS,
                state=ConflictState.OPEN,
                deduplication_key=dedup,
                reason_code=reason,
            )
        )
    return tuple(conflicts)


def compare_brochure_claims(
    official_facts: Mapping[str, Any],
    claims: Iterable[Any],
) -> tuple[ClaimComparison, ...]:
    claim_by_key: dict[str, Any] = {}
    for claim in claims:
        key = str(getattr(claim, "field_key", ""))
        if key and key not in claim_by_key:
            claim_by_key[key] = claim
    results: list[ClaimComparison] = []
    for key in sorted(set(official_facts) | set(claim_by_key)):
        official = official_facts.get(key)
        claim = claim_by_key.get(key)
        claim_value = getattr(claim, "normalized_value", None) if claim is not None else None
        confidence = getattr(claim, "extraction_confidence", None) if claim is not None else None
        if official is None and claim_value is None:
            state = ComparisonState.UNVERIFIED
        elif official is None:
            state = ComparisonState.MARKETING_ONLY
        elif claim_value is None:
            state = ComparisonState.OFFICIAL_ONLY
        elif confidence is not None and float(confidence) < 0.8:
            state = ComparisonState.NEEDS_REVIEW
        else:
            normalized_official = normalize_field_value(key, official).value
            normalized_claim = normalize_field_value(key, claim_value).value
            state = ComparisonState.MATCHED if _token(normalized_official) == _token(normalized_claim) else ComparisonState.CONFLICTING
        description = (
            "Marketing claim conflicts with the currently captured official record."
            if state == ComparisonState.CONFLICTING
            else "Marketing and official values were compared without changing the official record."
        )
        results.append(
            ClaimComparison(
                field_key=key,
                state=state,
                official_value=official,
                claim_value=claim_value,
                document_id=getattr(claim, "document_id", None) if claim is not None else None,
                document_version=getattr(claim, "document_version", None) if claim is not None else None,
                page=getattr(claim, "page", None) if claim is not None else None,
                evidence_text=getattr(claim, "evidence_text", None) if claim is not None else None,
                requires_review=state in {ComparisonState.CONFLICTING, ComparisonState.NEEDS_REVIEW},
                neutral_description=description,
            )
        )
    return tuple(results)


ConflictService = detect_source_conflicts


def persist_source_conflicts(
    session: Any,
    *,
    organization_id: str,
    conflicts: Iterable[SourceConflictRecord],
) -> tuple[Any, ...]:
    """Persist conflict pairs without deleting or overwriting either source value."""

    from sqlalchemy import select

    from app.models import Source, SourceConflict

    rows: list[Any] = []
    for conflict in conflicts:
        existing = session.scalar(
            select(SourceConflict).where(
                SourceConflict.organization_id == organization_id,
                SourceConflict.deduplication_key == conflict.deduplication_key,
            )
        )
        if existing:
            rows.append(existing)
            continue
        primary = conflict.display_precedence
        secondary = next(
            (
                item
                for item in conflict.observations
                if primary is not None and _token(item.value) != _token(primary.value)
            ),
            None,
        )
        if primary is None or secondary is None:
            continue
        source_a_id = primary.source_id if session.get(Source, primary.source_id) else None
        source_b_id = secondary.source_id if session.get(Source, secondary.source_id) else None
        row = SourceConflict(
            organization_id=organization_id,
            project_id=conflict.project_id,
            field_key=conflict.field_key,
            source_a_id=source_a_id,
            source_b_id=source_b_id,
            source_a_class=primary.source_class.value,
            source_b_class=secondary.source_class.value,
            source_a_value=str(primary.value),
            source_b_value=str(secondary.value),
            source_a_reference={
                "source_id": primary.source_id,
                "snapshot_id": primary.snapshot_id,
                "evidence_reference": primary.evidence_reference,
            },
            source_b_reference={
                "source_id": secondary.source_id,
                "snapshot_id": secondary.snapshot_id,
                "evidence_reference": secondary.evidence_reference,
            },
            source_a_freshness_at=primary.source_date or primary.captured_at,
            source_b_freshness_at=secondary.source_date or secondary.captured_at,
            comparison_state=conflict.comparison_state.value.lower(),
            is_material=conflict.is_material,
            state=conflict.state.value,
            detected_at=conflict.detected_at,
            deduplication_key=conflict.deduplication_key,
        )
        session.add(row)
        session.flush()
        rows.append(row)
    return tuple(rows)
