from __future__ import annotations

from calendar import month_name
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    Alert,
    AlertReview,
    AuditEvent,
    ChangeEvidence,
    DetectedChange,
    Developer,
    Document,
    DocumentVersion,
    EntityMergeProposal,
    Location,
    MonitoringRun,
    Project,
    ProjectAlias,
    Report,
    ReportItem,
    Signal,
    SnapshotField,
    Source,
    SourceConflict,
    SourceSnapshot,
    User,
    UserCorrection,
    WatchlistItem,
)

_CHANGE_TITLES = {
    "date_extended": "Completion date changed",
    "date_moved_earlier": "Completion date moved earlier",
    "value_removed": "Previously displayed value removed",
    "value_reverted": "Value returned to an earlier state",
    "status_changed": "Project status changed",
    "flag_added": "Official flag observed",
    "flag_removed": "Official flag cleared",
    "source_degraded": "Source monitoring degraded",
    "source_conflict": "Current sources conflict",
    "parser_version_incompatible": "Parser compatibility review required",
}
_BLOCKED_ERROR_CATEGORIES = {
    "access_restricted",
    "authentication_required",
    "captcha",
    "live_hrera_disabled",
    "policy_blocked",
}
_STALE_AFTER_DAYS = 30


def _date_from_value(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip()[:10])
        except ValueError:
            return None
    return None


def _display_value(value: Any) -> str:
    parsed = _date_from_value(value)
    if parsed is not None:
        return f"{parsed.day} {month_name[parsed.month]} {parsed.year}"
    if value is None or value == "":
        return "Not present"
    if isinstance(value, Decimal):
        return format(value, "f").rstrip("0").rstrip(".")
    return str(value)


def _display_timestamp(value: datetime | None) -> str:
    if value is None:
        return "Not recorded"
    aware = value if value.tzinfo else value.replace(tzinfo=UTC)
    utc_value = aware.astimezone(UTC)
    return f"{utc_value.day} {month_name[utc_value.month]} {utc_value.year}, {utc_value:%H:%M} UTC"


def _timestamp_iso(value: datetime | None) -> str:
    if value is None:
        return ""
    aware = value if value.tzinfo else value.replace(tzinfo=UTC)
    return aware.astimezone(UTC).isoformat()


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _parse_filter_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _monitoring_coverage(
    *,
    snapshot: SourceSnapshot | None,
    source: Source | None,
    run: MonitoringRun | None,
    now: datetime | None = None,
) -> tuple[str, str]:
    reference_now = now or datetime.now(UTC)
    blocked = bool(
        (source and source.circuit_breaker_state == "open")
        or (run and run.access_completeness == "blocked")
        or (run and run.error_category in _BLOCKED_ERROR_CATEGORIES)
    )
    if blocked:
        return (
            "blocked",
            "Automatic access is blocked or restricted. Last verified facts remain visible.",
        )
    degraded = bool(
        (source and source.health_status in {"degraded", "unavailable", "disabled"})
        or (
            run
            and (
                run.status not in {"success", "succeeded", "completed"}
                or run.access_completeness == "unavailable"
            )
        )
    )
    if degraded:
        return (
            "degraded",
            "The latest check was incomplete or unavailable. No no-change conclusion was created.",
        )
    if snapshot is None:
        return "unverified", "No verified source snapshot has been captured."
    if snapshot.access_completeness != "complete" or snapshot.validation_status != "valid":
        return (
            "partial",
            "Only part of the latest source was validated. Unverified fields remain explicit.",
        )
    captured = _aware(snapshot.fetched_at)
    if captured and (reference_now - captured.astimezone(UTC)).days > _STALE_AFTER_DAYS:
        return (
            "stale",
            f"The latest successful capture is older than {_STALE_AFTER_DAYS} days.",
        )
    return "current", "The latest permitted source capture is complete and current."


def _score_percent(value: Any, *, default: int = 0) -> int:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return default
    if score <= 1:
        score *= 100
    return max(0, min(100, round(score)))


def _confidence_band(score: int, *, confirmed: bool = False) -> str:
    if confirmed and score >= 99:
        return "Confirmed"
    if score >= 85:
        return "High confidence"
    if score >= 65:
        return "Probable"
    if score > 0:
        return "Needs review"
    return "Unverified"


def _change_magnitude(change: DetectedChange) -> str:
    if change.magnitude_days is not None:
        return f"{change.magnitude_days:+d} days"
    if change.magnitude is None:
        return "-"
    value = _display_value(change.magnitude)
    return f"{value} {change.magnitude_unit}".strip() if change.magnitude_unit else value


def _change_title(change: DetectedChange) -> str:
    if change.change_type in _CHANGE_TITLES:
        return _CHANGE_TITLES[change.change_type]
    return f"{change.field_key.replace('_', ' ').title()} changed"


def _change_description(change: DetectedChange) -> str:
    old_value = _display_value(change.old_canonical_value)
    new_value = _display_value(change.new_canonical_value)
    magnitude = _change_magnitude(change)
    suffix = f" ({magnitude})" if magnitude != "-" else ""
    if change.change_type == "value_removed":
        return (
            "A previously displayed value is no longer present. This may be a source "
            "change and requires review."
        )
    if change.change_type == "source_degraded":
        return (
            "Monitoring is incomplete. Last verified facts remain visible and this check "
            "is not reported as no change."
        )
    return (
        f"The captured {change.field_key.replace('_', ' ')} changed from "
        f"{old_value} to {new_value}{suffix}."
    )


