from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from urllib.parse import urlencode

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.models import (
    Alert,
    AuditEvent,
    DocumentVersion,
    OrganizationMembership,
    Project,
    Signal,
    Source,
    SourceConflict,
    User,
)


def _display_timestamp(value: datetime | None) -> str:
    if value is None:
        return "Not recorded"
    aware = value if value.tzinfo else value.replace(tzinfo=UTC)
    return aware.astimezone(UTC).strftime("%d %b %Y, %H:%M UTC")


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _alert_reason_condition(reason: str) -> Any:
    if reason == "material_change":
        return Alert.detected_change_id.is_not(None)
    if reason == "conflicting_sources":
        return or_(
            Alert.title.ilike("%conflict%"),
            Alert.neutral_description.ilike("%conflict%"),
        )
    if reason == "ocr_core_field":
        return or_(
            Alert.title.ilike("%OCR%"),
            Alert.neutral_description.ilike("%OCR%"),
            Alert.limitation.ilike("%OCR%"),
        )
    if reason == "identity":
        return or_(
            Alert.title.ilike("%identity%"),
            Alert.neutral_description.ilike("%identity%"),
            Alert.limitation.ilike("%identity%"),
        )
    return None


def _related_conflict(
    db: Session,
    *,
    organization_id: str,
    alert: Alert,
) -> SourceConflict | None:
    candidates = list(
        db.scalars(
            select(SourceConflict)
            .where(
                SourceConflict.organization_id == organization_id,
                SourceConflict.project_id == alert.project_id,
            )
            .order_by(SourceConflict.detected_at.desc())
        )
    )
    if not candidates:
        return None
    exact_dedup = next(
        (
            conflict
            for conflict in candidates
            if alert.deduplication_key == f"conflict:{conflict.deduplication_key}"
        ),
        None,
    )
    if exact_dedup:
        return exact_dedup

    signal = (
        db.scalar(
            select(Signal).where(
                Signal.id == alert.signal_id,
                Signal.organization_id == organization_id,
            )
        )
        if alert.signal_id
        else None
    )
    observed = signal.observed_facts if signal and isinstance(signal.observed_facts, dict) else {}
    linked_ids = {
        str(value)
        for value in observed.get("conflict_ids", [])
        if value is not None
    }
    if linked_ids:
        linked = next((item for item in candidates if item.id in linked_ids), None)
        if linked:
            return linked
    field_key = str(observed.get("field_key") or "").strip()
    if field_key:
        field_match = next((item for item in candidates if item.field_key == field_key), None)
        if field_match:
            return field_match

    for conflict in candidates:
        values_match = (
            alert.old_value == conflict.source_a_value
            and alert.new_value == conflict.source_b_value
        ) or (
            alert.old_value == conflict.source_b_value
            and alert.new_value == conflict.source_a_value
        )
        if values_match:
            return conflict
    alert_text = f"{alert.title} {alert.neutral_description}".casefold()
    field_match = next(
        (
            item
            for item in candidates
            if item.field_key.replace("_", " ").casefold() in alert_text
        ),
        None,
    )
    if field_match:
        return field_match
    return candidates[0] if "conflict" in alert_text and len(candidates) == 1 else None


def _conflict_reference_view(
    db: Session,
    *,
    organization_id: str,
    project_id: str,
    reference: dict[str, Any] | None,
) -> dict[str, Any]:
    preserved = dict(reference or {})
    version_id = preserved.get("document_version_id")
    version = (
        db.scalar(
            select(DocumentVersion).where(
                DocumentVersion.id == str(version_id),
                DocumentVersion.organization_id == organization_id,
            )
        )
        if version_id
        else None
    )
    document_id = preserved.get("document_id") or (version.document_id if version else None)
    page = preserved.get("page") or preserved.get("page_number")
    snapshot_id = preserved.get("snapshot_id")
    evidence_reference = preserved.get("evidence_reference")
    if version_id:
        kind = "Document version"
        identifier = str(version_id)
        href = (
            f"/projects/{project_id}/documents?document_id={document_id}"
            if document_id
            else f"/projects/{project_id}/documents"
        )
        link_label = "Open document evidence"
    elif snapshot_id:
        kind = "Source snapshot"
        identifier = str(snapshot_id)
        href = f"/projects/{project_id}#source-evidence"
        link_label = "Open captured source facts"
    else:
        kind = "Preserved source reference"
        identifier = str(evidence_reference or "Reference metadata retained")
        href = f"/projects/{project_id}#source-evidence"
        link_label = "Open source evidence context"
    return {
        "kind": kind,
        "identifier": identifier,
        "page": page,
        "content_hash": preserved.get("content_hash")
        or (version.content_hash if version else None),
        "url": preserved.get("url"),
        "evidence_reference": evidence_reference,
        "href": href,
        "link_label": link_label,
    }


