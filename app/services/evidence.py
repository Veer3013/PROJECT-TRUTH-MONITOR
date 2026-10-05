from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from urllib.parse import urlsplit

from app.domain import ChangeType, ConfidenceBand, ReviewState, SourceClass
from app.domain.changes import DetectedChange
from app.services.diff import comparable_snapshot


class EvidenceLinkageError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class EvidenceSide:
    snapshot_id: str
    raw_value: str | None
    canonical_value: Any
    excerpt: str | None
    selector: str | None
    page: int | None
    coordinates: tuple[float, float, float, float] | None
    content_hash: str | None
    extraction_method: str | None


@dataclass(frozen=True, slots=True)
class EvidenceCard:
    evidence_id: str
    project_id: str
    change_id: str
    title: str
    neutral_description: str
    old_value: Any
    new_value: Any
    magnitude: str | None
    source_class: SourceClass
    source_url: str | None
    source_date: date | datetime | None
    captured_at: datetime | None
    old_snapshot_id: str
    new_snapshot_id: str
    old_evidence: EvidenceSide
    new_evidence: EvidenceSide
    source_reliability: Any
    identity_confidence: Any
    extraction_confidence: Any
    change_confidence: Any
    freshness: Any
    limitation: str | None
    suggested_review_action: str
    reviewer_state: ReviewState

    @property
    def evidence_excerpt(self) -> str | None:
        return self.new_evidence.excerpt or self.old_evidence.excerpt


_TITLES = {
    ChangeType.VALUE_ADDED: "Official value added",
    ChangeType.VALUE_CHANGED: "Official value changed",
    ChangeType.VALUE_REMOVED: "Previously displayed value removed",
    ChangeType.DATE_EXTENDED: "Completion date changed",
    ChangeType.DATE_MOVED_EARLIER: "Completion date moved earlier",
    ChangeType.STATUS_CHANGED: "Project status changed",
    ChangeType.FLAG_ADDED: "Official flag observed",
    ChangeType.FLAG_REMOVED: "Official flag cleared",
    ChangeType.DOCUMENT_LINK_ADDED: "Linked document added",
    ChangeType.DOCUMENT_LINK_REMOVED: "Linked document removed",
    ChangeType.VALUE_REVERTED: "Value returned to an earlier state",
    ChangeType.PARSER_VERSION_INCOMPATIBLE: "Parser compatibility review required",
    ChangeType.SOURCE_CONFLICT: "Current sources conflict",
    ChangeType.SOURCE_DEGRADED: "Source monitoring degraded",
}


def _read(obj: Any, name: str, default: Any = None) -> Any:
    return obj.get(name, default) if isinstance(obj, Mapping) else getattr(obj, name, default)


def _safe_excerpt(value: str | None) -> str | None:
    if not value:
        return None
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(value)).strip()
    return f"{text[:997]}..." if len(text) > 1_000 else text


def _display_url(value: str | None, permitted: bool) -> str | None:
    if not value or not permitted:
        return None
    parsed = urlsplit(value)
    # Uploaded evidence is exposed through an access-controlled route supplied by
    # the API, never by a local path or file:// URI.
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return None
    if re.match(r"^[A-Za-z]:[\\/]", value) or value.startswith(("/", "\\")):
        return None
    return value


def _change_type(change: Any) -> ChangeType:
    value = _read(change, "change_type")
    return value if isinstance(value, ChangeType) else ChangeType(str(value))


def _side(snapshot: Any, field_key: str, raw_value: str | None, canonical_value: Any) -> EvidenceSide:
    comparable = comparable_snapshot(snapshot)
    fact = comparable.facts.get(field_key)
    evidence = fact.evidence if fact else None
    return EvidenceSide(
        snapshot_id=comparable.snapshot_id,
        raw_value=raw_value if raw_value is not None else (fact.raw_value if fact else None),
        canonical_value=canonical_value,
        excerpt=_safe_excerpt(evidence.excerpt if evidence else (fact.raw_value if fact else raw_value)),
        selector=evidence.selector if evidence else None,
        page=evidence.page if evidence else None,
        coordinates=evidence.coordinates if evidence else None,
        content_hash=evidence.content_hash if evidence else _read(snapshot, "content_hash"),
        extraction_method=fact.extraction_method if fact else None,
    )


