from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from app.schemas.common import ORMModel


class DocumentResponse(ORMModel):
    id: str
    organization_id: str
    project_id: str
    original_filename: str
    sha256: str
    status: str
    created_at: datetime


class DocumentVersionResponse(ORMModel):
    id: str
    document_id: str
    version_number: int
    content_hash: str
    file_size_bytes: int
    malware_scan_status: str
    captured_at: datetime


class ExtractRequest(BaseModel):
    use_ocr_if_needed: bool = True


class ClaimResponse(ORMModel):
    id: str
    document_version_id: str
    field_key: str
    raw_value: str
    normalized_value: str | None
    page_number: int | None
    evidence_text: str | None
    extraction_method: str
    extraction_confidence: float | None


class ComparisonResponse(BaseModel):
    project_id: str
    document_id: str
    results: list[dict[str, object]]
    needs_review_count: int
