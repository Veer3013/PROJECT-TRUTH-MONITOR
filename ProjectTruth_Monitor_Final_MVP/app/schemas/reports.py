from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel


class ReportCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    report_type: str = Field(default="weekly_digest", max_length=80)
    project_ids: list[str] = Field(default_factory=list, max_length=200)
    formats: list[str] = Field(default_factory=lambda: ["pdf", "csv", "json", "evidence"])
    idempotency_key: str = Field(min_length=8, max_length=200)


class ReportResponse(ORMModel):
    id: str
    organization_id: str
    title: str
    report_type: str
    state: str
    created_at: datetime
    approved_at: datetime | None
    expires_at: datetime | None


class ReportApprove(BaseModel):
    note: str | None = Field(default=None, max_length=2_000)


class MarketSummary(BaseModel):
    location_id: str
    projects_monitored: int
    active_projects: int
    lapsed_projects: int
    qpr_flagged_projects: int
    recent_reviewed_completion_changes: int
    new_registrations_or_phases: int
    developer_count: int
    source_health: dict[str, int]
    disclaimer: str = "Based only on projects in this monitored dataset - not the complete Gurugram market."