def _alert_item(
    alert: Alert,
    project: Project,
    source: Source | None,
    conflict: SourceConflict | None = None,
) -> dict[str, Any]:
    evidence_href = (
        f"/evidence/{alert.detected_change_id}"
        if alert.detected_change_id
        else f"/conflicts/{conflict.id}/evidence"
        if conflict
        else None
    )
    return {
        "id": alert.id,
        "project_id": alert.project_id,
        "project_name": project.name,
        "registration_number": project.registration_number,
        "title": alert.title,
        "description": alert.neutral_description,
        "review_reason": (
            alert.limitation
            or alert.suggested_review_action
            or alert.required_review_level.replace("_", " ")
        ),
        "observed_at": _display_timestamp(alert.first_seen_at),
        "severity": alert.severity.title(),
        "review_state": alert.state.replace("_", " ").title(),
        "evidence_id": alert.detected_change_id,
        "evidence_href": evidence_href,
        "evidence_label": (
            "View change evidence"
            if alert.detected_change_id
            else "View conflict evidence"
            if conflict
            else ""
        ),
        "safe_detail_href": f"/projects/{alert.project_id}/timeline",
        "has_evidence": bool(evidence_href),
        "conflict_id": conflict.id if conflict else None,
        "source_class": alert.source_class,
        "source_name": source.name if source else "Source not linked",
        "freshness": alert.freshness or _display_timestamp(alert.first_seen_at),
        "old_value": alert.old_value,
        "new_value": alert.new_value,
        "magnitude": alert.magnitude,
    }


