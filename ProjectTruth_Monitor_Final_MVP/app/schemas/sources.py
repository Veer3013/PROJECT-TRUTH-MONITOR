from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from app.schemas.common import ORMModel


class ProjectSourceCreate(BaseModel):
    source_type: Literal["hrera_gurugram", "fixture", "developer_page", "uploaded_document"]
    name: str = Field(min_length=1, max_length=200)
    url: HttpUrl | None = None
    fixture_key: str | None = Field(default=None, max_length=200)


class SourceResponse(ORMModel):
    id: str
    organization_id: str
    source_type: str
    name: str
    source_class: str
    base_url: str | None = None
    domain: str
    is_enabled: bool
    last_successful_fetch_at: datetime | None


class SourcePolicyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_name: str = Field(min_length=1, max_length=200)
    source_type: str = Field(min_length=1, max_length=80)
    base_url: HttpUrl
    enabled: bool = False
    permitted_access_modes: list[str] = Field(default_factory=lambda: ["httpx"])
    rate_limit_seconds: float = Field(default=3.0, ge=0.5, le=60.0)
    robots_review_status: str = "pending"
    terms_review_status: str = "pending"
    evidence_retention_days: int = Field(default=90, ge=1, le=3_650)
    raw_content_allowed: bool = False
    playwright_allowed: bool = False
    ocr_allowed: bool = False
    llm_allowed: bool = False
    policy_version: str = "1.0"


class SourcePolicyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    permitted_access_modes: list[str] | None = None
    rate_limit_seconds: float | None = Field(default=None, ge=0.5, le=60.0)
    robots_review_status: str | None = None
    terms_review_status: str | None = None
    evidence_retention_days: int | None = Field(default=None, ge=1, le=3_650)
    raw_content_allowed: bool | None = None
    playwright_allowed: bool | None = None
    ocr_allowed: bool | None = None
    llm_allowed: bool | None = None
    policy_version: str | None = None


class SourcePolicyResponse(ORMModel):
    id: str
    source_id: str
    is_enabled: bool
    permitted_access_modes: list[str]
    rate_limit_requests: int
    rate_limit_period_seconds: float
    robots_review_status: str
    terms_review_status: str
    evidence_retention_days: int
    raw_content_may_be_stored: bool
    playwright_allowed: bool
    ocr_allowed: bool
    llm_allowed: bool
    policy_version: str
    reviewed_at: datetime | None


class RefreshRequest(BaseModel):
    idempotency_key: str = Field(min_length=8, max_length=200)
    fixture_key: str | None = Field(default=None, max_length=200)
    source_id: str | None = Field(default=None, min_length=36, max_length=36)


class MonitoringRunResponse(ORMModel):
    id: str
    organization_id: str
    project_id: str | None
    source_id: str
    status: str
    access_completeness: str | None
    error_category: str | None
    started_at: datetime | None
    completed_at: datetime | None


class SourceHealthResponse(BaseModel):
    source_id: str
    source_name: str
    last_successful_fetch: datetime | None
    fetch_success_rate: float
    parser_success_rate: float
    average_latency_ms: float
    consecutive_failures: int
    circuit_breaker_status: str
    parser_version: str | None
    policy_version: str | None
    jobs_awaiting_retry: int
