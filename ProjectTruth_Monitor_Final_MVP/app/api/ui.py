from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from app.api.deps import ActorContext, DBSession
from app.core.config import ROOT_DIR, get_settings
from app.core.rate_limit import rate_limiter
from app.core.security import (
    create_session_token,
    csrf_token_for_session,
    read_session_token,
    verify_password,
)
from app.models import (
    AuditEvent,
    DetectedChange,
    Document,
    Organization,
    OrganizationMembership,
    Project,
    Report,
    User,
    Watchlist,
)
from app.services.document_read_models import project_documents_view
from app.services.governance_read_models import (
    alert_queue_view,
    audit_history_view,
    source_conflict_center_view,
)
from app.services.operational_read_models import (
    dashboard_view,
    market_view,
    reports_view,
    source_health_view,
    watchlist_detail_view,
    watchlists_view,
    weekly_digest_view,
)
from app.services.project_read_models import (
    change_evidence_card_view,
    project_change_ledger_view,
    project_phase_map_view,
    project_profile_view,
    project_timeline_view,
    project_truth_passport_view,
)

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=str(ROOT_DIR / "app" / "templates"))


def _page_actor(request: Request, db: DBSession) -> ActorContext | None:
    token = request.cookies.get(get_settings().session_cookie_name)
    identity = read_session_token(token, get_settings()) if token else None
    if identity is None:
        return None
    user = db.scalar(select(User).where(User.id == identity.user_id, User.is_active.is_(True)))
    if user is None:
        return None
    membership = db.scalar(
        select(OrganizationMembership)
        .join(
            Organization,
            Organization.id == OrganizationMembership.organization_id,
        )
        .where(
            OrganizationMembership.user_id == user.id,
            OrganizationMembership.is_active.is_(True),
            Organization.is_active.is_(True),
        )
        .order_by(OrganizationMembership.created_at)
    )
    return ActorContext(user, membership) if membership else None


def _login_redirect(request: Request) -> RedirectResponse:
    next_path = request.url.path if request.url.path.startswith("/") else "/dashboard"
    return RedirectResponse(f"/login?next={next_path}", status_code=303)


def _base_context(request: Request, actor: ActorContext, **values: object) -> dict[str, object]:
    token = request.cookies.get(get_settings().session_cookie_name, "")
    return {
        "request": request,
        "current_user": actor.user,
        "user": actor.user,
        "membership": actor.membership,
        "organization": getattr(
            actor.membership, "organization", {"name": "ProjectTruth Monitor"}
        ),
        "organization_id": actor.organization_id,
        "csrf_token": csrf_token_for_session(token) if token else "",
        "now": datetime.now(UTC),
        "page": 1,
        "total_pages": 1,
        "items": [],
        "alerts": [],
        "conflicts": [],
        "events": [],
        "reports": [],
        "sources": [],
        "documents": [],
        "facts": {},
        "passport": {},
        "confidence": {},
        "summary": {},
        "stats": {},
        "digest": {},
        "highlights": [],
        "recent_activity": [],
        "recent_alerts": [],
        "source_summary": {},
        "preview_rows": [],
        "import_summary": {},
        "artifact_inventory": {},
        "retention_settings": {},
        "incidents": [],
        "proposals": [],
        **values,
    }


def _render(
    name: str, request: Request, actor: ActorContext, **values: object
) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name=name,
        context=_base_context(request, actor, **values),
    )


@router.get("/", response_class=HTMLResponse)
def root(request: Request, db: DBSession):
    return RedirectResponse("/dashboard" if _page_actor(request, db) else "/login", status_code=303)


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, db: DBSession):
    if _page_actor(request, db):
        return RedirectResponse("/dashboard", status_code=303)
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "request": request,
            "error": None,
            "next_url": request.query_params.get("next", ""),
        },
    )


