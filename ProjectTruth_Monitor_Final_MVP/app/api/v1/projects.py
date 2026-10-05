from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, time, timedelta

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import func, select

from app.api.deps import AnalystActor, CSRFProtected, CurrentActor, DBSession
from app.api.utils import add_audit_event, commit_or_rollback
from app.models import (
    Alert,
    AlertReview,
    DetectedChange,
    Developer,
    Document,
    DocumentVersion,
    EntityMergeProposal,
    Location,
    MonitoringRun,
    Project,
    Report,
    ReportItem,
    Signal,
    Source,
    SourceConflict,
    SourceSnapshot,
    UserCorrection,
)
from app.schemas.projects import (
    ChangeResponse,
    ConflictResponse,
    PassportResponse,
    ProjectCreate,
    ProjectResponse,
    SignalResponse,
    TimelineEventResponse,
)
from app.services.identity import ProjectIdentity, resolve_project_identity
from app.services.impact_router import suggested_review_actions
from app.services.normalization import comparison_text, normalize_registration_number

router = APIRouter(prefix="/projects", tags=["Projects"])


def get_tenant_project(db: DBSession, organization_id: str, project_id: str) -> Project:
    project = db.scalar(
        select(Project).where(
            Project.id == project_id,
            Project.organization_id == organization_id,
            Project.deleted_at.is_(None),
        )
    )
    if project is None:
        raise HTTPException(status_code=404, detail={"code": "project_not_found"})
    return project


def _find_or_create_developer(db: DBSession, organization_id: str, name: str | None) -> Developer | None:
    normalized = comparison_text(name)
    if not normalized:
        return None
    developer = db.scalar(
        select(Developer).where(
            Developer.organization_id == organization_id,
            Developer.normalized_name == normalized,
        )
    )
    if developer is None:
        developer = Developer(
            organization_id=organization_id,
            canonical_name=(name or "").strip(),
            normalized_name=normalized,
            identity_status="unverified",
        )
        db.add(developer)
        db.flush()
    return developer


def _find_or_create_location(
    db: DBSession,
    organization_id: str,
    city: str,
    locality: str | None,
) -> Location:
    city_value = city.strip() or "Gurugram"
    locality_value = (locality or "").strip()
    location = db.scalar(
        select(Location).where(
            Location.organization_id == organization_id,
            Location.country == "India",
            Location.state == "Haryana",
            Location.city == city_value,
            Location.locality_or_sector == locality_value,
        )
    )
    if location is None:
        location = Location(
            organization_id=organization_id,
            country="India",
            state="Haryana",
            city=city_value,
            locality_or_sector=locality_value,
        )
        db.add(location)
        db.flush()
    return location