def project_profile_view(
    db: Session,
    *,
    organization_id: str,
    project: Project,
) -> dict[str, Any]:
    developer = (
        db.scalar(
            select(Developer).where(
                Developer.id == project.developer_id,
                Developer.organization_id == organization_id,
            )
        )
        if project.developer_id
        else None
    )
    location = (
        db.scalar(
            select(Location).where(
                Location.id == project.location_id,
                Location.organization_id == organization_id,
            )
        )
        if project.location_id
        else None
    )
    parent = (
        db.scalar(
            select(Project).where(
                Project.id == project.parent_project_id,
                Project.organization_id == organization_id,
                Project.deleted_at.is_(None),
            )
        )
        if project.parent_project_id
        else None
    )
    snapshot_rows = list(
        db.execute(
            select(SourceSnapshot, Source)
            .join(
                Source,
                (Source.id == SourceSnapshot.source_id)
                & (Source.organization_id == organization_id),
            )
            .where(
                SourceSnapshot.organization_id == organization_id,
                SourceSnapshot.project_id == project.id,
            )
            .order_by(SourceSnapshot.fetched_at.desc())
        )
    )
    latest_snapshot, latest_source = snapshot_rows[0] if snapshot_rows else (None, None)
    registration_source = (
        db.scalar(
            select(Source).where(
                Source.id == project.registration_source_id,
                Source.organization_id == organization_id,
            )
        )
        if project.registration_source_id
        else None
    )
    primary_source = latest_source or registration_source
    latest_run = db.scalar(
        select(MonitoringRun)
        .where(
            MonitoringRun.organization_id == organization_id,
            MonitoringRun.project_id == project.id,
        )
        .order_by(MonitoringRun.created_at.desc())
    )
    coverage, monitoring_message = _monitoring_coverage(
        snapshot=latest_snapshot,
        source=primary_source,
        run=latest_run,
    )
    watchlist_item = db.scalar(
        select(WatchlistItem)
        .where(
            WatchlistItem.organization_id == organization_id,
            WatchlistItem.project_id == project.id,
            WatchlistItem.status == "active",
        )
        .order_by(WatchlistItem.created_at)
    )
    alias_count = int(
        db.scalar(
            select(func.count())
            .select_from(ProjectAlias)
            .where(
                ProjectAlias.organization_id == organization_id,
                ProjectAlias.project_id == project.id,
            )
        )
        or 0
    )
    open_merge_proposals = int(
        db.scalar(
            select(func.count())
            .select_from(EntityMergeProposal)
            .where(
                EntityMergeProposal.organization_id == organization_id,
                EntityMergeProposal.entity_type == "project",
                EntityMergeProposal.status == "pending",
                (
                    (EntityMergeProposal.source_entity_id == project.id)
                    | (EntityMergeProposal.target_entity_id == project.id)
                ),
            )
        )
        or 0
    )
    raw_facts = dict(latest_snapshot.structured_facts) if latest_snapshot else {}
    completion = (
        raw_facts.get("revised_completion_date")
        or raw_facts.get("proposed_completion_date")
        or project.revised_completion_date
        or project.proposed_completion_date
    )
    location_label = (
        ", ".join(value for value in (location.state, location.city) if value)
        if location
        else ""
    )
    facts = {
        **raw_facts,
        "project_name": raw_facts.get("project_name") or project.name,
        "registration_number": (
            raw_facts.get("registration_number") or project.registration_number
        ),
        "promoter_or_developer": (
            raw_facts.get("promoter_or_developer")
            or (developer.canonical_name if developer else None)
        ),
        "project_type": raw_facts.get("project_type") or project.project_type,
        "location": raw_facts.get("location") or location_label,
        "locality_or_sector": (
            raw_facts.get("locality_or_sector")
            or (location.locality_or_sector if location else None)
        ),
        "revised_completion_date": _display_value(completion),
        "current_status": raw_facts.get("current_status") or project.current_status,
        "phase": raw_facts.get("phase") or project.phase_name,
        "qpr_missing_flag": (
            "Observed" if project.qpr_missing_flag is True else "Not observed"
        ),
        "lapsed_flag": "Observed" if project.lapsed_flag is True else "Not observed",
        "source_last_updated": _display_timestamp(
            latest_snapshot.source_date if latest_snapshot else None
        ),
        "completion_note": "Latest captured official value",
    }
    latest_by_source: dict[str, tuple[SourceSnapshot, Source]] = {}
    for snapshot, source in snapshot_rows:
        latest_by_source.setdefault(source.id, (snapshot, source))
    sources = [
        {
            "id": source.id,
            "name": source.name,
            "freshness": _display_timestamp(snapshot.fetched_at),
            "source_date": _display_timestamp(snapshot.source_date),
            "state": (
                "verified"
                if snapshot.access_completeness == "complete"
                and snapshot.validation_status == "valid"
                else "partial"
            ),
        }
        for snapshot, source in latest_by_source.values()
    ]
    if registration_source and registration_source.id not in latest_by_source:
        sources.append(
            {
                "id": registration_source.id,
                "name": registration_source.name,
                "freshness": _display_timestamp(registration_source.last_successful_fetch_at),
                "source_date": "",
                "state": registration_source.health_status,
            }
        )
    project_view = {
        "id": project.id,
        "name": project.name,
        "registration_number": project.registration_number,
        "developer_name": developer.canonical_name if developer else None,
        "locality_or_sector": location.locality_or_sector if location else None,
        "city": location.city if location else None,
        "identity_status": project.identity_status.replace("_", " ").title(),
        "current_status": project.current_status,
        "phase_name": project.phase_name,
        "primary_source_name": primary_source.name
        if primary_source
        else "No primary source captured",
        "freshness": _display_timestamp(
            latest_snapshot.fetched_at if latest_snapshot else project.last_verified_at
        ),
        "source_date": _display_timestamp(
            latest_snapshot.source_date if latest_snapshot else None
        ),
        "monitoring_state": coverage,
        "monitoring_message": monitoring_message,
        "last_verified_at": _display_timestamp(project.last_verified_at),
        "watchlist_item_id": watchlist_item.id if watchlist_item else None,
        "refresh_idempotency_key": (
            f"manual-{watchlist_item.id}-{int(datetime.now(UTC).timestamp())}"
            if watchlist_item
            else None
        ),
        "fixture_key": (project.official_url or "").removeprefix("fixture://"),
        "parent_project_name": parent.name if parent else None,
        "alias_count": alias_count,
        "open_merge_proposals": open_merge_proposals,
    }
    return {
        "project": project_view,
        "facts": facts,
        "sources": sources,
        "snapshots": [row[0] for row in snapshot_rows],
    }