@router.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    db: DBSession,
    email: str = Form(...),
    password: str = Form(...),
    next: str = Form(default=""),
):
    settings = get_settings()
    client_key = request.client.host if request.client else "unknown"
    rate_limiter.check(f"login:{client_key}", limit=settings.login_rate_limit)
    user = db.scalar(select(User).where(User.email == email.strip().lower()))
    if user is None or not user.is_active or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "request": request,
                "error": "Email or password is incorrect.",
                "next_url": next,
            },
            status_code=401,
        )
    membership = db.scalar(
        select(OrganizationMembership)
        .join(
            Organization,
            Organization.id == OrganizationMembership.organization_id,
        )
        .where(
            OrganizationMembership.user_id == user.id,
            OrganizationMembership.is_active.is_(True),
            Organization.is_active.is_(True),
        )
        .order_by(OrganizationMembership.created_at)
    )
    if membership is None:
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "request": request,
                "error": "No active organization access was found.",
                "next_url": next,
            },
            status_code=403,
        )
    user.last_login_at = datetime.now(UTC)
    db.add(
        AuditEvent(
            organization_id=membership.organization_id,
            actor_user_id=user.id,
            action="authentication.login",
            resource_type="user",
            resource_id=user.id,
            request_id=request.state.request_id,
            metadata_json={"surface": "dashboard"},
        )
    )
    db.commit()
    target = next if next.startswith("/") and not next.startswith("//") else "/dashboard"
    response = RedirectResponse(target, status_code=303)
    response.set_cookie(
        settings.session_cookie_name,
        create_session_token(user.id),
        max_age=settings.session_max_age_seconds,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        path="/",
    )
    return response


@router.post("/logout")
async def logout_page(request: Request, db: DBSession):
    actor = _page_actor(request, db)
    if actor:
        form = await request.form()
        token = request.cookies.get(get_settings().session_cookie_name, "")
        from app.core.security import csrf_token_matches

        if not csrf_token_matches(token, str(form.get("csrf_token", ""))):
            return HTMLResponse("Invalid form security token.", status_code=403)
        db.add(
            AuditEvent(
                organization_id=actor.organization_id,
                actor_user_id=actor.user_id,
                action="authentication.logout",
                resource_type="user",
                resource_id=actor.user_id,
                request_id=request.state.request_id,
                metadata_json={"surface": "dashboard"},
            )
        )
        db.commit()
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(get_settings().session_cookie_name, path="/")
    return response


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, db: DBSession):
    actor = _page_actor(request, db)
    if not actor:
        return _login_redirect(request)
    view = dashboard_view(db, organization_id=actor.organization_id)
    return _render(
        "dashboard.html",
        request,
        actor,
        **view,
    )


@router.get("/watchlists", response_class=HTMLResponse)
def watchlists_page(request: Request, db: DBSession, page: int = 1):
    actor = _page_actor(request, db)
    if not actor:
        return _login_redirect(request)
    view = watchlists_view(
        db,
        organization_id=actor.organization_id,
        page=page,
    )
    return _render("watchlists/list.html", request, actor, items=view["watchlists"], **view)


@router.get("/watchlists/import", response_class=HTMLResponse)
def watchlist_import_alias(request: Request, db: DBSession):
    return watchlist_import_chooser(request, db)


@router.get("/watchlists/{watchlist_id}", response_class=HTMLResponse)
def watchlist_detail(watchlist_id: str, request: Request, db: DBSession):
    actor = _page_actor(request, db)
    if not actor:
        return _login_redirect(request)
    watchlist = db.scalar(
        select(Watchlist).where(
            Watchlist.id == watchlist_id,
            Watchlist.organization_id == actor.organization_id,
            Watchlist.deleted_at.is_(None),
        )
    )
    if not watchlist:
        return HTMLResponse("Watchlist not found", status_code=404)
    view = watchlist_detail_view(
        db,
        organization_id=actor.organization_id,
        watchlist=watchlist,
    )
    return _render(
        "watchlists/detail.html",
        request,
        actor,
        projects=view["items"],
        **view,
    )


