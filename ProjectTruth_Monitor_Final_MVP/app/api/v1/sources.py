from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import urlsplit, urlunsplit

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import func, select

from app.api.deps import AdminActor, AnalystActor, CSRFProtected, CurrentActor, DBSession
from app.api.utils import add_audit_event, commit_or_rollback
from app.api.v1.projects import get_tenant_project
from app.core.config import get_settings
from app.models import (
    MonitoringJob,
    MonitoringRun,
    ProjectSourceLink,
    Source,
    SourceAccessPolicy,
    SourceSnapshot,
)
from app.schemas.sources import (
    ProjectSourceCreate,
    SourceHealthResponse,
    SourcePolicyCreate,
    SourcePolicyResponse,
    SourcePolicyUpdate,
    SourceResponse,
)
from app.services.scheduler import next_run_at

router = APIRouter(tags=["Sources"])


def _normalized_endpoint_url(value: str | None) -> str | None:
    if value is None:
        return None
    parsed = urlsplit(value.strip())
    if parsed.scheme.lower() == "fixture":
        return value.strip().rstrip("/")
    hostname = (parsed.hostname or "").lower().rstrip(".")
    port = parsed.port
    include_port = port is not None and not (
        (parsed.scheme.lower() == "https" and port == 443)
        or (parsed.scheme.lower() == "http" and port == 80)
    )
    netloc = f"{hostname}:{port}" if include_port else hostname
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/")
    return urlunsplit(
        (parsed.scheme.lower(), netloc, path, parsed.query, "")
    )


def _matching_source(
    db: DBSession,
    *,
    organization_id: str,
    domain: str,
    name: str,
    source_type: str,
    base_url: str | None,
) -> Source | None:
    normalized_url = _normalized_endpoint_url(base_url)
    candidates = db.scalars(
        select(Source).where(
            Source.organization_id == organization_id,
            Source.domain == domain,
            Source.name == name,
            Source.source_type == source_type,
        )
    )
    return next(
        (
            candidate
            for candidate in candidates
            if _normalized_endpoint_url(candidate.base_url) == normalized_url
        ),
        None,
    )


def get_tenant_source(db: DBSession, organization_id: str, source_id: str) -> Source:
    source = db.scalar(
        select(Source).where(
            Source.id == source_id,
            Source.organization_id == organization_id,
        )
    )
    if source is None:
        raise HTTPException(status_code=404, detail={"code": "source_not_found"})
    return source


def _source_values(payload: ProjectSourceCreate) -> tuple[str, str | None, str, str, bool]:
    if payload.source_type == "fixture":
        base_url = f"fixture://{payload.fixture_key or 'clean_project'}"
        return "fixture.local", base_url, "official_regulator", "fixture", True
    if payload.url is None:
        raise HTTPException(
            status_code=422,
            detail={"code": "source_url_required", "message": "This source needs a URL."},
        )
    base_url = str(payload.url)
    domain = (urlsplit(base_url).hostname or "").lower()
    source_class = (
        "official_regulator"
        if payload.source_type == "hrera_gurugram"
        else "official_developer"
        if payload.source_type == "developer_page"
        else "user_authorized_document"
    )
    return domain, base_url, source_class, payload.source_type, False