def project_phase_map_view(
    db: Session,
    *,
    organization_id: str,
    project: Project,
) -> dict[str, Any]:
    parent_id = project.parent_project_id or project.id
    parent = db.scalar(
        select(Project).where(
            Project.id == parent_id,
            Project.organization_id == organization_id,
            Project.deleted_at.is_(None),
        )
    )
    if parent is None:
        return {
            "project": {"id": project.id, "name": project.name},
            "parent": {},
            "phases": [],
            "proposals": [],
        }
    phase_rows = list(
        db.scalars(
            select(Project)
            .where(
                Project.organization_id == organization_id,
                Project.parent_project_id == parent.id,
                Project.deleted_at.is_(None),
            )
            .order_by(Project.name)
        )
    )
    family = {parent.id: parent, **{item.id: item for item in phase_rows}}
    proposals = list(
        db.scalars(
            select(EntityMergeProposal)
            .where(
                EntityMergeProposal.organization_id == organization_id,
                EntityMergeProposal.entity_type == "project",
                (
                    EntityMergeProposal.source_entity_id.in_(family)
                    | EntityMergeProposal.target_entity_id.in_(family)
                ),
            )
            .order_by(EntityMergeProposal.created_at.desc())
        )
    )
    source = (
        db.scalar(
            select(Source).where(
                Source.id == (project.registration_source_id or parent.registration_source_id),
                Source.organization_id == organization_id,
            )
        )
        if project.registration_source_id or parent.registration_source_id
        else None
    )
    latest_verified = max(
        (
            _aware(item.last_verified_at)
            for item in family.values()
            if item.last_verified_at
        ),
        default=None,
    )
    phase_views = [
        {
            "id": phase.id,
            "name": phase.name,
            "registration_number": phase.registration_number,
            "identity_status": phase.identity_status.replace("_", " ").title(),
            "relationship_basis": (
                "Explicit parent link; developer and locality match"
                if phase.developer_id == parent.developer_id
                and phase.location_id == parent.location_id
                else "Explicit reviewed parent link"
            ),
        }
        for phase in phase_rows
    ]
    proposal_views = []
    for proposal in proposals:
        source_project = family.get(proposal.source_entity_id)
        target_project = family.get(proposal.target_entity_id)
        proposal_views.append(
            {
                "candidate_name": (
                    f"{source_project.name if source_project else proposal.source_entity_id} -> "
                    f"{target_project.name if target_project else proposal.target_entity_id}"
                ),
                "reason": ", ".join(proposal.reason_codes) or proposal.note,
                "confidence": (
                    f"{round(proposal.confidence * 100)}%"
                    if proposal.confidence is not None
                    else "Unrated"
                ),
                "state": proposal.status.replace("_", " ").title(),
            }
        )
    return {
        "project": {
            "id": project.id,
            "name": project.name,
            "identity_status": project.identity_status.replace("_", " ").title(),
        },
        "parent": {
            "id": parent.id,
            "name": parent.name,
            "registration_number": parent.registration_number,
        },
        "phases": phase_views,
        "proposals": proposal_views,
        "map_source_name": source.name if source else "Canonical identity records",
        "map_freshness": _display_timestamp(latest_verified),
        "map_state": (
            "verified"
            if parent.identity_status == "confirmed"
            and all(phase.identity_status == "confirmed" for phase in phase_rows)
            else "unverified"
        ),
    }


def project_change_ledger_view(
    db: Session,
    *,
    organization_id: str,
    project: Project,
    field_key: str | None = None,
    severity: str | None = None,
    review_status: str | None = None,
    change_type: str | None = None,
    source_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, Any]:
    base_query = (
        select(DetectedChange, Source)
        .join(Source, Source.id == DetectedChange.source_id)
        .where(
            DetectedChange.organization_id == organization_id,
            DetectedChange.project_id == project.id,
            Source.organization_id == organization_id,
        )
    )
    all_rows = list(db.execute(base_query.order_by(DetectedChange.detected_at.desc())))
    normalized_field = (field_key or "").strip()
    normalized_severity = (severity or "").strip().lower()
    normalized_review = (review_status or "").strip().lower()
    normalized_type = (change_type or "").strip().lower()
    normalized_source = (source_id or "").strip()
    parsed_from = _parse_filter_date(date_from)
    parsed_to = _parse_filter_date(date_to)
    query = base_query
    if normalized_field:
        query = query.where(DetectedChange.field_key == normalized_field)
    if normalized_severity:
        query = query.where(DetectedChange.severity == normalized_severity)
    if normalized_review:
        query = query.where(DetectedChange.review_state == normalized_review)
    if normalized_type:
        query = query.where(DetectedChange.change_type == normalized_type)
    if normalized_source:
        query = query.where(DetectedChange.source_id == normalized_source)
    if parsed_from:
        query = query.where(
            DetectedChange.detected_at
            >= datetime.combine(parsed_from, time.min, tzinfo=UTC)
        )
    if parsed_to:
        query = query.where(
            DetectedChange.detected_at
            < datetime.combine(parsed_to + timedelta(days=1), time.min, tzinfo=UTC)
        )
    rows = list(db.execute(query.order_by(DetectedChange.detected_at.desc())))
    changes: list[dict[str, Any]] = []
    for change, source in rows:
        score = _score_percent(change.change_confidence, default=0)
        changes.append(
            {
                "id": change.id,
                "evidence_id": change.id,
                "field_key": change.field_key,
                "field_label": change.field_key.replace("_", " ").title(),
                "old_value": _display_value(change.old_canonical_value),
                "new_value": _display_value(change.new_canonical_value),
                "old_snapshot_id": change.old_snapshot_id,
                "new_snapshot_id": change.new_snapshot_id,
                "magnitude": _change_magnitude(change),
                "change_type": change.change_type,
                "source_name": source.name,
                "source_class": source.source_class,
                "detected_at": _display_timestamp(change.detected_at),
                "detected_at_iso": _timestamp_iso(change.detected_at),
                "confidence": _confidence_band(score, confirmed=score >= 99),
                "confidence_value": score,
                "severity": change.severity.title(),
                "review_state": change.review_state.replace("_", " ").title(),
                "requires_review": change.requires_review,
            }
        )
    latest = rows[0] if rows else None
    available_fields = sorted({item[0].field_key for item in all_rows})
    available_change_types = sorted({item[0].change_type for item in all_rows})
    available_sources = [
        {"id": source.id, "name": source.name}
        for source in {
            source.id: source for _, source in all_rows
        }.values()
    ]
    available_sources.sort(key=lambda item: item["name"])
    return {
        "changes": changes,
        "ledger": changes,
        "available_fields": available_fields,
        "available_change_types": available_change_types,
        "available_sources": available_sources,
        "ledger_source_name": latest[1].name if latest else "Compared canonical snapshots",
        "ledger_freshness": (
            _display_timestamp(latest[0].detected_at) if latest else "No compatible comparison"
        ),
        "ledger_state": "verified" if changes else "unverified",
        "filters": {
            "field": normalized_field,
            "severity": normalized_severity,
            "review_status": normalized_review,
            "change_type": normalized_type,
            "source_id": normalized_source,
            "date_from": date_from or "",
            "date_to": date_to or "",
        },
    }