@router.get("/watchlists/{watchlist_id}/import", response_class=HTMLResponse)
def watchlist_import(watchlist_id: str, request: Request, db: DBSession):
    actor = _page_actor(request, db)
    if not actor:
        return _login_redirect(request)
    watchlist = db.scalar(
        select(Watchlist).where(
            Watchlist.id == watchlist_id,
            Watchlist.organization_id == actor.organization_id,
            Watchlist.deleted_at.is_(None),
        )
    )
    if not watchlist:
        return HTMLResponse("Watchlist not found", status_code=404)
    return _render("watchlists/import.html", request, actor, watchlist=watchlist, rows=[])


@router.get("/watchlists/import/choose", response_class=HTMLResponse)
def watchlist_import_chooser(request: Request, db: DBSession):
    actor = _page_actor(request, db)
    if not actor:
        return _login_redirect(request)
    watchlist_id = request.query_params.get("watchlist_id")
    if not watchlist_id:
        watchlist_id = db.scalar(
            select(Watchlist.id)
            .where(
                Watchlist.organization_id == actor.organization_id,
                Watchlist.deleted_at.is_(None),
            )
            .order_by(Watchlist.name)
        )
    return (
        RedirectResponse(f"/watchlists/{watchlist_id}/import", status_code=303)
        if watchlist_id
        else RedirectResponse("/watchlists", status_code=303)
    )


def _ui_project(project_id: str, actor: ActorContext, db: DBSession) -> Project | None:
    return db.scalar(
        select(Project).where(
            Project.id == project_id,
            Project.organization_id == actor.organization_id,
            Project.deleted_at.is_(None),
        )
    )


@router.get("/projects/{project_id}", response_class=HTMLResponse)
def project_profile(project_id: str, request: Request, db: DBSession):
    actor = _page_actor(request, db)
    if not actor:
        return _login_redirect(request)
    project = _ui_project(project_id, actor, db)
    if not project:
        return HTMLResponse("Project not found", status_code=404)
    view = project_profile_view(
        db,
        organization_id=actor.organization_id,
        project=project,
    )
    return _render(
        "projects/profile.html",
        request,
        actor,
        **view,
    )


@router.get("/projects/{project_id}/passport", response_class=HTMLResponse)
def project_passport(project_id: str, request: Request, db: DBSession):
    actor = _page_actor(request, db)
    if not actor:
        return _login_redirect(request)
    project = _ui_project(project_id, actor, db)
    if not project:
        return HTMLResponse("Project not found", status_code=404)
    view = project_truth_passport_view(
        db, organization_id=actor.organization_id, project=project
    )
    return _render(
        "projects/passport.html",
        request,
        actor,
        project=project,
        **view,
    )


@router.get("/projects/{project_id}/phase-map", response_class=HTMLResponse)
def phase_map_page(project_id: str, request: Request, db: DBSession):
    actor = _page_actor(request, db)
    if not actor:
        return _login_redirect(request)
    project = _ui_project(project_id, actor, db)
    if not project:
        return HTMLResponse("Project not found", status_code=404)
    view = project_phase_map_view(
        db,
        organization_id=actor.organization_id,
        project=project,
    )
    return _render("projects/phase_map.html", request, actor, **view)


@router.get("/projects/{project_id}/timeline", response_class=HTMLResponse)
def timeline_page(project_id: str, request: Request, db: DBSession):
    actor = _page_actor(request, db)
    if not actor:
        return _login_redirect(request)
    project = _ui_project(project_id, actor, db)
    if not project:
        return HTMLResponse("Project not found", status_code=404)
    view = project_timeline_view(
        db,
        organization_id=actor.organization_id,
        project=project,
        event_type=request.query_params.get("event_type"),
        date_from=request.query_params.get("from"),
        date_to=request.query_params.get("to"),
    )
    return _render(
        "projects/timeline.html", request, actor, project=project, **view
    )