def _description(change: Any, change_type: ChangeType) -> str:
    field = str(_read(change, "field_key")).replace("_", " ")
    old = _read(change, "old_canonical_value", _read(change, "old_value"))
    new = _read(change, "new_canonical_value", _read(change, "new_value"))
    magnitude = _read(change, "magnitude", _read(change, "magnitude_days"))
    if change_type == ChangeType.DATE_EXTENDED:
        suffix = f" ({int(magnitude):+d} days)" if magnitude is not None else ""
        return f"The captured {field} changed from {old} to {new}{suffix}."
    if change_type == ChangeType.DATE_MOVED_EARLIER:
        suffix = f" ({int(magnitude):+d} days)" if magnitude is not None else ""
        return f"The captured {field} moved from {old} to {new}{suffix}."
    if change_type == ChangeType.VALUE_REMOVED:
        return "A previously displayed value is no longer present. This may be a source change and requires review."
    if change_type == ChangeType.SOURCE_DEGRADED:
        return "Monitoring is incomplete. Last verified facts remain visible and this check is not reported as no change."
    if change_type == ChangeType.SOURCE_CONFLICT:
        return "Current sources disagree. Review both records before using this value."
    if change_type == ChangeType.PARSER_VERSION_INCOMPATIBLE:
        return "Parser versions are not compatible, so factual field alerts were suppressed pending review."
    if change_type == ChangeType.VALUE_REVERTED:
        return f"The captured {field} returned from {old} to an earlier observed value, {new}."
    return f"The captured {field} changed from {old} to {new}."