def alert_queue_view(
    db: Session,
    *,
    organization_id: str,
    selected_alert_id: str | None = None,
    q: str | None = None,
    state: str | None = None,
    severity: str | None = None,
    reason: str | None = None,
    project_id: str | None = None,
    evidence_id: str | None = None,
) -> dict[str, Any]:
    query = (
        select(Alert, Project, Source)
        .join(Project, Project.id == Alert.project_id)
        .outerjoin(
            Source,
            and_(
                Source.id == Alert.source_id,
                Source.organization_id == organization_id,
            ),
        )
        .where(
            Alert.organization_id == organization_id,
            Project.organization_id == organization_id,
            Project.deleted_at.is_(None),
        )
    )
    normalized_state = (state or "").strip().lower()
    if normalized_state and normalized_state != "all":
        query = query.where(Alert.state == normalized_state)
    elif normalized_state != "all":
        query = query.where(Alert.state.in_(["open", "unresolved"]))
    normalized_severity = (severity or "").strip().lower()
    if normalized_severity:
        query = query.where(Alert.severity == normalized_severity)
    normalized_query = (q or "").strip()
    if normalized_query:
        pattern = f"%{normalized_query}%"
        query = query.where(
            or_(
                Project.name.ilike(pattern),
                Project.registration_number.ilike(pattern),
                Alert.title.ilike(pattern),
                Alert.neutral_description.ilike(pattern),
                Source.name.ilike(pattern),
            )
        )
    normalized_reason = (reason or "").strip().lower()
    if normalized_reason and (condition := _alert_reason_condition(normalized_reason)) is not None:
        query = query.where(condition)
    if project_id:
        query = query.where(Alert.project_id == project_id)
    if evidence_id:
        query = query.where(Alert.detected_change_id == evidence_id)

    items: list[dict[str, Any]] = []
    selection_params = {
        key: value
        for key, value in {
            "q": normalized_query,
            "state": normalized_state,
            "severity": normalized_severity,
            "reason": normalized_reason,
            "project_id": project_id or "",
            "evidence_id": evidence_id or "",
        }.items()
        if value
    }
    for alert, project, source in db.execute(
        query.order_by(Alert.first_seen_at.desc())
    ):
        item = _alert_item(
            alert,
            project,
            source,
            _related_conflict(db, organization_id=organization_id, alert=alert),
        )
        item["selection_href"] = f"/alerts?{urlencode({**selection_params, 'selected': alert.id})}"
        items.append(item)
    selected_alert = next(
        (item for item in items if item["id"] == selected_alert_id), None
    )
    if selected_alert_id and selected_alert is None:
        selected_row = db.execute(
            select(Alert, Project, Source)
            .join(Project, Project.id == Alert.project_id)
            .outerjoin(
                Source,
                and_(
                    Source.id == Alert.source_id,
                    Source.organization_id == organization_id,
                ),
            )
            .where(
                Alert.id == selected_alert_id,
                Alert.organization_id == organization_id,
                Project.organization_id == organization_id,
                Project.deleted_at.is_(None),
            )
        ).first()
        if selected_row:
            alert, project, source = selected_row
            selected_alert = _alert_item(
                alert,
                project,
                source,
                _related_conflict(db, organization_id=organization_id, alert=alert),
            )
    latest = items[0] if items else None
    return {
        "alerts": items,
        "items": items,
        "selected_alert": selected_alert,
        "queue_source_name": (
            f"Review routing with {len({item['source_name'] for item in items})} linked source(s)"
            if items
            else "Deterministic signals and review routing"
        ),
        "queue_freshness": latest["observed_at"] if latest else "No matching review items",
        "queue_state": "needs_review" if items else "verified",
        "filters": {
            "q": normalized_query,
            "state": normalized_state,
            "severity": normalized_severity,
            "reason": normalized_reason,
            "project_id": project_id or "",
            "evidence_id": evidence_id or "",
        },
    }


def _audit_category(action: str) -> str:
    lowered = action.lower()
    if "auth" in lowered:
        return "Authentication"
    if any(token in lowered for token in ("monitor", "source", "snapshot", "document")):
        return "Monitoring"
    if any(token in lowered for token in ("review", "alert", "conflict", "correction", "merge")):
        return "Review"
    if "report" in lowered:
        return "Report"
    if any(token in lowered for token in ("delet", "retention")):
        return "Deletion"
    if "member" in lowered or "organization" in lowered:
        return "Organization"
    return "General"


def _audit_summary(action: str, resource_type: str | None) -> str:
    category = _audit_category(action)
    summaries = {
        "Authentication": "An authentication event was recorded for this account.",
        "Monitoring": "A monitored source, document, or snapshot workflow changed state.",
        "Review": "An evidence-linked human review or correction action was recorded.",
        "Report": "A report workflow action was recorded with its authenticated actor.",
        "Deletion": "A retention or deletion action was recorded under organization policy.",
        "Organization": "An organization access or policy action was recorded.",
    }
    return summaries.get(
        category,
        f"An audited action was recorded for {resource_type or 'an organization resource'}.",
    )


