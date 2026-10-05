from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    Document,
    DocumentClaim,
    DocumentExtraction,
    DocumentVersion,
    Project,
    SnapshotField,
    Source,
    SourceSnapshot,
)


def _display_timestamp(value: datetime | None) -> str:
    if value is None:
        return "Not recorded"
    aware = value if value.tzinfo else value.replace(tzinfo=UTC)
    return aware.astimezone(UTC).strftime("%d %b %Y, %H:%M UTC")


def _latest_version(
    db: Session, *, organization_id: str, document_id: str
) -> DocumentVersion | None:
    return db.scalar(
        select(DocumentVersion)
        .where(
            DocumentVersion.organization_id == organization_id,
            DocumentVersion.document_id == document_id,
        )
        .order_by(DocumentVersion.version_number.desc())
    )


def _latest_extraction(
    db: Session, *, organization_id: str, version_id: str
) -> DocumentExtraction | None:
    return db.scalar(
        select(DocumentExtraction)
        .where(
            DocumentExtraction.organization_id == organization_id,
            DocumentExtraction.document_version_id == version_id,
        )
        .order_by(DocumentExtraction.created_at.desc())
    )


def project_documents_view(
    db: Session,
    *,
    organization_id: str,
    project: Project,
    selected_document_id: str | None = None,
) -> dict[str, Any]:
    document_rows = list(
        db.scalars(
            select(Document)
            .where(
                Document.organization_id == organization_id,
                Document.project_id == project.id,
                Document.deleted_at.is_(None),
            )
            .order_by(Document.created_at.desc())
        )
    )
    documents: list[dict[str, Any]] = []
    selected_document: dict[str, Any] | None = None
    selected_version: DocumentVersion | None = None
    selected_extraction: DocumentExtraction | None = None
    for document in document_rows:
        version = _latest_version(
            db, organization_id=organization_id, document_id=document.id
        )
        extraction = (
            _latest_extraction(
                db, organization_id=organization_id, version_id=version.id
            )
            if version
            else None
        )
        claim_count = (
            int(
                db.scalar(
                    select(func.count())
                    .select_from(DocumentClaim)
                    .where(
                        DocumentClaim.organization_id == organization_id,
                        DocumentClaim.document_version_id == version.id,
                    )
                )
                or 0
            )
            if version
            else 0
        )
        extraction_state = (
            "Complete"
            if extraction and extraction.status == "completed"
            else "Needs review"
            if extraction and extraction.status in {"needs_review", "failed", "blocked"}
            else "Pending"
        )
        item = {
            "id": document.id,
            "label": document.title,
            "safe_filename": document.sanitized_filename,
            "version": version.version_number if version else 0,
            "version_id": version.id if version else None,
            "content_hash": version.content_hash if version else document.sha256,
            "malware_scan_status": (
                version.malware_scan_status if version else "not_recorded"
            ),
            "extraction_state": extraction_state,
            "extraction_method": (
                extraction.extraction_method if extraction else "Not processed"
            ),
            "extraction_warnings": list(extraction.warnings) if extraction else [],
            "claim_count": claim_count,
            "uploaded_at": _display_timestamp(
                version.captured_at if version else document.created_at
            ),
            "status": document.status,
        }
        documents.append(item)
        if selected_document_id == document.id:
            selected_document = item
            selected_version = version
            selected_extraction = extraction

    versions: list[dict[str, Any]] = []
    comparisons: list[dict[str, Any]] = []
    if selected_document and selected_version:
        versions = [
            {
                "id": version.id,
                "version": version.version_number,
                "content_hash": version.content_hash,
                "uploaded_at": _display_timestamp(version.captured_at),
                "malware_scan_status": version.malware_scan_status,
            }
            for version in db.scalars(
                select(DocumentVersion)
                .where(
                    DocumentVersion.organization_id == organization_id,
                    DocumentVersion.document_id == selected_document["id"],
                )
                .order_by(DocumentVersion.version_number.desc())
            )
        ]
        latest_snapshot = db.scalar(
            select(SourceSnapshot)
            .join(Source, Source.id == SourceSnapshot.source_id)
            .where(
                SourceSnapshot.organization_id == organization_id,
                SourceSnapshot.project_id == project.id,
                SourceSnapshot.validation_status.in_(["valid", "partial"]),
                Source.organization_id == organization_id,
                Source.source_class == "official_regulator",
            )
            .order_by(SourceSnapshot.fetched_at.desc())
        )
        official_fields = (
            {
                field.field_key: field
                for field in db.scalars(
                    select(SnapshotField).where(
                        SnapshotField.organization_id == organization_id,
                        SnapshotField.snapshot_id == latest_snapshot.id,
                    )
                )
            }
            if latest_snapshot
            else {}
        )
        official_source = (
            db.scalar(
                select(Source).where(
                    Source.id == latest_snapshot.source_id,
                    Source.organization_id == organization_id,
                )
            )
            if latest_snapshot
            else None
        )
        for claim in db.scalars(
            select(DocumentClaim)
            .where(
                DocumentClaim.organization_id == organization_id,
                DocumentClaim.document_version_id == selected_version.id,
            )
            .order_by(DocumentClaim.field_key)
        ):
            official = official_fields.get(claim.field_key)
            comparisons.append(
                {
                    "id": claim.id,
                    "field_key": claim.field_key,
                    "field_label": claim.field_key.replace("_", " ").title(),
                    "marketing_value": claim.normalized_value or claim.raw_value,
                    "official_value": (
                        official.canonical_value if official else "Not present"
                    ),
                    "official_source": (
                        official_source.name
                        if official_source
                        else "No captured official record"
                    ),
                    "state": claim.comparison_state.upper(),
                    "page": claim.page_number,
                    "evidence_text": claim.evidence_text,
                    "coordinates": claim.coordinates,
                    "extraction_method": claim.extraction_method,
                    "extraction_confidence": claim.extraction_confidence,
                }
            )

    latest_item = documents[0] if documents else None
    return {
        "documents": documents,
        "selected_document": selected_document,
        "selected_document_versions": versions,
        "selected_extraction": (
            {
                "status": selected_extraction.status,
                "method": selected_extraction.extraction_method,
                "warnings": list(selected_extraction.warnings),
                "requires_review": selected_extraction.requires_review,
            }
            if selected_extraction
            else None
        ),
        "comparisons": comparisons,
        "comparison_summary": (
            f"{sum(item['state'] in {'CONFLICTING', 'NEEDS_REVIEW'} for item in comparisons)} "
            "need review"
            if comparisons
            else "Not compared"
        ),
        "document_source_name": "User-authorized documents",
        "document_freshness": (
            latest_item["uploaded_at"] if latest_item else "No document processed"
        ),
        "document_state": (
            "verified"
            if latest_item and latest_item["extraction_state"] == "Complete"
            else "needs_review"
            if latest_item
            else "unverified"
        ),
    }


__all__ = ["project_documents_view"]
