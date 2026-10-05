from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, text
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.api.ui import router as ui_router
from app.api.v1 import (
    alerts,
    auth,
    documents,
    imports,
    monitoring,
    organizations,
    projects,
    reports,
    sources,
    watchlists,
)
from app.core.config import ROOT_DIR, get_settings
from app.core.errors import DomainError
from app.core.logging import configure_logging
from app.core.middleware import RequestContextMiddleware
from app.db import engine

logger = logging.getLogger("projecttruth.scheduler")


def _monitoring_retry_at(now: datetime, retry_count: int) -> datetime:
    """Bounded exponential retry schedule, independent of the normal cadence."""

    delay_minutes = min(360, 5 * (2 ** max(0, retry_count - 1)))
    return now + timedelta(minutes=delay_minutes)


async def _dispatch_due_monitoring_jobs() -> None:
    """Run a bounded batch of due jobs through the same monitored refresh workflow."""

    from starlette.requests import Request as StarletteRequest

    from app.api.deps import ActorContext
    from app.api.v1.monitoring import execute_watchlist_refresh
    from app.db import session_scope
    from app.models import MonitoringJob, OrganizationMembership, User
    from app.schemas.sources import RefreshRequest
    from app.services.scheduler import monitoring_window, next_run_at

    now = datetime.now(UTC)
    with session_scope() as db:
        jobs = list(
            db.scalars(
                select(MonitoringJob)
                .where(
                    MonitoringJob.state.in_(["scheduled", "retry"]),
                    MonitoringJob.next_run_at.is_not(None),
                    MonitoringJob.next_run_at <= now,
                )
                .order_by(MonitoringJob.next_run_at)
                .limit(10)
            )
        )
        for job in jobs:
            item_id = str(job.payload.get("watchlist_item_id", ""))
            if not item_id:
                job.state = "failed"
                continue
            membership = db.scalar(
                select(OrganizationMembership)
                .where(
                    OrganizationMembership.organization_id == job.organization_id,
                    OrganizationMembership.is_active.is_(True),
                    OrganizationMembership.role.in_(["analyst", "reviewer", "admin"]),
                )
                .order_by(OrganizationMembership.created_at)
            )
            if membership is None:
                job.state = "failed"
                continue
            user = db.get(User, membership.user_id)
            if user is None or not user.is_active:
                job.state = "failed"
                continue
            scheduled_for = job.next_run_at or now
            window = monitoring_window(scheduled_for, job.cadence)
            key = f"scheduled:{job.id}:{window}:attempt:{job.retry_count + 1}"
            scope = {
                "type": "http",
                "asgi": {"version": "3.0", "spec_version": "2.4"},
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": "/scheduler",
                "raw_path": b"/scheduler",
                "query_string": b"",
                "headers": [],
                "client": None,
                "server": ("scheduler", 0),
            }
            request = StarletteRequest(scope)
            request.state.request_id = f"scheduler-{uuid4()}"
            job.state = "running"
            job.last_run_at = now
            try:
                result = await execute_watchlist_refresh(
                    item_id=item_id,
                    payload=RefreshRequest(idempotency_key=key),
                    request=request,
                    db=db,
                    actor=ActorContext(user=user, membership=membership),
                    csrf=None,
                    source_id=job.source_id,
                    monitoring_job_id=job.id,
                    trigger_type="scheduled",
                    attempt_number=job.retry_count + 1,
                    enforce_rate_limit=False,
                )
                run = result["monitoring_run"]
                if run.status == "succeeded":
                    job.state = "scheduled"
                    job.retry_count = 0
                    job.next_run_at = next_run_at(now, job.cadence, now=now)
                else:
                    job.retry_count += 1
                    job.state = "retry" if job.retry_count <= job.max_retries else "failed"
                    job.next_run_at = (
                        _monitoring_retry_at(now, job.retry_count)
                        if job.state == "retry"
                        else None
                    )
                logger.info(
                    "monitoring_job_dispatched state=%s retry_count=%s",
                    job.state,
                    job.retry_count,
                    extra={
                        "request_id": request.state.request_id,
                        "job_id": job.id,
                        "source_id": job.source_id,
                    },
                )
            except Exception:
                job.retry_count += 1
                job.state = "retry" if job.retry_count <= job.max_retries else "failed"
                job.next_run_at = (
                    _monitoring_retry_at(now, job.retry_count)
                    if job.state == "retry"
                    else None
                )
                logger.exception(
                    "monitoring_job_dispatch_error state=%s retry_count=%s",
                    job.state,
                    job.retry_count,
                    extra={
                        "request_id": getattr(request.state, "request_id", None),
                        "job_id": job.id,
                        "source_id": job.source_id,
                    },
                )


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.jobs.scheduler import LocalMonitoringScheduler

    settings = get_settings()
    settings.validate_for_startup()
    settings.ensure_runtime_directories()
    configure_logging()
    scheduler = LocalMonitoringScheduler(
        _dispatch_due_monitoring_jobs,
        enabled=settings.enable_scheduler,
        lock_path=ROOT_DIR / "data" / ".scheduler.lock",
        interval_seconds=60,
    )
    app.state.scheduler_started = scheduler.start()
    try:
        yield
    finally:
        scheduler.stop(wait=False)