def create_or_resolve_project(
    db: DBSession,
    *,
    organization_id: str,
    payload: ProjectCreate,
    proposed_by_user_id: str | None = None,
) -> tuple[Project, bool]:
    registration = normalize_registration_number(payload.registration_number)
    existing_rows = list(
        db.execute(
            select(Project, Developer, Location)
            .outerjoin(Developer, Developer.id == Project.developer_id)
            .outerjoin(Location, Location.id == Project.location_id)
            .where(
                Project.organization_id == organization_id,
                Project.deleted_at.is_(None),
                Project.merged_into_project_id.is_(None),
            )
        )
    )
    existing_identities = [
        ProjectIdentity(
            project_id=project.id,
            registration_number=project.registration_number,
            official_source=project.official_url,
            project_name=project.name,
            developer_name=developer.canonical_name if developer else None,
            city=location.city if location else None,
            locality_or_sector=location.locality_or_sector if location else None,
            provisional=project.is_provisional,
        )
        for project, developer, location in existing_rows
    ]
    resolution = resolve_project_identity(
        ProjectIdentity(
            project_id="incoming",
            registration_number=registration,
            official_source=str(payload.official_url) if payload.official_url else None,
            project_name=payload.name,
            developer_name=payload.developer_name,
            city=payload.city,
            locality_or_sector=payload.locality_or_sector,
            provisional=not bool(registration),
        ),
        existing_identities,
    )
    if resolution.may_auto_link and resolution.project_id:
        existing = db.get(Project, resolution.project_id)
        if existing is not None:
            return existing, False
    if registration:
        merged = db.scalar(
            select(Project).where(
                Project.organization_id == organization_id,
                Project.registration_number == registration,
                Project.merged_into_project_id.is_not(None),
                Project.deleted_at.is_(None),
            )
        )
        if merged and merged.merged_into_project_id:
            target = get_tenant_project(db, organization_id, merged.merged_into_project_id)
            return target, False
    developer = _find_or_create_developer(db, organization_id, payload.developer_name)
    location = _find_or_create_location(
        db, organization_id, payload.city, payload.locality_or_sector
    )
    normalized_name = comparison_text(payload.name)
    if payload.parent_project_id:
        get_tenant_project(db, organization_id, payload.parent_project_id)
    identity_needs_review = resolution.requires_review and bool(
        resolution.candidate_project_ids
    )
    project = Project(
        organization_id=organization_id,
        developer_id=developer.id if developer else None,
        location_id=location.id,
        parent_project_id=payload.parent_project_id,
        name=payload.name.strip(),
        normalized_name=normalized_name,
        registration_number=registration,
        official_url=str(payload.official_url) if payload.official_url else None,
        phase_name=payload.phase_name,
        identity_status=(
            "needs_review"
            if identity_needs_review
            else "confirmed"
            if registration
            else "unverified"
        ),
        is_provisional=not bool(registration) or identity_needs_review,
    )
    db.add(project)
    db.flush()
    if proposed_by_user_id and identity_needs_review:
        for candidate_id in resolution.candidate_project_ids:
            deduplication_key = hashlib.sha256(
                f"project-merge:{organization_id}:{project.id}:{candidate_id}".encode()
            ).hexdigest()
            db.add(
                EntityMergeProposal(
                    organization_id=organization_id,
                    entity_type="project",
                    source_entity_id=project.id,
                    target_entity_id=candidate_id,
                    confidence=resolution.confidence,
                    reason_codes=list(resolution.reason_codes),
                    proposed_by_user_id=proposed_by_user_id,
                    deduplication_key=deduplication_key,
                )
            )
        db.flush()
    return project, True


@router.post("", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED)
def create_project(
    payload: ProjectCreate,
    request: Request,
    db: DBSession,
    actor: AnalystActor,
    csrf: CSRFProtected,
) -> Project:
    del csrf
    project, created = create_or_resolve_project(
        db,
        organization_id=actor.organization_id,
        payload=payload,
        proposed_by_user_id=actor.user_id,
    )
    add_audit_event(
        db,
        actor=actor,
        action="project.created" if created else "project.duplicate_resolved",
        resource_type="project",
        resource_id=project.id,
        request=request,
        metadata={
            "registration_number": project.registration_number,
            "identity_status": project.identity_status,
        },
    )
    commit_or_rollback(db)
    db.refresh(project)
    return project


@router.get("/{project_id}", response_model=ProjectResponse)
def get_project(project_id: str, db: DBSession, actor: CurrentActor) -> Project:
    return get_tenant_project(db, actor.organization_id, project_id)