def audit_history_view(
    db: Session,
    *,
    organization_id: str,
    q: str | None = None,
    category: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    event_id: str | None = None,
) -> dict[str, Any]:
    query = (
        select(AuditEvent, User, OrganizationMembership)
        .outerjoin(User, User.id == AuditEvent.actor_user_id)
        .outerjoin(
            OrganizationMembership,
            and_(
                OrganizationMembership.user_id == AuditEvent.actor_user_id,
                OrganizationMembership.organization_id == organization_id,
            ),
        )
        .where(AuditEvent.organization_id == organization_id)
    )
    if event_id:
        query = query.where(AuditEvent.id == event_id)
    parsed_from = _parse_date(date_from)
    parsed_to = _parse_date(date_to)
    if parsed_from:
        query = query.where(
            AuditEvent.created_at >= datetime.combine(parsed_from, time.min, tzinfo=UTC)
        )
    if parsed_to:
        query = query.where(
            AuditEvent.created_at
            < datetime.combine(parsed_to + timedelta(days=1), time.min, tzinfo=UTC)
        )
    rows = list(db.execute(query.order_by(AuditEvent.created_at.desc()).limit(500)))
    normalized_query = (q or "").strip().casefold()
    normalized_category = (category or "").strip().casefold()
    events: list[dict[str, Any]] = []
    for event, user, membership in rows:
        event_category = _audit_category(event.action or "")
        if normalized_category and event_category.casefold() != normalized_category:
            continue
        actor_display = user.display_name if user else "System"
        searchable = " ".join(
            str(value or "")
            for value in (
                actor_display,
                user.email if user else None,
                event.action,
                event.resource_type,
                event.resource_id,
                event.request_id,
            )
        ).casefold()
        if normalized_query and normalized_query not in searchable:
            continue
        result = str(event.metadata_json.get("result", "")).strip().lower()
        outcome = (
            "Success"
            if result in {"success", "succeeded", "completed"}
            else "Failed"
            if result in {"failed", "error"}
            else "Recorded"
        )
        job_id = event.metadata_json.get("job_id")
        events.append(
            {
                "id": event.id,
                "timestamp": _display_timestamp(event.created_at),
                "actor_display": actor_display,
                "actor_role": membership.role if membership else "service",
                "action": event.action or "audit.recorded",
                "category": event_category,
                "resource_type": event.resource_type or "resource",
                "resource_id": event.resource_id,
                "outcome": outcome,
                "request_id": event.request_id,
                "job_id": str(job_id) if job_id else "",
                "safe_summary": _audit_summary(
                    event.action or "", event.resource_type
                ),
                "selected": event.id == event_id,
            }
        )
    return {
        "events": events,
        "items": events,
        "audit_freshness": events[0]["timestamp"] if events else "No matching events",
        "filters": {
            "q": q or "",
            "category": normalized_category,
            "from": date_from or "",
            "to": date_to or "",
            "event_id": event_id or "",
        },
    }