@router.get("/projects/{project_id}/changes", response_class=HTMLResponse)
def changes_page(project_id: str, request: Request, db: DBSession):
    actor = _page_actor(request, db)
    if not actor:
        return _login_redirect(request)
    project = _ui_project(project_id, actor, db)
    if not project:
        return HTMLResponse("Project not found", status_code=404)
    view = project_change_ledger_view(
        db,
        organization_id=actor.organization_id,
        project=project,
        field_key=request.query_params.get("field"),
        severity=request.query_params.get("severity"),
        review_status=request.query_params.get("review_status"),
        change_type=request.query_params.get("change_type"),
        source_id=request.query_params.get("source_id"),
        date_from=request.query_params.get("date_from"),
        date_to=request.query_params.get("date_to"),
    )
    return _render(
        "projects/changes.html", request, actor, project=project, **view
    )


@router.get("/evidence/{change_id}", response_class=HTMLResponse)
def evidence_page(change_id: str, request: Request, db: DBSession):
    actor = _page_actor(request, db)
    if not actor:
        return _login_redirect(request)
    change = db.scalar(
        select(DetectedChange).where(
            DetectedChange.id == change_id,
            DetectedChange.organization_id == actor.organization_id,
        )
    )
    if not change:
        return HTMLResponse("Evidence not found", status_code=404)
    view = change_evidence_card_view(
        db, organization_id=actor.organization_id, change=change
    )
    return _render("evidence/detail.html", request, actor, **view)


@router.get("/alerts", response_class=HTMLResponse)
def alerts_page(request: Request, db: DBSession):
    actor = _page_actor(request, db)
    if not actor:
        return _login_redirect(request)
    view = alert_queue_view(
        db,
        organization_id=actor.organization_id,
        selected_alert_id=request.query_params.get("selected"),
        q=request.query_params.get("q"),
        state=request.query_params.get("state"),
        severity=request.query_params.get("severity"),
        reason=request.query_params.get("reason"),
        project_id=request.query_params.get("project_id"),
        evidence_id=request.query_params.get("evidence_id"),
    )
    if request.query_params.get("selected") and view["selected_alert"] is None:
        return HTMLResponse("Alert not found", status_code=404)
    return _render("alerts/list.html", request, actor, **view)


@router.get("/alerts/{alert_id}", response_class=HTMLResponse)
def alert_detail_page(alert_id: str, request: Request, db: DBSession):
    actor = _page_actor(request, db)
    if not actor:
        return _login_redirect(request)
    view = alert_queue_view(
        db,
        organization_id=actor.organization_id,
        selected_alert_id=alert_id,
        q=request.query_params.get("q"),
        state=request.query_params.get("state"),
        severity=request.query_params.get("severity"),
        reason=request.query_params.get("reason"),
        project_id=request.query_params.get("project_id"),
        evidence_id=request.query_params.get("evidence_id"),
    )
    if view["selected_alert"] is None:
        return HTMLResponse("Alert not found", status_code=404)
    return _render("alerts/list.html", request, actor, **view)


@router.get("/conflicts", response_class=HTMLResponse)
def conflicts_page(request: Request, db: DBSession):
    actor = _page_actor(request, db)
    if not actor:
        return _login_redirect(request)
    view = source_conflict_center_view(
        db,
        organization_id=actor.organization_id,
        selected_conflict_id=request.query_params.get("selected"),
        project_id=request.query_params.get("project_id"),
        q=request.query_params.get("q"),
        state=request.query_params.get("state"),
        field_key=request.query_params.get("field"),
    )
    if request.query_params.get("selected") and view["selected_conflict"] is None:
        return HTMLResponse("Conflict not found", status_code=404)
    return _render("conflicts/list.html", request, actor, **view)


@router.get("/conflicts/{conflict_id}", response_class=HTMLResponse)
def conflict_detail_page(conflict_id: str, request: Request, db: DBSession):
    actor = _page_actor(request, db)
    if not actor:
        return _login_redirect(request)
    view = source_conflict_center_view(
        db,
        organization_id=actor.organization_id,
        selected_conflict_id=conflict_id,
        project_id=request.query_params.get("project_id"),
        q=request.query_params.get("q"),
        state=request.query_params.get("state"),
        field_key=request.query_params.get("field"),
    )
    if view["selected_conflict"] is None:
        return HTMLResponse("Conflict not found", status_code=404)
    return _render("conflicts/list.html", request, actor, **view)