@router.get("/{project_id}/passport", response_model=PassportResponse)
def get_passport(project_id: str, db: DBSession, actor: CurrentActor) -> PassportResponse:
    project = get_tenant_project(db, actor.organization_id, project_id)
    source_rows = list(
        db.execute(
            select(Source.source_class, func.count(func.distinct(Source.id)))
            .join(SourceSnapshot, SourceSnapshot.source_id == Source.id)
            .where(
                SourceSnapshot.organization_id == actor.organization_id,
                SourceSnapshot.project_id == project.id,
            )
            .group_by(Source.source_class)
        )
    )
    official_count = sum(
        int(count) for source_class, count in source_rows if source_class == "official_regulator"
    )
    marketing_count = sum(
        int(count) for source_class, count in source_rows if source_class != "official_regulator"
    )
    last_snapshot = db.scalar(
        select(SourceSnapshot)
        .where(
            SourceSnapshot.organization_id == actor.organization_id,
            SourceSnapshot.project_id == project.id,
        )
        .order_by(SourceSnapshot.fetched_at.desc())
    )
    open_alerts = int(
        db.scalar(
            select(func.count())
            .select_from(Alert)
            .where(
                Alert.organization_id == actor.organization_id,
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
                SourceConflict.organization_id == actor.organization_id,
                SourceConflict.project_id == project.id,
                SourceConflict.state == "open",
            )
        )
        or 0
    )
    latest_review = db.scalar(
        select(AlertReview)
        .join(Alert, Alert.id == AlertReview.alert_id)
        .where(
            AlertReview.organization_id == actor.organization_id,
            Alert.project_id == project.id,
        )
        .order_by(AlertReview.reviewed_at.desc())
    )
    completeness = last_snapshot.access_completeness if last_snapshot else "unavailable"
    conflict_band = "Conflicting sources" if unresolved_conflicts else "Confirmed"
    extraction_band = "High confidence" if last_snapshot else "Unverified"
    coverage = (
        "complete"
        if completeness == "complete"
        else "monitoring_incomplete" if last_snapshot else "not_started"
    )
    return PassportResponse(
        project=project,
        official_source_count=official_count,
        marketing_source_count=marketing_count,
        last_successful_verification=project.last_verified_at,
        monitoring_coverage_status=coverage,
        open_alerts=open_alerts,
        unresolved_conflicts=unresolved_conflicts,
        history_tracking_start=project.tracking_started_at,
        data_quality={
            "source_reliability": "Confirmed" if official_count else "Unverified",
            "source_access_completeness": completeness,
            "project_identity_confidence": (
                "Confirmed" if project.identity_status == "confirmed" else "Unverified"
            ),
            "extraction_confidence": extraction_band,
            "change_confidence": "Needs review" if open_alerts else "High confidence",
            "freshness": last_snapshot.fetched_at.isoformat() if last_snapshot else "Never checked",
            "conflict_state": conflict_band,
        },
        latest_reviewer_decision=latest_review.decision if latest_review else None,
    )