class EvidenceBuilder:
    def build(
        self,
        change: DetectedChange | Any,
        *,
        old_snapshot: Any,
        new_snapshot: Any,
        source_class: SourceClass | str,
        source_url: str | None = None,
        source_url_permitted: bool = True,
        source_date: date | datetime | None = None,
        captured_at: datetime | None = None,
        source_reliability: Any = ConfidenceBand.HIGH.value,
        identity_confidence: Any = ConfidenceBand.CONFIRMED.value,
        freshness: Any = ConfidenceBand.HIGH.value,
        suggested_review_action: str | None = None,
        reviewer_state: ReviewState | str | None = None,
    ) -> EvidenceCard:
        old = comparable_snapshot(old_snapshot)
        new = comparable_snapshot(new_snapshot)
        old_change_id = str(_read(change, "old_snapshot_id"))
        new_change_id = str(_read(change, "new_snapshot_id"))
        if old.snapshot_id != old_change_id or new.snapshot_id != new_change_id:
            raise EvidenceLinkageError("Evidence snapshots must exactly match the detected change links.")
        field_key = str(_read(change, "field_key"))
        old_value = _read(change, "old_canonical_value", _read(change, "old_value"))
        new_value = _read(change, "new_canonical_value", _read(change, "new_value"))
        old_side = _side(old_snapshot, field_key, _read(change, "old_raw_value"), old_value)
        new_side = _side(new_snapshot, field_key, _read(change, "new_raw_value"), new_value)
        dimensions = dict(_read(change, "confidence_dimensions", {}) or {})
        change_type = _change_type(change)
        magnitude_value = _read(change, "magnitude", _read(change, "magnitude_days"))
        magnitude = None
        if magnitude_value is not None:
            magnitude = f"{int(magnitude_value):+d} days" if isinstance(magnitude_value, int) else str(magnitude_value)
        state_value = reviewer_state or _read(change, "review_state", ReviewState.PENDING.value)
        state = state_value if isinstance(state_value, ReviewState) else ReviewState(str(state_value))
        source_kind = source_class if isinstance(source_class, SourceClass) else SourceClass(str(source_class))
        change_id = str(_read(change, "change_id") or _read(change, "id") or _read(change, "deduplication_key"))
        evidence_id = hashlib.sha256(
            f"evidence|{change_id}|{old.snapshot_id}|{new.snapshot_id}".encode()
        ).hexdigest()[:32]
        limitation_values = tuple(_read(change, "limitations", ()) or ())
        limitation = " ".join(str(item) for item in limitation_values) or None
        title = _TITLES[change_type]
        if change_type in {ChangeType.DATE_EXTENDED, ChangeType.DATE_MOVED_EARLIER} and field_key not in {
            "proposed_completion_date",
            "revised_completion_date",
            "completion_date",
            "possession_date",
        }:
            title = "Date moved later" if change_type == ChangeType.DATE_EXTENDED else "Date moved earlier"
        return EvidenceCard(
            evidence_id=evidence_id,
            project_id=str(_read(change, "project_id")),
            change_id=change_id,
            title=title,
            neutral_description=_description(change, change_type),
            old_value=old_value,
            new_value=new_value,
            magnitude=magnitude,
            source_class=source_kind,
            source_url=_display_url(source_url, source_url_permitted),
            source_date=source_date or _read(new_snapshot, "source_date"),
            captured_at=captured_at or _read(new_snapshot, "fetched_at"),
            old_snapshot_id=old.snapshot_id,
            new_snapshot_id=new.snapshot_id,
            old_evidence=old_side,
            new_evidence=new_side,
            source_reliability=dimensions.get("source_reliability", source_reliability),
            identity_confidence=dimensions.get("identity_confidence", identity_confidence),
            extraction_confidence=dimensions.get("extraction_confidence"),
            change_confidence=dimensions.get("change_confidence"),
            freshness=dimensions.get("freshness", freshness),
            limitation=limitation,
            suggested_review_action=suggested_review_action
            or "Review both captured snapshots and confirm or keep the item unresolved.",
            reviewer_state=state,
        )


build_evidence_card = EvidenceBuilder().build


def persist_evidence_card(
    session: Any,
    *,
    organization_id: str,
    card: EvidenceCard,
) -> tuple[Any, ...]:
    """Persist both old/new evidence sides; caller owns the transaction."""

    from sqlalchemy import select

    from app.models import ChangeEvidence

    rows: list[Any] = []
    for side_name, side in (("old", card.old_evidence), ("new", card.new_evidence)):
        existing = session.scalar(
            select(ChangeEvidence).where(
                ChangeEvidence.organization_id == organization_id,
                ChangeEvidence.detected_change_id == card.change_id,
                ChangeEvidence.snapshot_id == side.snapshot_id,
                ChangeEvidence.evidence_side == side_name,
                ChangeEvidence.evidence_type == "field_provenance",
            )
        )
        if existing:
            rows.append(existing)
            continue
        coordinates = None
        if side.coordinates:
            coordinates = dict(zip(("x0", "y0", "x1", "y1"), side.coordinates, strict=True))
        row = ChangeEvidence(
            organization_id=organization_id,
            detected_change_id=card.change_id,
            snapshot_id=side.snapshot_id,
            evidence_side=side_name,
            evidence_type="field_provenance",
            source_class=card.source_class.value,
            source_url=card.source_url,
            evidence_reference=None,
            excerpt=side.excerpt,
            selector=side.selector,
            page_number=side.page,
            coordinates=coordinates,
            content_hash=side.content_hash or "unavailable",
            extraction_method=side.extraction_method,
            evidence_metadata={"evidence_card_id": card.evidence_id},
        )
        session.add(row)
        session.flush()
        rows.append(row)
    return tuple(rows)