@router.get("/projects/{project_id}/documents", response_class=HTMLResponse)
def documents_page(
    project_id: str,
    request: Request,
    db: DBSession,
    document_id: str | None = None,
):
    actor = _page_actor(request, db)
    if not actor:
        return _login_redirect(request)
    project = _ui_project(project_id, actor, db)
    if not project:
        return HTMLResponse("Project not found", status_code=404)
    view = project_documents_view(
        db,
        organization_id=actor.organization_id,
        project=project,
        selected_document_id=document_id,
    )
    settings = get_settings()
    return _render(
        "documents/upload.html",
        request,
        actor,
        project=project,
        max_upload_mb=settings.max_upload_mb,
        ocr_state=settings.ocr_provider,
        llm_state=settings.llm_provider,
        can_manage_documents=actor.role in {"admin", "reviewer", "analyst"},
        **view,
    )


@router.get("/markets/summary", response_class=HTMLResponse)
def market_summary_alias(
    request: Request,
    db: DBSession,
    location_id: str | None = None,
):
    return market_page(location_id, request, db) if location_id else market_overview(request, db)


@router.get("/markets/{location_id}", response_class=HTMLResponse)
def market_page(location_id: str, request: Request, db: DBSession):
    actor = _page_actor(request, db)
    if not actor:
        return _login_redirect(request)
    view = market_view(
        db,
        organization_id=actor.organization_id,
        location_id=location_id,
    )
    if view is None:
        return HTMLResponse("Location not found", status_code=404)
    return _render(
        "markets/summary.html",
        request,
        actor,
        location_id=location_id,
        **view,
    )


@router.get("/market-overview", response_class=HTMLResponse)
def market_overview(request: Request, db: DBSession):
    actor = _page_actor(request, db)
    if not actor:
        return _login_redirect(request)
    location_id = db.scalar(
        select(Project.location_id)
        .where(
            Project.organization_id == actor.organization_id,
            Project.location_id.is_not(None),
            Project.deleted_at.is_(None),
        )
        .limit(1)
    )
    return (
        RedirectResponse(f"/markets/{location_id}", status_code=303)
        if location_id
        else RedirectResponse("/dashboard", status_code=303)
    )


@router.get("/digest", response_class=HTMLResponse)
def digest_page(request: Request, db: DBSession):
    actor = _page_actor(request, db)
    if not actor:
        return _login_redirect(request)
    view = weekly_digest_view(db, organization_id=actor.organization_id)
    return _render("digest.html", request, actor, items=view["highlights"], **view)


@router.get("/reports", response_class=HTMLResponse)
def reports_page(request: Request, db: DBSession, page: int = 1):
    actor = _page_actor(request, db)
    if not actor:
        return _login_redirect(request)
    view = reports_view(db, organization_id=actor.organization_id, page=page)
    return _render(
        "reports/list.html",
        request,
        actor,
        items=view["reports"],
        report_idempotency_key=(
            f"report-{actor.user_id}-{int(datetime.now(UTC).timestamp())}"
        ),
        **view,
    )


@router.get("/reports/{report_id}", response_class=HTMLResponse)
def report_detail_alias(report_id: str, request: Request, db: DBSession):
    actor = _page_actor(request, db)
    if not actor:
        return _login_redirect(request)
    report = db.scalar(
        select(Report).where(
            Report.id == report_id,
            Report.organization_id == actor.organization_id,
            Report.deleted_at.is_(None),
        )
    )
    if report is None:
        return HTMLResponse("Report not found", status_code=404)
    return RedirectResponse("/reports", status_code=303)


