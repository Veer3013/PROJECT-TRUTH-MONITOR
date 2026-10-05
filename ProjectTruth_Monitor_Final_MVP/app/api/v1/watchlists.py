from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import select

from app.api.deps import AnalystActor, CSRFProtected, CurrentActor, DBSession
from app.api.utils import add_audit_event, commit_or_rollback
from app.api.v1.projects import create_or_resolve_project
from app.models import MonitoringJob, ProjectSourceLink, Watchlist, WatchlistItem
from app.schemas.common import DeleteResponse
from app.schemas.projects import ProjectCreate
from app.schemas.watchlists import (
    WatchlistCreate,
    WatchlistItemCreate,
    WatchlistItemResponse,
    WatchlistResponse,
    WatchlistUpdate,
)
from app.services.scheduler import next_run_at

router = APIRouter(prefix="/watchlists", tags=["Watchlists"])


def get_tenant_watchlist(db: DBSession, organization_id: str, watchlist_id: str) -> Watchlist:
    watchlist = db.scalar(
        select(Watchlist).where(
            Watchlist.id == watchlist_id,
            Watchlist.organization_id == organization_id,
            Watchlist.deleted_at.is_(None),
        )
    )
    if watchlist is None:
        raise HTTPException(status_code=404, detail={"code": "watchlist_not_found"})
    return watchlist


@router.post("", response_model=WatchlistResponse, status_code=status.HTTP_201_CREATED)
def create_watchlist(
    payload: WatchlistCreate,
    request: Request,
    db: DBSession,
    actor: AnalystActor,
    csrf: CSRFProtected,
) -> Watchlist:
    del csrf
    existing = db.scalar(
        select(Watchlist).where(
            Watchlist.organization_id == actor.organization_id,
            Watchlist.name == payload.name,
            Watchlist.deleted_at.is_(None),
        )
    )
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail={"code": "watchlist_exists", "message": "A watchlist with this name exists."},
        )
    watchlist = Watchlist(
        organization_id=actor.organization_id,
        created_by_user_id=actor.user_id,
        name=payload.name,
        monitoring_cadence=payload.monitoring_cadence,
        alert_preferences=payload.alert_preferences,
    )
    db.add(watchlist)
    db.flush()
    add_audit_event(
        db,
        actor=actor,
        action="watchlist.created",
        resource_type="watchlist",
        resource_id=watchlist.id,
        request=request,
        metadata={"cadence": payload.monitoring_cadence},
    )
    commit_or_rollback(db)
    db.refresh(watchlist)
    return watchlist


@router.get("", response_model=list[WatchlistResponse])
def list_watchlists(db: DBSession, actor: CurrentActor) -> list[Watchlist]:
    return list(
        db.scalars(
            select(Watchlist)
            .where(
                Watchlist.organization_id == actor.organization_id,
                Watchlist.deleted_at.is_(None),
            )
            .order_by(Watchlist.name)
        )
    )


@router.get("/{watchlist_id}", response_model=WatchlistResponse)
def get_watchlist(watchlist_id: str, db: DBSession, actor: CurrentActor) -> Watchlist:
    return get_tenant_watchlist(db, actor.organization_id, watchlist_id)


@router.patch("/{watchlist_id}", response_model=WatchlistResponse)
def update_watchlist(
    watchlist_id: str,
    payload: WatchlistUpdate,
    request: Request,
    db: DBSession,
    actor: AnalystActor,
    csrf: CSRFProtected,
) -> Watchlist:
    del csrf
    watchlist = get_tenant_watchlist(db, actor.organization_id, watchlist_id)
    data = payload.model_dump(exclude_unset=True)
    if "name" in data:
        watchlist.name = data["name"].strip()
    if "monitoring_cadence" in data:
        watchlist.monitoring_cadence = data["monitoring_cadence"]
    if "alert_preferences" in data:
        watchlist.alert_preferences = data["alert_preferences"]
    add_audit_event(
        db,
        actor=actor,
        action="watchlist.updated",
        resource_type="watchlist",
        resource_id=watchlist.id,
        request=request,
        metadata={"changes": payload.model_dump(mode="json", exclude_unset=True)},
    )
    commit_or_rollback(db)
    db.refresh(watchlist)
    return watchlist


@router.delete("/{watchlist_id}", response_model=DeleteResponse)
def delete_watchlist(
    watchlist_id: str,
    request: Request,
    db: DBSession,
    actor: AnalystActor,
    csrf: CSRFProtected,
) -> DeleteResponse:
    del csrf
    from datetime import UTC, datetime

    watchlist = db.scalar(
        select(Watchlist).where(
            Watchlist.id == watchlist_id,
            Watchlist.organization_id == actor.organization_id,
        )
    )
    if watchlist is None:
        return DeleteResponse(deleted=False)
    already_deleted = watchlist.deleted_at is not None
    if not already_deleted:
        watchlist.deleted_at = datetime.now(UTC)
        watchlist.is_active = False
        for item in db.scalars(
            select(WatchlistItem).where(WatchlistItem.watchlist_id == watchlist.id)
        ):
            item.status = "inactive"
        for job in db.scalars(
            select(MonitoringJob).where(
                MonitoringJob.organization_id == actor.organization_id,
                MonitoringJob.watchlist_id == watchlist.id,
                MonitoringJob.state.in_(["scheduled", "retry", "queued", "running"]),
            )
        ):
            job.state = "cancelled"
        add_audit_event(
            db,
            actor=actor,
            action="watchlist.deleted",
            resource_type="watchlist",
            resource_id=watchlist.id,
            request=request,
        )
        commit_or_rollback(db)
    return DeleteResponse(deleted=not already_deleted)


