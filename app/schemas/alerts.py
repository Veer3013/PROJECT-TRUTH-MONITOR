from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel


class AlertResponse(ORMModel):
    id: str
    project_id: str
    detected_change_id: str | None
    signal_id: str | None
    title: str
    neutral_description: str
    severity: str
    state: str
    requires_review: bool
    suggested_review_action: str | None
    first_seen_at: datetime


class AlertReviewCreate(BaseModel):
    decision: str = Field(
        pattern="^(confirm|dismiss|correct|request_another_source|mark_expected|keep_unresolved)$"
    )
    reason_code: str = Field(min_length=2, max_length=120)
    note: str | None = Field(default=None, max_length=2_000)


class EvidenceResponse(ORMModel):
    id: str
    detected_change_id: str
    snapshot_id: str
    evidence_side: str
    evidence_type: str
    source_class: str
    source_url: str | None
    excerpt: str | None
    selector: str | None
    page_number: int | None
    content_hash: str
    extraction_method: str | None
    created_at: datetime