def source_conflict_center_view(
    db: Session,
    *,
    organization_id: str,
    selected_conflict_id: str | None = None,
    project_id: str | None = None,
    q: str | None = None,
    state: str | None = None,
    field_key: str | None = None,
) -> dict[str, Any]:
    query = (
        select(SourceConflict, Project)
        .join(Project, Project.id == SourceConflict.project_id)
        .where(
            SourceConflict.organization_id == organization_id,
            Project.organization_id == organization_id,
            Project.deleted_at.is_(None),
        )
    )
    normalized_query = (q or "").strip()
    normalized_state = (state or "open").strip().lower()
    normalized_field = (field_key or "").strip()
    if project_id:
        query = query.where(SourceConflict.project_id == project_id)
    if normalized_state and normalized_state != "all":
        if normalized_state == "open":
            query = query.where(SourceConflict.state.in_(["open", "unresolved"]))
        else:
            query = query.where(SourceConflict.state == normalized_state)
    if normalized_field:
        query = query.where(SourceConflict.field_key == normalized_field)
    if normalized_query:
        pattern = f"%{normalized_query}%"
        query = query.where(
            or_(
                Project.name.ilike(pattern),
                Project.registration_number.ilike(pattern),
                SourceConflict.field_key.ilike(pattern),
                SourceConflict.source_a_value.ilike(pattern),
                SourceConflict.source_b_value.ilike(pattern),
            )
        )
    rows = list(db.execute(query.order_by(SourceConflict.detected_at.desc())))
    selection_params = {
        key: value
        for key, value in {
            "q": normalized_query,
            "state": normalized_state,
            "field": normalized_field,
            "project_id": project_id or "",
        }.items()
        if value
    }
    conflicts: list[dict[str, Any]] = []
    for conflict, project in rows:
        source_a = (
            db.scalar(
                select(Source).where(
                    Source.id == conflict.source_a_id,
                    Source.organization_id == organization_id,
                )
            )
            if conflict.source_a_id
            else None
        )
        source_b = (
            db.scalar(
                select(Source).where(
                    Source.id == conflict.source_b_id,
                    Source.organization_id == organization_id,
                )
            )
            if conflict.source_b_id
            else None
        )
        values = [
            {
                "source_class": conflict.source_a_class.replace("_", " ").title(),
                "source_name": source_a.name if source_a else "Preserved source A",
                "value": conflict.source_a_value,
                "freshness": _display_timestamp(conflict.source_a_freshness_at),
                "source_date": conflict.source_a_freshness_at,
                "state": "verified" if conflict.source_a_value is not None else "unverified",
            },
            {
                "source_class": conflict.source_b_class.replace("_", " ").title(),
                "source_name": source_b.name if source_b else "Preserved source B",
                "value": conflict.source_b_value,
                "freshness": _display_timestamp(conflict.source_b_freshness_at),
                "source_date": conflict.source_b_freshness_at,
                "state": "verified" if conflict.source_b_value is not None else "unverified",
            },
        ]
        conflicts.append(
            {
                "id": conflict.id,
                "project_id": conflict.project_id,
                "project_name": project.name,
                "field_key": conflict.field_key,
                "field_label": conflict.field_key.replace("_", " ").title(),
                "source_values": values,
                "value_count": sum(item["value"] is not None for item in values),
                "source_classes": (
                    f"{conflict.source_a_class} / {conflict.source_b_class}"
                ),
                "freshness": _display_timestamp(
                    max(
                        (
                            value
                            for value in (
                                conflict.source_a_freshness_at,
                                conflict.source_b_freshness_at,
                            )
                            if value
                        ),
                        default=None,
                    )
                ),
                "materiality": (
                    "Material - review required" if conflict.is_material else "Review"
                ),
                "resolution_state": conflict.state.replace("_", " ").title(),
                "review_action": f"/api/v1/conflicts/{conflict.id}/review",
                "selection_href": (
                    f"/conflicts?{urlencode({**selection_params, 'selected': conflict.id})}"
                ),
            }
        )
    selected = next(
        (item for item in conflicts if item["id"] == selected_conflict_id), None
    )
    if selected_conflict_id and selected is None:
        selected_row = db.execute(
            select(SourceConflict, Project)
            .join(Project, Project.id == SourceConflict.project_id)
            .where(
                SourceConflict.id == selected_conflict_id,
                SourceConflict.organization_id == organization_id,
                Project.organization_id == organization_id,
                Project.deleted_at.is_(None),
            )
        ).first()
        if selected_row:
            selected_conflict, selected_project = selected_row
            fallback = source_conflict_center_view(
                db,
                organization_id=organization_id,
                selected_conflict_id=selected_conflict.id,
                project_id=selected_project.id,
                state="all",
                field_key=selected_conflict.field_key,
            )
            selected = fallback["selected_conflict"]
    latest = conflicts[0] if conflicts else None
    available_fields = list(
        db.scalars(
            select(SourceConflict.field_key)
            .where(SourceConflict.organization_id == organization_id)
            .distinct()
            .order_by(SourceConflict.field_key)
        )
    )
    return {
        "conflicts": conflicts,
        "items": conflicts,
        "selected_conflict": selected,
        "conflict_source_name": "Compared source facts and document claims",
        "conflict_freshness": latest["freshness"] if latest else "No current comparison",
        "conflict_state": "conflicting" if conflicts else "verified",
        "available_fields": available_fields,
        "filters": {
            "q": normalized_query,
            "state": normalized_state,
            "field": normalized_field,
            "project_id": project_id or "",
        },
    }


__all__ = ["alert_queue_view", "audit_history_view", "source_conflict_center_view"]