@router.get("/{project_id}/timeline", response_model=list[TimelineEventResponse])
def project_timeline(
    project_id: str, db: DBSession, actor: CurrentActor
) -> list[TimelineEventResponse]:
    project = get_tenant_project(db, actor.organization_id, project_id)
    events = [
        TimelineEventResponse(
            id=f"project:{project.id}",
            event_type="project_identity",
            title="Project tracking started",
            description=(
                f"Identity captured as {project.registration_number}."
                if project.registration_number
                else "Provisional identity captured; confirmation is required."
            ),
            occurred_at=project.tracking_started_at,
        )
    ]
    snapshots = db.scalars(
        select(SourceSnapshot)
        .where(
            SourceSnapshot.organization_id == actor.organization_id,
            SourceSnapshot.project_id == project.id,
        )
        .order_by(SourceSnapshot.fetched_at)
    )
    for index, snapshot in enumerate(snapshots):
        events.append(
            TimelineEventResponse(
                id=f"snapshot:{snapshot.id}",
                event_type="baseline_capture" if index == 0 else "source_check",
                title="Baseline captured" if index == 0 else "Source checked",
                description=(
                    "Source evidence was captured successfully."
                    if snapshot.access_completeness == "complete"
                    else "Only part of the source was readable. Some fields could not be verified."
                ),
                occurred_at=snapshot.fetched_at,
            )
        )
    changes = db.scalars(
        select(DetectedChange)
        .where(
            DetectedChange.organization_id == actor.organization_id,
            DetectedChange.project_id == project.id,
        )
        .order_by(DetectedChange.detected_at)
    )
    for change in changes:
        events.append(
            TimelineEventResponse(
                id=f"change:{change.id}",
                event_type=change.change_type,
                title=f"{change.field_key.replace('_', ' ').title()} changed",
                description=(
                    f"{change.old_canonical_value or 'Unknown'} -> "
                    f"{change.new_canonical_value or 'Unknown'}"
                ),
                occurred_at=change.detected_at,
                evidence_id=change.id,
            )
        )
    degraded_runs = db.scalars(
        select(MonitoringRun)
        .where(
            MonitoringRun.organization_id == actor.organization_id,
            MonitoringRun.project_id == project.id,
            (
                MonitoringRun.status.not_in(["succeeded", "completed"])
                | (MonitoringRun.access_completeness != "complete")
            ),
        )
        .order_by(MonitoringRun.created_at)
    )
    for run in degraded_runs:
        events.append(
            TimelineEventResponse(
                id=f"monitoring-run:{run.id}",
                event_type="source_unavailable_or_degraded",
                title="Source monitoring incomplete",
                description=(
                    "The source check was incomplete. Last verified facts remain visible; "
                    "no factual no-change conclusion was created."
                ),
                occurred_at=run.completed_at or run.created_at,
            )
        )
    document_versions = db.execute(
        select(Document, DocumentVersion)
        .join(DocumentVersion, DocumentVersion.document_id == Document.id)
        .where(
            Document.organization_id == actor.organization_id,
            Document.project_id == project.id,
        )
        .order_by(DocumentVersion.captured_at)
    )
    for document, version in document_versions:
        events.append(
            TimelineEventResponse(
                id=f"document-version:{version.id}",
                event_type="document_version",
                title="Authorized document captured",
                description=(
                    f"{document.title} version {version.version_number} was stored with "
                    "page-linked evidence available after extraction."
                ),
                occurred_at=version.captured_at,
            )
        )
    conflicts = db.scalars(
        select(SourceConflict).where(
            SourceConflict.organization_id == actor.organization_id,
            SourceConflict.project_id == project.id,
        )
    )
    for conflict in conflicts:
        events.append(
            TimelineEventResponse(
                id=f"conflict:{conflict.id}",
                event_type="source_conflict",
                title=f"{conflict.field_key.replace('_', ' ').title()} sources conflict",
                description="Both source values were preserved for human review.",
                occurred_at=conflict.detected_at,
            )
        )
    reviews = db.execute(
        select(AlertReview, Alert)
        .join(Alert, Alert.id == AlertReview.alert_id)
        .where(
            AlertReview.organization_id == actor.organization_id,
            Alert.project_id == project.id,
        )
    )
    for review, alert in reviews:
        events.append(
            TimelineEventResponse(
                id=f"alert-review:{review.id}",
                event_type="alert_review_decision",
                title=f"Review decision: {review.decision.replace('_', ' ')}",
                description=f"{alert.title} was reviewed with reason {review.reason_code}.",
                occurred_at=review.reviewed_at,
            )
        )
    corrections = db.scalars(
        select(UserCorrection).where(
            UserCorrection.organization_id == actor.organization_id,
            UserCorrection.project_id == project.id,
        )
    )
    for correction in corrections:
        events.append(
            TimelineEventResponse(
                id=f"correction:{correction.id}",
                event_type="user_correction",
                title=f"Correction submitted for {correction.field_key.replace('_', ' ')}",
                description="The correction is preserved separately from captured source facts.",
                occurred_at=correction.created_at,
            )
        )
    report_items = db.execute(
        select(ReportItem, Report)
        .join(Report, Report.id == ReportItem.report_id)
        .where(
            ReportItem.organization_id == actor.organization_id,
            ReportItem.project_id == project.id,
        )
    )
    for item, report in report_items:
        events.append(
            TimelineEventResponse(
                id=f"report-item:{item.id}",
                event_type="report_inclusion",
                title="Included in report",
                description=f"Included in {report.title} ({report.state}).",
                occurred_at=item.included_at,
            )
        )
    phases = db.scalars(
        select(Project).where(
            Project.organization_id == actor.organization_id,
            Project.parent_project_id == project.id,
            Project.deleted_at.is_(None),
        )
    )
    for phase in phases:
        events.append(
            TimelineEventResponse(
                id=f"phase:{phase.id}",
                event_type="new_phase_or_registration",
                title="New phase or registration linked",
                description=f"{phase.name} ({phase.registration_number or 'registration unverified'}).",
                occurred_at=phase.created_at,
            )
        )
    return sorted(events, key=lambda item: item.occurred_at, reverse=True)


