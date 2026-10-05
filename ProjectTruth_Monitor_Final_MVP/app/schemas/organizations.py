from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.core.permissions import Role
from app.schemas.common import ORMModel


class OrganizationResponse(ORMModel):
    id: str
    name: str
    slug: str
    is_active: bool
    retention_upload_days: int
    retention_report_days: int
    created_at: datetime


class MemberCreate(BaseModel):
    email: EmailStr
    display_name: str = Field(min_length=1, max_length=120)
    role: Role
    temporary_password: str = Field(min_length=10, max_length=200)


class MemberUpdate(BaseModel):
    role: Role | None = None
    is_active: bool | None = None

    @field_validator("role")
    @classmethod
    def valid_role(cls, value: Role | None) -> Role | None:
        return value


class MemberResponse(ORMModel):
    id: str
    user_id: str
    organization_id: str
    role: str
    is_active: bool


class RetentionUpdate(BaseModel):
    retention_upload_days: int = Field(ge=1, le=3_650)
    retention_report_days: int = Field(ge=1, le=3_650)


class OrganizationDeletionRequest(BaseModel):
    confirmation: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=1_000)

    @field_validator("confirmation", "reason")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("This field is required.")
        return cleaned


class OrganizationDeletionResponse(BaseModel):
    organization_id: str
    tombstone_id: str
    deleted: bool = True
    already_deleted: bool
    monitoring_jobs_cancelled: int
    monitoring_runs_cancelled: int
    projects_deleted: int
    watchlists_deleted: int
    documents_deleted: int
    reports_deleted: int
    sources_disabled: int
    artifacts_removed: int
    deleted_at: datetime