def create_app() -> FastAPI:
    settings = get_settings()
    application = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        description=(
            "Evidence-first real-estate project monitoring. Data may be incomplete or delayed; "
            "this application is not legal, investment or title-verification advice."
        ),
        lifespan=lifespan,
    )
    application.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=settings.allowed_host_list,
    )
    application.add_middleware(GZipMiddleware, minimum_size=1_000)
    application.add_middleware(RequestContextMiddleware)
    static_dir = ROOT_DIR / "app" / "static"
    static_dir.mkdir(parents=True, exist_ok=True)
    application.mount("/static", StaticFiles(directory=static_dir), name="static")

    for api_router in (
        auth.router,
        organizations.router,
        watchlists.router,
        projects.router,
        imports.router,
        sources.router,
        monitoring.router,
        documents.router,
        alerts.router,
        reports.router,
    ):
        application.include_router(api_router, prefix="/api/v1")
    application.include_router(ui_router)

    @application.get("/health/live", tags=["Health"])
    def health_live() -> dict[str, str]:
        return {"status": "live"}

    @application.get("/health/ready", tags=["Health"])
    def health_ready() -> JSONResponse:
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
                context = MigrationContext.configure(connection)
                current = context.get_current_revision()
            config = Config(str(ROOT_DIR / "alembic.ini"))
            scripts = ScriptDirectory.from_config(config)
            expected = scripts.get_current_head()
            if current != expected:
                return JSONResponse(
                    status_code=503,
                    content={
                        "status": "not_ready",
                        "database": "migration_required",
                        "current_revision": current,
                        "expected_revision": expected,
                    },
                )
            return JSONResponse({"status": "ready", "database": "ready", "revision": current})
        except Exception:
            return JSONResponse(
                status_code=503,
                content={"status": "not_ready", "database": "unavailable"},
            )

    @application.get("/health", include_in_schema=False)
    def health_alias() -> dict[str, str]:
        return {"status": "live", "readiness": "/health/ready"}

    @application.exception_handler(DomainError)
    async def domain_error_handler(request: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": exc.code,
                    "message": str(exc),
                    "request_id": getattr(request.state, "request_id", None),
                }
            },
        )

    @application.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        errors = [
            {"location": list(item["loc"]), "message": item["msg"], "type": item["type"]}
            for item in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "validation_error",
                    "message": "The submitted data is invalid.",
                    "fields": errors,
                    "request_id": getattr(request.state, "request_id", None),
                }
            },
        )

    @application.exception_handler(HTTPException)
    async def http_error_handler(request: Request, exc: HTTPException) -> JSONResponse:
        detail = exc.detail
        if isinstance(detail, dict):
            code = str(detail.get("code", "request_failed"))
            message = str(detail.get("message", code.replace("_", " ").title()))
            extra = {key: value for key, value in detail.items() if key not in {"code", "message"}}
        else:
            code, message, extra = "request_failed", str(detail), {}
        return JSONResponse(
            status_code=exc.status_code,
            headers=exc.headers,
            content={
                "error": {
                    "code": code,
                    "message": message,
                    "request_id": getattr(request.state, "request_id", None),
                    **extra,
                }
            },
        )

    return application


app = create_app()
