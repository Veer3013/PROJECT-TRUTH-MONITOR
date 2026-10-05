from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.permissions import Role, enforce_role
from app.core.security import csrf_token_matches, read_session_token
from app.db import get_db
from app.models import Organization, OrganizationMembership, User

bearer_scheme = HTTPBearer(auto_error=False)
DBSession = Annotated[Session, Depends(get_db)]


@dataclass(frozen=True)
class ActorContext:
    user: User
    membership: OrganizationMembership

    @property
    def user_id(self) -> str:
        return self.user.id

    @property
    def organization_id(self) -> str:
        return self.membership.organization_id

    @property
    def role(self) -> str:
        return self.membership.role


def _authentication_error() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"code": "not_authenticated", "message": "Please sign in to continue."},
        headers={"WWW-Authenticate": "Bearer"},
    )


def _extract_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None,
    settings: Settings,
) -> str | None:
    if credentials and credentials.scheme.lower() == "bearer":
        return credentials.credentials
    return request.cookies.get(settings.session_cookie_name)


def _resolve_actor(
    *,
    db: Session,
    request: Request,
    credentials: HTTPAuthorizationCredentials | None,
    x_organization_id: str | None,
    require_active_organization: bool,
) -> ActorContext:
    settings = get_settings()
    token = _extract_token(request, credentials, settings)
    if not token:
        raise _authentication_error()
    identity = read_session_token(token, settings)
    if identity is None:
        raise _authentication_error()

    user = db.scalar(select(User).where(User.id == identity.user_id, User.is_active.is_(True)))
    if user is None:
        raise _authentication_error()

    membership_query = select(OrganizationMembership).where(
        OrganizationMembership.user_id == user.id,
        OrganizationMembership.is_active.is_(True),
    )
    if require_active_organization:
        membership_query = membership_query.join(
            Organization, Organization.id == OrganizationMembership.organization_id
        ).where(Organization.is_active.is_(True))
    if x_organization_id:
        membership_query = membership_query.where(
            OrganizationMembership.organization_id == x_organization_id
        )
    membership_query = membership_query.order_by(OrganizationMembership.created_at)
    membership = db.scalar(membership_query)
    if membership is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "no_membership", "message": "No active organization access was found."},
        )
    return ActorContext(user=user, membership=membership)


async def get_current_actor(
    request: Request,
    db: DBSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    x_organization_id: Annotated[str | None, Header()] = None,
) -> ActorContext:
    return _resolve_actor(
        db=db,
        request=request,
        credentials=credentials,
        x_organization_id=x_organization_id,
        require_active_organization=True,
    )


async def get_deletion_actor(
    request: Request,
    db: DBSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    x_organization_id: Annotated[str | None, Header()] = None,
) -> ActorContext:
    """Authenticate an existing membership solely for idempotent deletion retries."""

    actor = _resolve_actor(
        db=db,
        request=request,
        credentials=credentials,
        x_organization_id=x_organization_id,
        require_active_organization=False,
    )
    enforce_role(actor.role, Role.ADMIN)
    return actor


CurrentActor = Annotated[ActorContext, Depends(get_current_actor)]


def require_role(minimum: Role) -> Callable[[ActorContext], ActorContext]:
    async def dependency(actor: CurrentActor) -> ActorContext:
        enforce_role(actor.role, minimum)
        return actor

    return dependency


AdminActor = Annotated[ActorContext, Depends(require_role(Role.ADMIN))]
ReviewerActor = Annotated[ActorContext, Depends(require_role(Role.REVIEWER))]
AnalystActor = Annotated[ActorContext, Depends(require_role(Role.ANALYST))]
DeletionAdminActor = Annotated[ActorContext, Depends(get_deletion_actor)]


async def require_csrf(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> None:
    """Protect cookie-authenticated mutations; bearer requests are not CSRF-prone."""

    if credentials and credentials.scheme.lower() == "bearer":
        return
    settings = get_settings()
    session_token = request.cookies.get(settings.session_cookie_name)
    if not session_token:
        raise _authentication_error()
    submitted = request.headers.get("X-CSRF-Token", "")
    content_type = request.headers.get("content-type", "")
    if not submitted and (
        content_type.startswith("application/x-www-form-urlencoded")
        or content_type.startswith("multipart/form-data")
    ):
        form = await request.form()
        submitted = str(form.get("csrf_token", ""))
    if not submitted or not csrf_token_matches(session_token, submitted, settings):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "csrf_failed", "message": "The form security token is invalid."},
        )


CSRFProtected = Annotated[None, Depends(require_csrf)]