@router.post(
    "/projects/{project_id}/sources",
    response_model=SourceResponse,
    status_code=status.HTTP_201_CREATED,
)
def add_project_source(
    project_id: str,
    payload: ProjectSourceCreate,
    request: Request,
    db: DBSession,
    actor: AnalystActor,
    csrf: CSRFProtected,
) -> Source:
    del csrf
    project = get_tenant_project(db, actor.organization_id, project_id)
    domain, base_url, source_class, source_type, enabled = _source_values(payload)
    source = _matching_source(
        db,
        organization_id=actor.organization_id,
        domain=domain,
        name=payload.name,
        source_type=source_type,
        base_url=base_url,
    )
    created = source is None
    if source is None:
        source = Source(
            organization_id=actor.organization_id,
            name=payload.name,
            source_type=source_type,
            source_class=source_class,
            base_url=base_url,
            domain=domain,
            is_enabled=enabled,
        )
        db.add(source)
        db.flush()
        policy = SourceAccessPolicy(
            organization_id=actor.organization_id,
            source_id=source.id,
            is_enabled=enabled,
            is_current=True,
            permitted_access_modes=["fixture"] if source_type == "fixture" else [],
            rate_limit_requests=1,
            rate_limit_period_seconds=1.0 if source_type == "fixture" else 3.0,
            robots_review_status="not_applicable" if source_type == "fixture" else "not_reviewed",
            terms_review_status="not_applicable" if source_type == "fixture" else "not_reviewed",
            evidence_retention_days=90,
            raw_content_may_be_stored=source_type == "fixture",
            policy_version="1.0",
            reviewer_user_id=actor.user_id if source_type == "fixture" else None,
            reviewed_at=datetime.now(UTC) if source_type == "fixture" else None,
        )
        db.add(policy)
    became_primary = (
        project.registration_source_id is None or source_class == "official_regulator"
    )
    if became_primary:
        project.registration_source_id = source.id
        for current_link in db.scalars(
            select(ProjectSourceLink).where(
                ProjectSourceLink.organization_id == actor.organization_id,
                ProjectSourceLink.project_id == project.id,
                ProjectSourceLink.source_role == "primary_registration",
            )
        ):
            current_link.source_role = "supporting"
    link = db.scalar(
        select(ProjectSourceLink).where(
            ProjectSourceLink.organization_id == actor.organization_id,
            ProjectSourceLink.project_id == project.id,
            ProjectSourceLink.source_id == source.id,
        )
    )
    if link is None:
        link = ProjectSourceLink(
            organization_id=actor.organization_id,
            project_id=project.id,
            source_id=source.id,
            source_role="primary_registration" if became_primary else "supporting",
            is_active=True,
        )
        db.add(link)
    else:
        link.source_role = "primary_registration" if became_primary else link.source_role
        link.is_active = True
    from app.models import WatchlistItem

    items = list(
        db.scalars(
            select(WatchlistItem).where(
                WatchlistItem.organization_id == actor.organization_id,
                WatchlistItem.project_id == project.id,
                WatchlistItem.status == "active",
            )
        )
    )
    for item in items:
        cadence = item.monitoring_cadence or get_settings().default_monitoring_cadence
        if cadence == "manual":
            continue
        job_key = f"watchlist-item:{item.id}:source:{source.id}"
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
                source_id=source.id,
                project_id=project.id,
                watchlist_id=item.watchlist_id,
                cadence=cadence,
                state="scheduled",
                next_run_at=next_run_at(None, cadence),
                deduplication_key=job_key,
                payload={"watchlist_item_id": item.id},
            )
        )
    add_audit_event(
        db,
        actor=actor,
        action="source.created" if created else "source.linked",
        resource_type="source",
        resource_id=source.id,
        request=request,
        metadata={"project_id": project.id, "enabled": source.is_enabled},
    )
    commit_or_rollback(db)
    db.refresh(source)
    return source


@router.get("/sources/{source_id}", response_model=SourceResponse)
def get_source(source_id: str, db: DBSession, actor: CurrentActor) -> Source:
    return get_tenant_source(db, actor.organization_id, source_id)


@router.get("/source-policies", response_model=list[SourcePolicyResponse])
def list_source_policies(
    db: DBSession, actor: CurrentActor
) -> list[SourceAccessPolicy]:
    return list(
        db.scalars(
            select(SourceAccessPolicy)
            .where(
                SourceAccessPolicy.organization_id == actor.organization_id,
                SourceAccessPolicy.is_current.is_(True),
            )
            .order_by(SourceAccessPolicy.created_at.desc())
        )
    )