def change_evidence_card_view(
    db: Session,
    *,
    organization_id: str,
    change: DetectedChange,
) -> dict[str, Any]:
    new_snapshot = db.scalar(
        select(SourceSnapshot).where(
            SourceSnapshot.id == change.new_snapshot_id,
            SourceSnapshot.organization_id == organization_id,
        )
    )
    source = db.scalar(
        select(Source).where(
            Source.id == change.source_id,
            Source.organization_id == organization_id,
        )
    )
    evidence_rows = list(
        db.scalars(
            select(ChangeEvidence)
            .where(
                ChangeEvidence.organization_id == organization_id,
                ChangeEvidence.detected_change_id == change.id,
            )
            .order_by(ChangeEvidence.evidence_side)
        )
    )
    evidence_by_side = {row.evidence_side: row for row in evidence_rows}
    old_evidence = evidence_by_side.get("old")
    new_evidence = evidence_by_side.get("new")
    new_field = db.scalar(
        select(SnapshotField).where(
            SnapshotField.organization_id == organization_id,
            SnapshotField.snapshot_id == change.new_snapshot_id,
            SnapshotField.field_key == change.field_key,
        )
    )
    old_field = db.scalar(
        select(SnapshotField).where(
            SnapshotField.organization_id == organization_id,
            SnapshotField.snapshot_id == change.old_snapshot_id,
            SnapshotField.field_key == change.field_key,
        )
    )
    alert = db.scalar(
        select(Alert)
        .where(
            Alert.organization_id == organization_id,
            Alert.detected_change_id == change.id,
        )
        .order_by(Alert.first_seen_at.desc())
    )
    source_url = (new_evidence.source_url if new_evidence else None) or (
        new_snapshot.final_url if new_snapshot else None
    )
    parsed_url = urlsplit(source_url or "")
    source_url_permitted = parsed_url.scheme in {"http", "https"} and bool(parsed_url.hostname)
    extraction_score = _score_percent(
        change.extraction_confidence
        if change.extraction_confidence is not None
        else new_field.extraction_confidence
        if new_field
        else None
    )
    change_score = _score_percent(change.change_confidence)
    identity_score = _score_percent(change.identity_confidence)
    source_score = _score_percent(change.source_reliability)
    freshness_score = 100 if new_snapshot and new_snapshot.access_completeness == "complete" else 45
    locator = None
    if new_evidence:
        locator = (
            new_evidence.selector
            or (f"Page {new_evidence.page_number}" if new_evidence.page_number else None)
            or new_evidence.evidence_reference
        )
    if not locator and new_field:
        locator = new_field.selector or (
            f"Page {new_field.page_number}" if new_field.page_number else None
        )
    card = {
        "id": change.id,
        "title": _change_title(change),
        "neutral_description": _change_description(change),
        "old_value": _display_value(change.old_canonical_value),
        "new_value": _display_value(change.new_canonical_value),
        "magnitude": _change_magnitude(change),
        "old_snapshot_id": change.old_snapshot_id,
        "new_snapshot_id": change.new_snapshot_id,
        "old_excerpt": (
            old_evidence.excerpt
            if old_evidence and old_evidence.excerpt
            else old_field.raw_value
            if old_field
            else None
        ),
        "new_excerpt": (
            new_evidence.excerpt
            if new_evidence and new_evidence.excerpt
            else new_field.raw_value
            if new_field
            else None
        ),
        "excerpt": (
            new_evidence.excerpt
            if new_evidence and new_evidence.excerpt
            else new_field.raw_value
            if new_field
            else None
        ),
        "source_name": source.name if source else "Source not recorded",
        "source_class": source.source_class if source else "unknown",
        "source_url": source_url if source_url_permitted else None,
        "source_url_permitted": source_url_permitted,
        "source_date": _display_timestamp(new_snapshot.source_date if new_snapshot else None),
        "captured_at": _display_timestamp(new_snapshot.fetched_at if new_snapshot else None),
        "freshness": _display_timestamp(new_snapshot.fetched_at if new_snapshot else None),
        "source_state": (
            "verified"
            if new_snapshot
            and new_snapshot.access_completeness == "complete"
            and new_snapshot.validation_status == "valid"
            else "partial"
            if new_snapshot
            else "unverified"
        ),
        "locator": locator or "Hash only",
        "selector": new_evidence.selector
        if new_evidence
        else new_field.selector
        if new_field
        else None,
        "page_reference": (
            f"Page {new_evidence.page_number}"
            if new_evidence and new_evidence.page_number
            else f"Page {new_field.page_number}"
            if new_field and new_field.page_number
            else None
        ),
        "content_hash": (
            new_evidence.content_hash
            if new_evidence
            else new_snapshot.content_hash
            if new_snapshot
            else "Not retained"
        ),
        "extraction_method": (
            new_evidence.extraction_method
            if new_evidence and new_evidence.extraction_method
            else new_field.extraction_method
            if new_field
            else "Not recorded"
        ),
        "parser_name": new_snapshot.parser_name if new_snapshot else "Not recorded",
        "parser_version": new_snapshot.parser_version if new_snapshot else "",
        "limitation": (
            alert.limitation
            if alert and alert.limitation
            else (
                "This card reflects captured source material and does not establish "
                "a legal conclusion."
            )
        ),
        "suggested_review_action": (
            alert.suggested_review_action
            if alert and alert.suggested_review_action
            else (
                "Compare both snapshots and confirm whether the captured source supports "
                "this change."
            )
        ),
        "reviewer_state": change.review_state.replace("_", " ").title(),
        "tone": "warning" if change.requires_review else "verified",
    }
    confidence = {
        "source_reliability_band": _confidence_band(source_score, confirmed=source_score >= 99),
        "source_reliability": source_score,
        "identity_band": _confidence_band(identity_score, confirmed=identity_score >= 99),
        "identity": identity_score,
        "extraction_band": _confidence_band(extraction_score, confirmed=extraction_score >= 99),
        "extraction": extraction_score,
        "change_band": _confidence_band(change_score, confirmed=change_score >= 99),
        "change": change_score,
        "freshness_band": _confidence_band(freshness_score),
        "freshness": freshness_score,
    }
    return {
        "change": {
            "id": change.id,
            "field_key": change.field_key,
            "old_value": card["old_value"],
            "new_value": card["new_value"],
            "old_snapshot_id": change.old_snapshot_id,
            "new_snapshot_id": change.new_snapshot_id,
            "magnitude": card["magnitude"],
        },
        "evidence": card,
        "confidence": confidence,
        "evidence_id": change.id,
    }


