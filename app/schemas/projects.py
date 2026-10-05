from __future__ import annotations

from datetime import date, datetime

from pydantic import AliasChoices, BaseModel, Field, HttpUrl, field_validator

from app.schemas.common import ORMModel


class ProjectCreate(BaseModel):
    registration_number: str | None = Field(default=None, max_length=100)
    name: str = Field(min_length=1, max_length=240)
    developer_name: str | None = Field(default=None, max_length=240)
    city: str = Field(default="Gurugram", max_length=120)
    locality_or_sector: str | None = Field(default=None, max_length=160)
    official_url: HttpUrl | None = None
    parent_project_id: str | None = None
    phase_name: str | None = Field(default=None, max_length=120)

    @field_validator("registration_number")
    @classmethod
    def normalize_registration(cls, value: str | None) -> str | None:
        return value.strip().upper() if value and value.strip() else None


class ProjectResponse(ORMModel):
    id: str
    organization_id: str
    registration_number: str | None
    name: str
    identity_status: str
    is_provisional: bool
    parent_project_id: str | None
    merged_into_project_id: str | None
    phase_name: str | None
    current_status: str | None = None
    proposed_completion_date: date | None = None
    revised_completion_date: date | None = None
    created_at: datetime


class PassportResponse(BaseModel):
    project: ProjectResponse
    official_source_count: int
    marketing_source_count: int
    last_successful_verification: datetime | None
    monitoring_coverage_status: str
    open_alerts: int
    unresolved_conflicts: int
    history_tracking_start: datetime
    data_quality: dict[str, str]
    latest_reviewer_decision: str | None


class TimelineEventResponse(BaseModel):
    id: str
    event_type: str
    title: str
    description: str
    occurred_at: datetime
    evidence_id: str | None = None
    audit_event_id: str | None = None


class ChangeResponse(ORMModel):
    id: str
    project_id: str
    field_key: str
    old_snapshot_id: str | None
    new_snapshot_id: str
    old_raw_value: str | None
    old_canonical_value: str | None
    new_raw_value: str | None
    new_canonical_value: str | None
    change_type: str
    magnitude: float | None
    severity: str
    requires_review: bool
    review_state: str
    detected_at: datetime


class SignalResponse(ORMModel):
    id: str
    project_id: str
    rule_code: str
    rule_version: str
    severity: str
    confidence: float | None
    limitations: list[str]
    required_review_level: str
    detected_at: datetime


class ConflictResponse(ORMModel):
    id: str
    project_id: str
    field_key: str
    state: str
    source_a_class: str
    source_b_class: str
    source_a_value: str | None
    source_b_value: str | None
    created_at: datetime


class CorrectionCreate(BaseModel):
    project_id: str
    field_key: str = Field(min_length=1, max_length=160)
    corrected_value: str = Field(min_length=1, max_length=2_000)
    reason: str = Field(min_length=3, max_length=2_000)


class CorrectionReview(BaseModel):
    decision: str = Field(pattern="^(confirm|dismiss|keep_unresolved)$")
    note: str | None = Field(default=None, max_length=2_000)


class CorrectionReversal(BaseModel):
    note: str | None = Field(default=None, max_length=2_000)


class MergeReview(BaseModel):
    decision: str = Field(pattern="^(confirm|dismiss|keep_unresolved)$")
    note: str | None = Field(default=None, max_length=2_000)


class MergeReversal(BaseModel):
    note: str | None = Field(default=None, max_length=2_000)


class ConflictReview(BaseModel):
    decision: str = Field(
        pattern=(
            "^(select_official|select_other|prefer_official_display|confirm_correction|"
            "request_another_source|keep_unresolved)$"
        )
    )
    reason: str = Field(
        min_length=3,
        max_length=2_000,
        validation_alias=AliasChoices("reason", "note"),
    )
