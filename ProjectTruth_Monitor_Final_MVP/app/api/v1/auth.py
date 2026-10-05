from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request, Response, status
from sqlalchemy import select

from app.api.deps import CSRFProtected, CurrentActor, DBSession
from app.core.config import get_settings
from app.core.rate_limit import rate_limiter
from app.core.security import (
    create_session_token,
    csrf_token_for_session,
    verify_password,
)
from app.models import AuditEvent, Organization, OrganizationMembership, User
from app.schemas.auth import LoginRequest, MeResponse, SessionResponse

router = APIRouter(prefix="/auth", tags=["Authentication"])


@router.post("/login", response_model=SessionResponse)
def login(
    payload: LoginRequest, request: Request, response: Response, db: DBSession
) -> SessionResponse:
    settings = get_settings()
    client_key = request.client.host if request.client else "unknown"
    rate_limiter.check(f"login:{client_key}", limit=settings.login_rate_limit)
    email, password = str(payload.email).lower(), payload.password
    user = db.scalar(select(User).where(User.email == email))
    if user is None or not user.is_active or not verify_password(password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "invalid_credentials", "message": "Email or password is incorrect."},
        )
    membership = db.scalar(
        select(OrganizationMembership)
        .join(Organization, Organization.id == OrganizationMembership.organization_id)
        .where(
            OrganizationMembership.user_id == user.id,
            OrganizationMembership.is_active.is_(True),
            Organization.is_active.is_(True),
        )
        .order_by(OrganizationMembership.created_at)
    )
    if membership is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "no_membership", "message": "No active organization access was found."},
        )
    token = create_session_token(user.id, settings)
    csrf = csrf_token_for_session(token, settings)
    user.last_login_at = datetime.now(UTC)
    db.add(
        AuditEvent(
            organization_id=membership.organization_id,
            actor_user_id=user.id,
            action="authentication.login",
            resource_type="user",
            resource_id=user.id,
            request_id=request.state.request_id,
            metadata_json={"result": "success"},
        )
    )
    db.commit()
    response.set_cookie(
        settings.session_cookie_name,
        token,
        max_age=settings.session_max_age_seconds,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        path="/",
    )
    return SessionResponse(token=token, csrf_token=csrf, user=user, membership=membership)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    request: Request,
    response: Response,
    db: DBSession,
    actor: CurrentActor,
    csrf: CSRFProtected,
) -> Response:
    del csrf
    settings = get_settings()
    db.add(
        AuditEvent(
            organization_id=actor.organization_id,
            actor_user_id=actor.user_id,
            action="authentication.logout",
            resource_type="user",
            resource_id=actor.user_id,
            request_id=request.state.request_id,
            metadata_json={},
        )
    )
    db.commit()
    response.delete_cookie(settings.session_cookie_name, path="/")
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.get("/me", response_model=MeResponse)
def me(actor: CurrentActor) -> MeResponse:
    return MeResponse(user=actor.user, membership=actor.membership)
