from __future__ import annotations

import secrets

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import func, select

from app.api.deps import AdminActor, CSRFProtected, CurrentActor, DBSession, DeletionAdminActor
from app.api.utils import add_audit_event, commit_or_rollback
from app.core.config import get_settings
from app.core.security import hash_password
from app.models import Organization, OrganizationMembership, User
from app.schemas.organizations import (
    MemberCreate,
    MemberResponse,
    MemberUpdate,
    OrganizationDeletionRequest,
    OrganizationDeletionResponse,
    OrganizationResponse,
    RetentionUpdate,
)
from app.services.deletion import PersistentOrganizationDeletionService

router = APIRouter(prefix="/organizations", tags=["Organizations"])


@router.get("/current", response_model=OrganizationResponse)
def current_organization(db: DBSession, actor: CurrentActor) -> Organization:
    organization = db.get(Organization, actor.organization_id)
    if organization is None:
        raise HTTPException(status_code=404, detail={"code": "not_found"})
    return organization


@router.get("/current/members", response_model=list[MemberResponse])
def list_members(db: DBSession, actor: CurrentActor) -> list[OrganizationMembership]:
    return list(
        db.scalars(
            select(OrganizationMembership)
            .where(OrganizationMembership.organization_id == actor.organization_id)
            .order_by(OrganizationMembership.created_at)
        )
    )


@router.post(
    "/current/members", response_model=MemberResponse, status_code=status.HTTP_201_CREATED
)
def create_member(
    payload: MemberCreate,
    request: Request,
    db: DBSession,
    actor: AdminActor,
    csrf: CSRFProtected,
) -> OrganizationMembership:
    del csrf
    email = str(payload.email).lower()
    user = db.scalar(select(User).where(User.email == email))
    if user is None:
        user = User(
            email=email,
            display_name=payload.display_name.strip(),
            password_hash=hash_password(payload.temporary_password),
            is_active=True,
        )
        db.add(user)
        db.flush()
    existing = db.scalar(
        select(OrganizationMembership).where(
            OrganizationMembership.organization_id == actor.organization_id,
            OrganizationMembership.user_id == user.id,
        )
    )
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail={"code": "membership_exists", "message": "This user is already a member."},
        )
    membership = OrganizationMembership(
        organization_id=actor.organization_id,
        user_id=user.id,
        role=payload.role.value,
        is_active=True,
    )
    db.add(membership)
    db.flush()
    add_audit_event(
        db,
        actor=actor,
        action="organization.member_created",
        resource_type="organization_membership",
        resource_id=membership.id,
        request=request,
        metadata={"role": payload.role.value},
    )
    commit_or_rollback(db)
    db.refresh(membership)
    return membership


@router.patch("/current/retention", response_model=OrganizationResponse)
def update_retention(
    payload: RetentionUpdate,
    request: Request,
    db: DBSession,
    actor: AdminActor,
    csrf: CSRFProtected,
) -> Organization:
    del csrf
    organization = db.get(Organization, actor.organization_id)
    if organization is None:
        raise HTTPException(status_code=404, detail={"code": "organization_not_found"})
    before = {
        "retention_upload_days": organization.retention_upload_days,
        "retention_report_days": organization.retention_report_days,
    }
    organization.retention_upload_days = payload.retention_upload_days
    organization.retention_report_days = payload.retention_report_days
    add_audit_event(
        db,
        actor=actor,
        action="organization.retention_updated",
        resource_type="organization",
        resource_id=organization.id,
        request=request,
        metadata={"before": before, "after": payload.model_dump()},
    )
    commit_or_rollback(db)
    db.refresh(organization)
    return organization


@router.post("/current/deletion", response_model=OrganizationDeletionResponse)
def delete_current_organization_data(
    payload: OrganizationDeletionRequest,
    request: Request,
    db: DBSession,
    actor: DeletionAdminActor,
    csrf: CSRFProtected,
) -> OrganizationDeletionResponse:
    del csrf
    organization = db.get(Organization, actor.organization_id)
    if organization is None:
        raise HTTPException(status_code=404, detail={"code": "organization_not_found"})
    if not secrets.compare_digest(payload.confirmation, organization.name):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "organization_confirmation_mismatch",
                "message": "Type the organization name exactly to confirm deletion.",
            },
        )

    settings = get_settings()
    service = PersistentOrganizationDeletionService(
        db,
        upload_root=settings.upload_dir,
        report_root=settings.report_dir,
    )
    result = service.delete(
        organization=organization,
        requested_by_user_id=actor.user_id,
        request_id=getattr(request.state, "request_id", None),
    )
    if not result.already_deleted:
        add_audit_event(
            db,
            actor=actor,
            action="organization.data_deleted",
            resource_type="organization",
            resource_id=organization.id,
            request=request,
            metadata={
                **result.audit_metadata(),
                "reason": payload.reason,
                "tombstone_id": result.tombstone.id,
            },
        )
        commit_or_rollback(db)
        db.refresh(result.tombstone)

    return OrganizationDeletionResponse(
        organization_id=organization.id,
        tombstone_id=result.tombstone.id,
        already_deleted=result.already_deleted,
        monitoring_jobs_cancelled=result.monitoring_jobs_cancelled,
        monitoring_runs_cancelled=result.monitoring_runs_cancelled,
        projects_deleted=result.projects_deleted,
        watchlists_deleted=result.watchlists_deleted,
        documents_deleted=result.documents_deleted,
        reports_deleted=result.reports_deleted,
        sources_disabled=result.sources_disabled,
        artifacts_removed=result.artifacts_removed,
        deleted_at=result.tombstone.deleted_at,
    )


@router.patch("/current/members/{membership_id}", response_model=MemberResponse)
def update_member(
    membership_id: str,
    payload: MemberUpdate,
    request: Request,
    db: DBSession,
    actor: AdminActor,
    csrf: CSRFProtected,
) -> OrganizationMembership:
    del csrf
    membership = db.scalar(
        select(OrganizationMembership).where(
            OrganizationMembership.id == membership_id,
            OrganizationMembership.organization_id == actor.organization_id,
        )
    )
    if membership is None:
        raise HTTPException(status_code=404, detail={"code": "not_found"})
    data = payload.model_dump(exclude_unset=True)
    new_role = data.get("role")
    new_active = data.get("is_active")
    removing_admin = membership.role == "admin" and (
        (new_role is not None and str(new_role) != "admin") or new_active is False
    )
    if removing_admin:
        admin_count = db.scalar(
            select(func.count())
            .select_from(OrganizationMembership)
            .where(
                OrganizationMembership.organization_id == actor.organization_id,
                OrganizationMembership.role == "admin",
                OrganizationMembership.is_active.is_(True),
            )
        )
        if int(admin_count or 0) <= 1:
            raise HTTPException(
                status_code=409,
                detail={"code": "last_admin", "message": "The organization needs an active admin."},
            )
    if new_role is not None:
        membership.role = new_role.value if hasattr(new_role, "value") else str(new_role)
    if new_active is not None:
        membership.is_active = new_active
    add_audit_event(
        db,
        actor=actor,
        action="organization.member_updated",
        resource_type="organization_membership",
        resource_id=membership.id,
        request=request,
        metadata={"changes": payload.model_dump(mode="json", exclude_unset=True)},
    )
    commit_or_rollback(db)
    db.refresh(membership)
    return membership