@router.get("/sources/health", response_class=HTMLResponse)
def source_health_page(request: Request, db: DBSession, page: int = 1):
    actor = _page_actor(request, db)
    if not actor:
        return _login_redirect(request)
    view = source_health_view(
        db,
        organization_id=actor.organization_id,
        page=page,
    )
    return _render("sources/health.html", request, actor, items=view["sources"], **view)


@router.get("/organization/members", response_class=HTMLResponse)
def members_page(request: Request, db: DBSession):
    actor = _page_actor(request, db)
    if not actor:
        return _login_redirect(request)
    member_rows = list(
        db.execute(
            select(OrganizationMembership, User)
            .join(User, User.id == OrganizationMembership.user_id)
            .where(OrganizationMembership.organization_id == actor.organization_id)
        )
    )
    capability_by_role = {
        "admin": "Members, policies, deletion, and final reports",
        "reviewer": "Review decisions and permitted report approval",
        "analyst": "Watchlists, monitoring, documents, and report drafts",
        "viewer": "Read-only organization access",
    }
    members = [
        {
            "id": membership.id,
            "membership_id": membership.id,
            "display_name": user.display_name,
            "email": user.email,
            "role": membership.role,
            "capability_summary": capability_by_role.get(membership.role, "Read-only access"),
            "status": "Active" if membership.is_active and user.is_active else "Inactive",
            "joined_at": membership.created_at,
            "is_self": user.id == actor.user_id,
        }
        for membership, user in member_rows
    ]
    organization = db.get(Organization, actor.organization_id)
    return _render(
        "organizations/members.html",
        request,
        actor,
        organization=organization,
        members=members,
        items=members,
        can_manage_members=actor.membership.role == "admin",
    )


@router.get("/organizations/members", response_class=HTMLResponse)
def members_page_alias(request: Request, db: DBSession):
    return members_page(request, db)


@router.get("/audit", response_class=HTMLResponse)
def audit_page(request: Request, db: DBSession):
    actor = _page_actor(request, db)
    if not actor:
        return _login_redirect(request)
    view = audit_history_view(
        db,
        organization_id=actor.organization_id,
        q=request.query_params.get("q"),
        category=request.query_params.get("category"),
        date_from=request.query_params.get("from"),
        date_to=request.query_params.get("to"),
        event_id=request.query_params.get("event_id"),
    )
    if request.query_params.get("event_id") and not view["events"]:
        return HTMLResponse("Audit event not found", status_code=404)
    return _render("audit/list.html", request, actor, **view)


@router.get("/settings/retention", response_class=HTMLResponse)
def retention_page(request: Request, db: DBSession):
    actor = _page_actor(request, db)
    if not actor:
        return _login_redirect(request)
    organization = db.get(Organization, actor.organization_id)
    if organization is None:
        return HTMLResponse("Organization not found", status_code=404)
    upload_count = int(
        db.scalar(
            select(func.count())
            .select_from(Document)
            .where(
                Document.organization_id == actor.organization_id,
                Document.deleted_at.is_(None),
            )
        )
        or 0
    )
    report_count = int(
        db.scalar(
            select(func.count())
            .select_from(Report)
            .where(
                Report.organization_id == actor.organization_id,
                Report.deleted_at.is_(None),
            )
        )
        or 0
    )
    next_expiry = db.scalar(
        select(func.min(Report.expires_at)).where(
            Report.organization_id == actor.organization_id,
            Report.deleted_at.is_(None),
            Report.expires_at.is_not(None),
        )
    )
    return _render(
        "settings/retention.html",
        request,
        actor,
        organization=organization,
        retention_settings={
            "upload_days": organization.retention_upload_days,
            "report_days": organization.retention_report_days,
            "evidence_mode": "Controlled by source policy",
            "tombstone_mode": "minimal",
            "reviewed_at": organization.updated_at,
            "state": "verified" if organization.is_active else "deleted",
        },
        artifact_inventory={
            "upload_count": upload_count,
            "report_count": report_count,
            "next_expiry": next_expiry,
            "pending_deletions": 0,
        },
        can_manage_retention=actor.role == "admin",
        organization_deleted=not organization.is_active,
    )