@router.post(
    "/source-policies",
    response_model=SourcePolicyResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_source_policy(
    payload: SourcePolicyCreate,
    request: Request,
    db: DBSession,
    actor: AdminActor,
    csrf: CSRFProtected,
) -> SourceAccessPolicy:
    del csrf
    url = str(payload.base_url)
    domain = (urlsplit(url).hostname or "").lower()
    source = _matching_source(
        db,
        organization_id=actor.organization_id,
        domain=domain,
        name=payload.source_name,
        source_type=payload.source_type,
        base_url=url,
    )
    if source is None:
        source = Source(
            organization_id=actor.organization_id,
            name=payload.source_name,
            source_type=payload.source_type,
            source_class=(
                "official_regulator"
                if payload.source_type == "hrera_gurugram"
                else "official_developer"
            ),
            base_url=url,
            domain=domain,
            is_enabled=payload.enabled,
        )
        db.add(source)
        db.flush()
    for current in db.scalars(
        select(SourceAccessPolicy).where(
            SourceAccessPolicy.organization_id == actor.organization_id,
            SourceAccessPolicy.source_id == source.id,
            SourceAccessPolicy.is_current.is_(True),
        )
    ):
        current.is_current = False
    policy = SourceAccessPolicy(
        organization_id=actor.organization_id,
        source_id=source.id,
        is_enabled=payload.enabled,
        is_current=True,
        permitted_access_modes=payload.permitted_access_modes,
        rate_limit_requests=1,
        rate_limit_period_seconds=payload.rate_limit_seconds,
        robots_review_status=payload.robots_review_status,
        terms_review_status=payload.terms_review_status,
        evidence_retention_days=payload.evidence_retention_days,
        raw_content_may_be_stored=payload.raw_content_allowed,
        playwright_allowed=payload.playwright_allowed,
        ocr_allowed=payload.ocr_allowed,
        llm_allowed=payload.llm_allowed,
        policy_version=payload.policy_version,
        reviewer_user_id=actor.user_id,
        reviewed_at=datetime.now(UTC),
    )
    source.is_enabled = payload.enabled
    db.add(policy)
    db.flush()
    add_audit_event(
        db,
        actor=actor,
        action="source_policy.created",
        resource_type="source_access_policy",
        resource_id=policy.id,
        request=request,
        metadata={"source_id": source.id, "version": policy.policy_version},
    )
    commit_or_rollback(db)
    db.refresh(policy)
    return policy


@router.patch("/source-policies/{policy_id}", response_model=SourcePolicyResponse)
def update_source_policy(
    policy_id: str,
    payload: SourcePolicyUpdate,
    request: Request,
    db: DBSession,
    actor: AdminActor,
    csrf: CSRFProtected,
) -> SourceAccessPolicy:
    del csrf
    old = db.scalar(
        select(SourceAccessPolicy).where(
            SourceAccessPolicy.id == policy_id,
            SourceAccessPolicy.organization_id == actor.organization_id,
            SourceAccessPolicy.is_current.is_(True),
        )
    )
    if old is None:
        raise HTTPException(status_code=404, detail={"code": "source_policy_not_found"})
    data = payload.model_dump(exclude_unset=True)
    new_version = data.pop("policy_version", None) or f"{old.policy_version}.1"
    old.is_current = False
    policy = SourceAccessPolicy(
        organization_id=actor.organization_id,
        source_id=old.source_id,
        is_enabled=data.get("enabled", old.is_enabled),
        is_current=True,
        permitted_access_modes=data.get(
            "permitted_access_modes", old.permitted_access_modes
        ),
        rate_limit_requests=old.rate_limit_requests,
        rate_limit_period_seconds=data.get(
            "rate_limit_seconds", old.rate_limit_period_seconds
        ),
        robots_review_status=data.get("robots_review_status", old.robots_review_status),
        terms_review_status=data.get("terms_review_status", old.terms_review_status),
        evidence_retention_days=data.get(
            "evidence_retention_days", old.evidence_retention_days
        ),
        raw_content_may_be_stored=data.get(
            "raw_content_allowed", old.raw_content_may_be_stored
        ),
        playwright_allowed=data.get("playwright_allowed", old.playwright_allowed),
        ocr_allowed=data.get("ocr_allowed", old.ocr_allowed),
        llm_allowed=data.get("llm_allowed", old.llm_allowed),
        policy_version=new_version,
        reviewer_user_id=actor.user_id,
        reviewed_at=datetime.now(UTC),
        notes=old.notes,
    )
    source = get_tenant_source(db, actor.organization_id, old.source_id)
    source.is_enabled = policy.is_enabled
    db.add(policy)
    db.flush()
    add_audit_event(
        db,
        actor=actor,
        action="source_policy.updated",
        resource_type="source_access_policy",
        resource_id=policy.id,
        request=request,
        metadata={"supersedes": old.id, "version": new_version},
    )
    commit_or_rollback(db)
    db.refresh(policy)
    return policy


@router.get("/source-health", response_model=list[SourceHealthResponse])
def source_health(db: DBSession, actor: CurrentActor) -> list[SourceHealthResponse]:
    sources = list(
        db.scalars(
            select(Source)
            .where(Source.organization_id == actor.organization_id)
            .order_by(Source.name)
        )
    )
    result: list[SourceHealthResponse] = []
    for source in sources:
        runs = list(
            db.scalars(
                select(MonitoringRun).where(
                    MonitoringRun.organization_id == actor.organization_id,
                    MonitoringRun.source_id == source.id,
                )
            )
        )
        snapshots = list(
            db.scalars(
                select(SourceSnapshot).where(
                    SourceSnapshot.organization_id == actor.organization_id,
                    SourceSnapshot.source_id == source.id,
                )
            )
        )
        successes = sum(run.status == "succeeded" for run in runs)
        parsed = sum(snapshot.validation_status in {"valid", "partial"} for snapshot in snapshots)
        parser_failures = sum(
            run.error_category
            in {
                "layout_changed",
                "parser_failed",
                "pdf_unreadable",
                "invalid_layout",
            }
            for run in runs
        )
        parser_attempts = parsed + parser_failures
        policy = db.scalar(
            select(SourceAccessPolicy).where(
                SourceAccessPolicy.organization_id == actor.organization_id,
                SourceAccessPolicy.source_id == source.id,
                SourceAccessPolicy.is_current.is_(True),
            )
        )
        retry_count = int(
            db.scalar(
                select(func.count())
                .select_from(MonitoringJob)
                .where(
                    MonitoringJob.organization_id == actor.organization_id,
                    MonitoringJob.source_id == source.id,
                    MonitoringJob.state == "retry",
                )
            )
            or 0
        )
        latencies = [snapshot.latency_ms for snapshot in snapshots if snapshot.latency_ms is not None]
        result.append(
            SourceHealthResponse(
                source_id=source.id,
                source_name=source.name,
                last_successful_fetch=source.last_successful_fetch_at,
                fetch_success_rate=successes / len(runs) if runs else 0.0,
                parser_success_rate=parsed / parser_attempts if parser_attempts else 0.0,
                average_latency_ms=sum(latencies) / len(latencies) if latencies else 0.0,
                consecutive_failures=source.consecutive_failures,
                circuit_breaker_status=source.circuit_breaker_state,
                parser_version=snapshots[-1].parser_version if snapshots else None,
                policy_version=policy.policy_version if policy else None,
                jobs_awaiting_retry=retry_count,
            )
        )
    return result