def project_timeline_view(
    db: Session,
    *,
    organization_id: str,
    project: Project,
    event_type: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, Any]:
    events: list[dict[str, Any]] = [
        {
            "id": f"project:{project.id}",
            "category": "identity",
            "event_type": "Project identity",
            "title": "Project tracking started",
            "description": (
                f"Identity captured as {project.registration_number}."
                if project.registration_number
                else "Provisional identity captured; confirmation is required."
            ),
            "timestamp": _display_timestamp(project.tracking_started_at),
            "timestamp_iso": _timestamp_iso(project.tracking_started_at),
            "sort_at": project.tracking_started_at,
            "tone": "verified" if project.identity_status == "confirmed" else "warning",
            "detail_href": f"/projects/{project.id}#identity-evidence",
            "detail_label": "Identity evidence",
        }
    ]
    snapshots = list(
        db.scalars(
            select(SourceSnapshot)
            .where(
                SourceSnapshot.organization_id == organization_id,
                SourceSnapshot.project_id == project.id,
            )
            .order_by(SourceSnapshot.fetched_at)
        )
    )
    source_ids = {snapshot.source_id for snapshot in snapshots}
    sources = (
        {
            source.id: source
            for source in db.scalars(
                select(Source).where(
                    Source.organization_id == organization_id,
                    Source.id.in_(source_ids),
                )
            )
        }
        if source_ids
        else {}
    )
    for index, snapshot in enumerate(snapshots):
        source = sources.get(snapshot.source_id)
        complete = (
            snapshot.access_completeness == "complete" and snapshot.validation_status == "valid"
        )
        events.append(
            {
                "id": f"snapshot:{snapshot.id}",
                "category": "source",
                "event_type": "Baseline capture" if index == 0 else "Source check",
                "title": "Baseline captured" if index == 0 else "Source checked",
                "description": (
                    f"{source.name if source else 'Source'} evidence was captured successfully "
                    f"with parser {snapshot.parser_name} {snapshot.parser_version}."
                    if complete
                    else "Only part of the source was readable. Some fields could not be verified."
                ),
                "timestamp": _display_timestamp(snapshot.fetched_at),
                "timestamp_iso": _timestamp_iso(snapshot.fetched_at),
                "sort_at": snapshot.fetched_at,
                "tone": "verified" if complete else "warning",
                "detail_href": f"/projects/{project.id}#source-evidence",
                "detail_label": "Captured source evidence",
            }
        )
    changes = list(
        db.scalars(
            select(DetectedChange)
            .where(
                DetectedChange.organization_id == organization_id,
                DetectedChange.project_id == project.id,
            )
            .order_by(DetectedChange.detected_at)
        )
    )
    for change in changes:
        events.append(
            {
                "id": f"change:{change.id}",
                "category": "change",
                "event_type": change.change_type.replace("_", " ").title(),
                "title": _change_title(change),
                "description": _change_description(change),
                "timestamp": _display_timestamp(change.detected_at),
                "timestamp_iso": _timestamp_iso(change.detected_at),
                "sort_at": change.detected_at,
                "tone": "warning" if change.requires_review else "verified",
                "review_state": change.review_state.replace("_", " ").title(),
                "evidence_id": change.id,
            }
        )
    failed_runs = list(
        db.scalars(
            select(MonitoringRun).where(
                MonitoringRun.organization_id == organization_id,
                MonitoringRun.project_id == project.id,
                MonitoringRun.status.notin_(["success", "succeeded"]),
                MonitoringRun.completed_at.is_not(None),
            )
        )
    )
    for run in failed_runs:
        occurred_at = run.completed_at or run.created_at
        events.append(
            {
                "id": f"run:{run.id}",
                "category": "source",
                "event_type": "Source health",
                "title": "Source monitoring incomplete",
                "description": run.error_message
                or "The source check did not replace the last verified facts.",
                "timestamp": _display_timestamp(occurred_at),
                "timestamp_iso": _timestamp_iso(occurred_at),
                "sort_at": occurred_at,
                "tone": "warning",
                "detail_href": f"/projects/{project.id}#source-evidence",
                "detail_label": "Source monitoring context",
            }
        )

    alerts = list(
        db.scalars(
            select(Alert)
            .where(
                Alert.organization_id == organization_id,
                Alert.project_id == project.id,
            )
            .order_by(Alert.first_seen_at)
        )
    )
    conflicts = list(
        db.scalars(
            select(SourceConflict)
            .where(
                SourceConflict.organization_id == organization_id,
                SourceConflict.project_id == project.id,
            )
            .order_by(SourceConflict.detected_at)
        )
    )
    conflict_signal_ids = set(
        db.scalars(
            select(Signal.id).where(
                Signal.organization_id == organization_id,
                Signal.project_id == project.id,
                Signal.rule_code.in_(
                    [
                        "OFFICIAL_VS_MARKETING_CONFLICT",
                        "OFFICIAL_VS_DEVELOPER_CONFLICT",
                    ]
                ),
            )
        )
    )
    conflict_alert_ids = {
        alert.id
        for alert in alerts
        if not alert.detected_change_id
        and (
            "conflict" in f"{alert.title} {alert.neutral_description}".lower()
            or alert.signal_id in conflict_signal_ids
        )
        and conflicts
    }
    for alert in alerts:
        if alert.detected_change_id or alert.id in conflict_alert_ids:
            continue
        events.append(
            {
                "id": f"alert:{alert.id}",
                "category": "review",
                "event_type": "Review alert",
                "title": alert.title,
                "description": alert.neutral_description,
                "timestamp": _display_timestamp(alert.first_seen_at),
                "timestamp_iso": _timestamp_iso(alert.first_seen_at),
                "sort_at": alert.first_seen_at,
                "tone": "warning",
                "review_state": alert.state.replace("_", " ").title(),
                "detail_href": f"/alerts?selected={alert.id}",
                "detail_label": "Open review item",
            }
        )
    for conflict in conflicts:
        events.append(
            {
                "id": f"conflict:{conflict.id}",
                "category": "review",
                "event_type": "Source conflict",
                "title": f"{conflict.field_key.replace('_', ' ').title()} sources conflict",
                "description": (
                    f"Preserved {conflict.source_a_class.replace('_', ' ')} value "
                    f"{_display_value(conflict.source_a_value)} and "
                    f"{conflict.source_b_class.replace('_', ' ')} value "
                    f"{_display_value(conflict.source_b_value)} for review."
                ),
                "timestamp": _display_timestamp(conflict.detected_at),
                "timestamp_iso": _timestamp_iso(conflict.detected_at),
                "sort_at": conflict.detected_at,
                "tone": "warning",
                "review_state": conflict.state.replace("_", " ").title(),
                "detail_href": f"/conflicts?selected={conflict.id}",
                "detail_label": "Compare sources",
            }
        )

    review_rows = list(
        db.execute(
            select(AlertReview, Alert, User)
            .join(
                Alert,
                (Alert.id == AlertReview.alert_id)
                & (Alert.organization_id == organization_id),
            )
            .join(
                User,
                User.id == AlertReview.reviewer_user_id,
            )
            .where(
                AlertReview.organization_id == organization_id,
                Alert.project_id == project.id,
            )
            .order_by(AlertReview.reviewed_at)
        )
    )
    for review, alert, reviewer in review_rows:
        events.append(
            {
                "id": f"review:{review.id}",
                "category": "review",
                "event_type": "Review decision",
                "title": f"Review decision: {review.decision.replace('_', ' ').title()}",
                "description": (
                    f'{reviewer.display_name} reviewed "{alert.title}" with reason '
                    f"{review.reason_code.replace('_', ' ')}."
                ),
                "timestamp": _display_timestamp(review.reviewed_at),
                "timestamp_iso": _timestamp_iso(review.reviewed_at),
                "sort_at": review.reviewed_at,
                "tone": "verified"
                if review.decision in {"confirm", "mark_expected"}
                else "warning",
                "review_state": review.decision.replace("_", " ").title(),
                "detail_href": f"/alerts?selected={alert.id}",
                "detail_label": "Review record",
            }
        )

    document_rows = list(
        db.execute(
            select(Document, DocumentVersion)
            .join(
                DocumentVersion,
                (DocumentVersion.document_id == Document.id)
                & (DocumentVersion.organization_id == organization_id),
            )
            .where(
                Document.organization_id == organization_id,
                Document.project_id == project.id,
                Document.deleted_at.is_(None),
            )
            .order_by(DocumentVersion.captured_at)
        )
    )
    for document, version in document_rows:
        events.append(
            {
                "id": f"document:{version.id}",
                "category": "document",
                "event_type": "Document version",
                "title": "Authorized document captured",
                "description": (
                    f"{document.title} version {version.version_number} was stored with "
                    f"content hash {version.content_hash[:12]}...."
                ),
                "timestamp": _display_timestamp(version.captured_at),
                "timestamp_iso": _timestamp_iso(version.captured_at),
                "sort_at": version.captured_at,
                "tone": "verified",
                "detail_href": f"/projects/{project.id}/documents?document_id={document.id}",
                "detail_label": "Document evidence",
            }
        )

    corrections = list(
        db.scalars(
            select(UserCorrection)
            .where(
                UserCorrection.organization_id == organization_id,
                UserCorrection.project_id == project.id,
            )
            .order_by(UserCorrection.created_at)
        )
    )
    for correction in corrections:
        events.append(
            {
                "id": f"correction:{correction.id}",
                "category": "review",
                "event_type": "Correction",
                "title": f"Correction recorded for {correction.field_key.replace('_', ' ')}",
                "description": (
                    f"A {correction.status.replace('_', ' ')} correction was preserved "
                    "separately from captured source facts."
                ),
                "timestamp": _display_timestamp(correction.created_at),
                "timestamp_iso": _timestamp_iso(correction.created_at),
                "sort_at": correction.created_at,
                "tone": "warning" if correction.status == "pending" else "verified",
                "review_state": correction.status.replace("_", " ").title(),
                "detail_href": (
                    f"/alerts?selected={correction.related_alert_id}"
                    if correction.related_alert_id
                    else f"/conflicts/{correction.related_conflict_id}/evidence"
                    if correction.related_conflict_id
                    else f"/projects/{project.id}#source-evidence"
                ),
                "detail_label": (
                    "Related review item"
                    if correction.related_alert_id
                    else "Related conflict evidence"
                    if correction.related_conflict_id
                    else "Correction evidence context"
                ),
            }
        )

    report_rows = list(
        db.execute(
            select(ReportItem, Report)
            .join(
                Report,
                (Report.id == ReportItem.report_id)
                & (Report.organization_id == organization_id),
            )
            .where(
                ReportItem.organization_id == organization_id,
                ReportItem.project_id == project.id,
                Report.deleted_at.is_(None),
            )
            .order_by(ReportItem.included_at)
        )
    )
    for item, report in report_rows:
        events.append(
            {
                "id": f"report:{item.id}",
                "category": "report",
                "event_type": "Report inclusion",
                "title": "Included in report",
                "description": f"Included in {report.title} ({report.state}).",
                "timestamp": _display_timestamp(item.included_at),
                "timestamp_iso": _timestamp_iso(item.included_at),
                "sort_at": item.included_at,
                "tone": "verified" if report.state == "approved" else "warning",
                "detail_href": f"/reports/{report.id}",
                "detail_label": "Report record",
            }
        )

    signals = list(
        db.scalars(
            select(Signal)
            .where(
                Signal.organization_id == organization_id,
                Signal.project_id == project.id,
            )
            .order_by(Signal.detected_at)
        )
    )
    alert_signal_ids = {alert.signal_id for alert in alerts if alert.signal_id}
    change_ids = {change.id for change in changes}
    new_phase_signal = False
    for signal in signals:
        is_new_phase = signal.rule_code == "NEW_PHASE_OR_REGISTRATION"
        if not is_new_phase and (
            signal.id in alert_signal_ids
            or signal.primary_detected_change_id in change_ids
        ):
            continue
        if is_new_phase:
            new_phase_signal = True
        events.append(
            {
                "id": f"signal:{signal.id}",
                "category": "phase" if is_new_phase else "signal",
                "event_type": "New phase" if is_new_phase else "Signal",
                "title": (
                    "New phase or registration observed"
                    if is_new_phase
                    else signal.rule_code.replace("_", " ").title()
                ),
                "description": (
                    f"{project.name} was linked to a captured registration and parent "
                    "relationship in the monitored dataset."
                    if is_new_phase
                    else "A deterministic rule emitted this evidence-linked signal."
                ),
                "timestamp": _display_timestamp(signal.detected_at),
                "timestamp_iso": _timestamp_iso(signal.detected_at),
                "sort_at": signal.detected_at,
                "tone": "new" if is_new_phase else "warning",
                "review_state": signal.state.replace("_", " ").title(),
                "evidence_id": (
                    signal.primary_detected_change_id
                    if signal.primary_detected_change_id in change_ids
                    else None
                ),
                "detail_href": (
                    f"/projects/{project.id}/phase-map"
                    if is_new_phase
                    else f"/projects/{project.id}#source-evidence"
                ),
                "detail_label": (
                    "Registration evidence"
                    if is_new_phase
                    else "Signal evidence context"
                ),
            }
        )

    phases = list(
        db.scalars(
            select(Project)
            .where(
                Project.organization_id == organization_id,
                Project.parent_project_id == project.id,
                Project.deleted_at.is_(None),
            )
            .order_by(Project.tracking_started_at)
        )
    )
    for phase in phases:
        events.append(
            {
                "id": f"phase:{phase.id}",
                "category": "phase",
                "event_type": "New phase",
                "title": "New phase linked",
                "description": (
                    f"{phase.name} ({phase.registration_number or 'registration unverified'}) "
                    "was linked to this parent project."
                ),
                "timestamp": _display_timestamp(phase.tracking_started_at),
                "timestamp_iso": _timestamp_iso(phase.tracking_started_at),
                "sort_at": phase.tracking_started_at,
                "tone": "new",
                "detail_href": f"/projects/{phase.id}/phase-map",
                "detail_label": "Relationship map",
            }
        )
    if project.parent_project_id and not new_phase_signal:
        events.append(
            {
                "id": f"phase-link:{project.id}",
                "category": "phase",
                "event_type": "New phase",
                "title": "New phase linked",
                "description": (
                    f"{project.name} was linked to its parent project with registration "
                    f"{project.registration_number or 'unverified'}."
                ),
                "timestamp": _display_timestamp(project.tracking_started_at),
                "timestamp_iso": _timestamp_iso(project.tracking_started_at),
                "sort_at": project.tracking_started_at,
                "tone": "new",
                "detail_href": f"/projects/{project.id}/phase-map",
                "detail_label": "Relationship map",
            }
        )

    audit_events = list(
        db.scalars(
            select(AuditEvent)
            .where(
                AuditEvent.organization_id == organization_id,
                AuditEvent.resource_id == project.id,
            )
            .order_by(AuditEvent.created_at)
        )
    )
    for audit in audit_events:
        events.append(
            {
                "id": f"audit:{audit.id}",
                "category": "audit",
                "event_type": "Audit action",
                "title": (audit.action or "Project action").replace("_", " ").title(),
                "description": "An authenticated project-level action was recorded.",
                "timestamp": _display_timestamp(audit.created_at),
                "timestamp_iso": _timestamp_iso(audit.created_at),
                "sort_at": audit.created_at,
                "tone": "verified",
                "audit_id": audit.id,
            }
        )

    # Every chronology entry must lead to preserved evidence or an audited
    # record. Specific links above take precedence; this fallback also protects
    # future event categories from becoming dead ends in the rendered timeline.
    for event in events:
        if not any(event.get(key) for key in ("evidence_id", "detail_href", "audit_id")):
            event["detail_href"] = f"/projects/{project.id}#source-evidence"
            event["detail_label"] = "Project evidence context"

    normalized_type = (event_type or "").strip().lower()
    parsed_from = _parse_filter_date(date_from)
    parsed_to = _parse_filter_date(date_to)
    filtered_events = []
    for event in events:
        occurred_at = _aware(event["sort_at"])
        if normalized_type:
            matches_category = event["category"] == normalized_type
            # A new-phase registration is both a phase relationship and a
            # deterministic signal. Keep it visible under either UI filter.
            matches_signal_alias = (
                normalized_type == "signal"
                and str(event.get("id", "")).startswith("signal:")
            )
            if not (matches_category or matches_signal_alias):
                continue
        if parsed_from and occurred_at and occurred_at < datetime.combine(
            parsed_from, time.min, tzinfo=UTC
        ):
            continue
        if parsed_to and occurred_at and occurred_at >= datetime.combine(
            parsed_to + timedelta(days=1), time.min, tzinfo=UTC
        ):
            continue
        filtered_events.append(event)
    filtered_events.sort(
        key=lambda item: _aware(item["sort_at"])
        or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )
    for event in filtered_events:
        event.pop("sort_at", None)
    latest_snapshot = max(snapshots, key=lambda item: item.fetched_at) if snapshots else None
    latest_source = sources.get(latest_snapshot.source_id) if latest_snapshot else None
    return {
        "events": filtered_events,
        "timeline": filtered_events,
        "timeline_source_name": (
            latest_source.name if latest_source else "Snapshots, evidence, and audit events"
        ),
        "timeline_freshness": (
            filtered_events[0]["timestamp"]
            if filtered_events
            else "No recorded event"
        ),
        "timeline_state": (
            "verified"
            if latest_snapshot
            and latest_snapshot.access_completeness == "complete"
            and latest_snapshot.validation_status == "valid"
            else "unverified"
        ),
        "filters": {
            "event_type": normalized_type,
            "from": date_from or "",
            "to": date_to or "",
        },
    }