@router.post(
    "/{watchlist_id}/items",
    response_model=WatchlistItemResponse,
    status_code=status.HTTP_201_CREATED,
)
def add_watchlist_item(
    watchlist_id: str,
    payload: WatchlistItemCreate,
    request: Request,
    db: DBSession,
    actor: AnalystActor,
    csrf: CSRFProtected,
) -> WatchlistItem:
    del csrf
    watchlist = get_tenant_watchlist(db, actor.organization_id, watchlist_id)
    if payload.project_id:
        from app.api.v1.projects import get_tenant_project

        project = get_tenant_project(db, actor.organization_id, payload.project_id)
    else:
        if not payload.registration_number and not payload.project_name:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "identity_required",
                    "message": "Provide a registration number or project identity details.",
                },
            )
        project, _ = create_or_resolve_project(
            db,
            organization_id=actor.organization_id,
            proposed_by_user_id=actor.user_id,
            payload=ProjectCreate(
                registration_number=payload.registration_number,
                name=payload.project_name or payload.registration_number or "Unverified project",
                developer_name=payload.developer_name,
                city=payload.city or "Gurugram",
                locality_or_sector=payload.locality_or_sector,
                official_url=payload.official_url,
            ),
        )
    existing = db.scalar(
        select(WatchlistItem).where(
            WatchlistItem.organization_id == actor.organization_id,
            WatchlistItem.watchlist_id == watchlist.id,
            WatchlistItem.project_id == project.id,
        )
    )
    if existing is not None:
        return existing
    item = WatchlistItem(
        organization_id=actor.organization_id,
        watchlist_id=watchlist.id,
        project_id=project.id,
        official_url=str(payload.official_url) if payload.official_url else None,
        monitoring_cadence=payload.monitoring_cadence or watchlist.monitoring_cadence,
        input_identity=payload.model_dump(mode="json"),
        status="active",
        added_by_user_id=actor.user_id,
    )
    db.add(item)
    db.flush()
    source_ids = list(
        db.scalars(
            select(ProjectSourceLink.source_id).where(
                ProjectSourceLink.organization_id == actor.organization_id,
                ProjectSourceLink.project_id == project.id,
                ProjectSourceLink.is_active.is_(True),
            )
        )
    )
    if project.registration_source_id and project.registration_source_id not in source_ids:
        source_ids.append(project.registration_source_id)
    if item.monitoring_cadence != "manual":
        for source_id in source_ids:
            job_key = f"watchlist-item:{item.id}:source:{source_id}"
            if db.scalar(
                select(MonitoringJob.id).where(
                    MonitoringJob.organization_id == actor.organization_id,
                    MonitoringJob.deduplication_key == job_key,
                )
            ):
                continue
            db.add(
                MonitoringJob(
                    organization_id=actor.organization_id,
                    source_id=source_id,
                    project_id=project.id,
                    watchlist_id=watchlist.id,
                    cadence=item.monitoring_cadence,
                    state="scheduled",
                    next_run_at=next_run_at(None, item.monitoring_cadence),
                    deduplication_key=job_key,
                    payload={"watchlist_item_id": item.id},
                )
            )
    add_audit_event(
        db,
        actor=actor,
        action="watchlist.item_added",
        resource_type="watchlist_item",
        resource_id=item.id,
        request=request,
        metadata={"project_id": project.id},
    )
    commit_or_rollback(db)
    db.refresh(item)
    return item


@router.delete("/{watchlist_id}/items/{item_id}", response_model=DeleteResponse)
def delete_watchlist_item(
    watchlist_id: str,
    item_id: str,
    request: Request,
    db: DBSession,
    actor: AnalystActor,
    csrf: CSRFProtected,
) -> DeleteResponse:
    del csrf
    get_tenant_watchlist(db, actor.organization_id, watchlist_id)
    item = db.scalar(
        select(WatchlistItem).where(
            WatchlistItem.id == item_id,
            WatchlistItem.watchlist_id == watchlist_id,
            WatchlistItem.organization_id == actor.organization_id,
        )
    )
    if item is None:
        return DeleteResponse(deleted=False)
    if item.status == "inactive":
        return DeleteResponse(deleted=False)
    item.status = "inactive"
    for job in db.scalars(
        select(MonitoringJob).where(
            MonitoringJob.organization_id == actor.organization_id,
            MonitoringJob.payload["watchlist_item_id"].as_string() == item.id,
            MonitoringJob.state.in_(["scheduled", "retry", "queued", "running"]),
        )
    ):
        job.state = "cancelled"
    add_audit_event(
        db,
        actor=actor,
        action="watchlist.item_removed",
        resource_type="watchlist_item",
        resource_id=item.id,
        request=request,
    )
    commit_or_rollback(db)
    return DeleteResponse(deleted=True)