@router.get("/{project_id}/changes", response_model=list[ChangeResponse])
def project_changes(
    project_id: str,
    db: DBSession,
    actor: CurrentActor,
    severity: str | None = None,
    review_status: str | None = None,
    change_type: str | None = None,
    field_key: str | None = None,
    source_id: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> list[DetectedChange]:
    get_tenant_project(db, actor.organization_id, project_id)
    query = select(DetectedChange).where(
        DetectedChange.organization_id == actor.organization_id,
        DetectedChange.project_id == project_id,
    )
    if severity:
        query = query.where(DetectedChange.severity == severity)
    if review_status:
        query = query.where(DetectedChange.review_state == review_status)
    if change_type:
        query = query.where(DetectedChange.change_type == change_type)
    if field_key:
        query = query.where(DetectedChange.field_key == field_key)
    if source_id:
        query = query.where(DetectedChange.source_id == source_id)
    if date_from:
        query = query.where(
            DetectedChange.detected_at >= datetime.combine(date_from, time.min, tzinfo=UTC)
        )
    if date_to:
        query = query.where(
            DetectedChange.detected_at
            < datetime.combine(date_to + timedelta(days=1), time.min, tzinfo=UTC)
        )
    return list(db.scalars(query.order_by(DetectedChange.detected_at.desc())))


@router.get("/{project_id}/signals", response_model=list[SignalResponse])
def project_signals(project_id: str, db: DBSession, actor: CurrentActor) -> list[Signal]:
    get_tenant_project(db, actor.organization_id, project_id)
    return list(
        db.scalars(
            select(Signal)
            .where(
                Signal.organization_id == actor.organization_id,
                Signal.project_id == project_id,
            )
            .order_by(Signal.detected_at.desc())
        )
    )


@router.get("/{project_id}/signals/{signal_id}/impact-actions")
def project_signal_impact_actions(
    project_id: str,
    signal_id: str,
    db: DBSession,
    actor: CurrentActor,
    roles: str = "property_adviser,supplier,lender,marketing_team",
) -> list[dict[str, str]]:
    get_tenant_project(db, actor.organization_id, project_id)
    signal = db.scalar(
        select(Signal).where(
            Signal.id == signal_id,
            Signal.organization_id == actor.organization_id,
            Signal.project_id == project_id,
        )
    )
    if signal is None:
        raise HTTPException(status_code=404, detail={"code": "signal_not_found"})
    selected_roles = tuple(item.strip() for item in roles.split(",") if item.strip())
    return [
        {
            "role": action.role,
            "rule_code": action.rule_code.value,
            "action": action.action,
            "reason": action.reason,
            "limitation": action.limitation,
        }
        for action in suggested_review_actions(signal.rule_code, selected_roles)
    ]


@router.get("/{project_id}/conflicts", response_model=list[ConflictResponse])
def project_conflicts(
    project_id: str, db: DBSession, actor: CurrentActor
) -> list[SourceConflict]:
    get_tenant_project(db, actor.organization_id, project_id)
    return list(
        db.scalars(
            select(SourceConflict)
            .where(
                SourceConflict.organization_id == actor.organization_id,
                SourceConflict.project_id == project_id,
            )
            .order_by(SourceConflict.detected_at.desc())
        )
    )


@router.get("/{project_id}/phase-map")
def project_phase_map(project_id: str, db: DBSession, actor: CurrentActor) -> dict[str, object]:
    project = get_tenant_project(db, actor.organization_id, project_id)
    parent = (
        get_tenant_project(db, actor.organization_id, project.parent_project_id)
        if project.parent_project_id
        else project
    )
    phases = list(
        db.scalars(
            select(Project)
            .where(
                Project.organization_id == actor.organization_id,
                Project.parent_project_id == parent.id,
                Project.deleted_at.is_(None),
            )
            .order_by(Project.name)
        )
    )
    return {
        "parent": ProjectResponse.model_validate(parent),
        "phases": [ProjectResponse.model_validate(item) for item in phases],
        "generated_at": datetime.now(UTC),
        "note": "Relationships reflect reviewed canonical identities; probable aliases are not merged.",
    }