def project_truth_passport_view(
    db: Session,
    *,
    organization_id: str,
    project: Project,
) -> dict[str, Any]:
    snapshot_rows = list(
        db.execute(
            select(SourceSnapshot, Source)
            .join(Source, Source.id == SourceSnapshot.source_id)
            .where(
                SourceSnapshot.organization_id == organization_id,
                SourceSnapshot.project_id == project.id,
                Source.organization_id == organization_id,
            )
            .order_by(SourceSnapshot.fetched_at.desc())
        )
    )
    latest_snapshot, latest_source = snapshot_rows[0] if snapshot_rows else (None, None)
    official_source_count = len(
        {source.id for _, source in snapshot_rows if source.source_class == "official_regulator"}
    )
    marketing_source_count = len(
        {source.id for _, source in snapshot_rows if source.source_class != "official_regulator"}
    )
    open_alerts = int(
        db.scalar(
            select(func.count())
            .select_from(Alert)
            .where(
                Alert.organization_id == organization_id,
                Alert.project_id == project.id,
                Alert.state.in_(["open", "unresolved"]),
            )
        )
        or 0
    )
    unresolved_conflicts = int(
        db.scalar(
            select(func.count())
            .select_from(SourceConflict)
            .where(
                SourceConflict.organization_id == organization_id,
                SourceConflict.project_id == project.id,
                SourceConflict.state.in_(["open", "unresolved"]),
            )
        )
        or 0
    )
    latest_review_row = db.execute(
        select(AlertReview, User)
        .join(Alert, Alert.id == AlertReview.alert_id)
        .join(User, User.id == AlertReview.reviewer_user_id)
        .where(
            AlertReview.organization_id == organization_id,
            Alert.organization_id == organization_id,
            Alert.project_id == project.id,
        )
        .order_by(AlertReview.reviewed_at.desc())
        .limit(1)
    ).first()
    latest_run = db.scalar(
        select(MonitoringRun)
        .where(
            MonitoringRun.organization_id == organization_id,
            MonitoringRun.project_id == project.id,
        )
        .order_by(MonitoringRun.created_at.desc())
    )
    coverage, coverage_message = _monitoring_coverage(
        snapshot=latest_snapshot,
        source=latest_source,
        run=latest_run,
    )
    extraction_score = (
        int(
            round(
                100
                * float(
                    db.scalar(
                        select(func.min(SnapshotField.extraction_confidence)).where(
                            SnapshotField.organization_id == organization_id,
                            SnapshotField.snapshot_id == latest_snapshot.id,
                        )
                    )
                    or 0
                )
            )
        )
        if latest_snapshot
        else 0
    )
    identity_score = (
        100 if project.identity_status == "confirmed" else 45 if not project.is_provisional else 0
    )
    source_score = (
        100
        if latest_source and latest_source.source_class in {"official_regulator", "fixture"}
        else 75
        if latest_source
        else 0
    )
    access_score = (
        100
        if coverage == "current"
        else 50
        if coverage == "partial"
        else 40
        if coverage == "stale"
        else 20
        if coverage == "degraded"
        else 10
        if coverage == "blocked"
        else 0
    )
    freshness_score = 0
    if latest_snapshot:
        captured = latest_snapshot.fetched_at
        captured = captured if captured.tzinfo else captured.replace(tzinfo=UTC)
        age_days = max(0, (datetime.now(UTC) - captured.astimezone(UTC)).days)
        freshness_score = 100 if age_days <= 30 else 75 if age_days <= 90 else 45
    change_score = 50 if open_alerts else 90 if latest_snapshot else 0
    parent = None
    if project.parent_project_id:
        parent = db.scalar(
            select(Project).where(
                Project.id == project.parent_project_id,
                Project.organization_id == organization_id,
                Project.deleted_at.is_(None),
            )
        )
    completion = project.revised_completion_date or project.proposed_completion_date
    latest_review = None
    if latest_review_row:
        review, reviewer = latest_review_row
        latest_review = {
            "decision": review.decision.replace("_", " ").title(),
            "reviewer_name": reviewer.display_name,
            "decided_at": _display_timestamp(review.reviewed_at),
            "note": review.note,
        }
    passport = {
        "identity_status": project.identity_status.replace("_", " ").title(),
        "official_source_count": official_source_count,
        "marketing_source_count": marketing_source_count,
        "primary_source_name": latest_source.name
        if latest_source
        else "Official source not captured",
        "freshness": _display_timestamp(latest_snapshot.fetched_at if latest_snapshot else None),
        "source_date": _display_timestamp(latest_snapshot.source_date if latest_snapshot else None),
        "monitoring_coverage": coverage,
        "monitoring_message": coverage_message,
        "last_successful_verification": _display_timestamp(project.last_verified_at),
        "open_alerts": open_alerts,
        "unresolved_conflicts": unresolved_conflicts,
        "history_start_date": _display_timestamp(project.tracking_started_at),
        "official_completion_date": _display_value(completion),
        "official_status": project.current_status or "Unverified",
        "parent_phase_label": (
            f"{parent.name} / {project.phase_name or project.name}"
            if parent
            else project.phase_name or "No parent phase recorded"
        ),
        "latest_reviewer_decision": latest_review,
    }
    confidence = {
        "source_reliability_band": _confidence_band(source_score, confirmed=source_score >= 99),
        "source_reliability": source_score,
        "access_completeness_band": (
            "Confirmed"
            if coverage == "current"
            else "Needs review"
            if coverage in {"partial", "stale", "degraded"}
            else "Unverified"
        ),
        "access_completeness": access_score,
        "identity_band": _confidence_band(identity_score, confirmed=identity_score >= 99),
        "identity": identity_score,
        "extraction_band": _confidence_band(extraction_score, confirmed=extraction_score >= 99),
        "extraction": extraction_score,
        "change_band": "Needs review" if open_alerts else _confidence_band(change_score),
        "change": change_score,
        "freshness_band": _confidence_band(freshness_score),
        "freshness": freshness_score,
        "conflict_state": "Conflicting sources"
        if unresolved_conflicts
        else "No unresolved conflict",
        "limitation": coverage_message,
    }
    return {"passport": passport, "confidence": confidence}
