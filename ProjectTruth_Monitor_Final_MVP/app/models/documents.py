from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship, synonym

from app.db import Base, UTCDateTime, utcnow
from app.models.base import (
    CreatedAtMixin,
    OrganizationOwnedMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    new_uuid,
)
from app.models.enums import ComparisonState


class Document(UUIDPrimaryKeyMixin, OrganizationOwnedMixin, TimestampMixin, Base):
    __tablename__ = "documents"

    project_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="SET NULL"), index=True
    )
    source_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("sources.id", ondelete="SET NULL"), index=True
    )
    document_type: Mapped[str] = mapped_column(String(64), default="brochure", nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    sanitized_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(255), nullable=False)
    authorization_attested: Mapped[bool] = mapped_column(Boolean, nullable=False)
    rights_attestation_text: Mapped[str | None] = mapped_column(Text)
    attested_by_user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_by_user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), default="uploaded", nullable=False)
    storage_key: Mapped[str | None] = mapped_column(String(1024))
    sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    rights_attested = synonym("authorization_attested")

    versions: Mapped[list[DocumentVersion]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint(
            "size_bytes IS NULL OR size_bytes >= 0", name="document_size_nonnegative"
        ),
        Index("ix_documents_org_project_status", "organization_id", "project_id", "status"),
    )


class DocumentVersion(UUIDPrimaryKeyMixin, OrganizationOwnedMixin, CreatedAtMixin, Base):
    __tablename__ = "document_versions"

    document_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    file_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    storage_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    source_url: Mapped[str | None] = mapped_column(String(2048))
    uploaded_by_user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    captured_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utcnow, nullable=False
    )
    is_encrypted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    malware_scan_status: Mapped[str] = mapped_column(
        String(32), default="not_scanned", nullable=False
    )

    document: Mapped[Document] = relationship(back_populates="versions")

    __table_args__ = (
        UniqueConstraint("document_id", "version_number", name="uq_document_version_number"),
        UniqueConstraint("document_id", "content_hash", name="uq_document_version_hash"),
        CheckConstraint("version_number >= 1", name="document_version_number_positive"),
        CheckConstraint("file_size_bytes >= 0", name="document_version_size_nonnegative"),
        Index("ix_document_versions_org_hash", "organization_id", "content_hash"),
    )


class DocumentExtraction(UUIDPrimaryKeyMixin, OrganizationOwnedMixin, CreatedAtMixin, Base):
    __tablename__ = "document_extractions"

    document_version_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("document_versions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    provider_name: Mapped[str] = mapped_column(String(160), nullable=False)
    provider_version: Mapped[str] = mapped_column(String(64), nullable=False)
    extraction_method: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    text_content: Mapped[str | None] = mapped_column(Text)
    page_count: Mapped[int | None] = mapped_column(Integer)
    confidence: Mapped[float | None] = mapped_column(Float)
    warnings: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    extraction_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON, default=dict, nullable=False
    )
    requires_review: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    deduplication_key: Mapped[str] = mapped_column(
        String(255), default=new_uuid, nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "organization_id", "deduplication_key", name="uq_document_extraction_dedup"
        ),
        CheckConstraint(
            "page_count IS NULL OR page_count >= 0", name="document_extraction_pages_nonnegative"
        ),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="document_extraction_confidence_range",
        ),
        Index(
            "ix_document_extractions_org_status", "organization_id", "status", "requires_review"
        ),
    )


class DocumentClaim(UUIDPrimaryKeyMixin, OrganizationOwnedMixin, CreatedAtMixin, Base):
    __tablename__ = "document_claims"

    document_extraction_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("document_extractions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    document_version_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("document_versions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="SET NULL"), index=True
    )
    official_snapshot_field_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("snapshot_fields.id", ondelete="SET NULL"), index=True
    )
    field_key: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    raw_value: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_value: Mapped[str | None] = mapped_column(Text)
    value_type: Mapped[str] = mapped_column(String(32), default="text", nullable=False)
    unit: Mapped[str | None] = mapped_column(String(64))
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    evidence_text: Mapped[str] = mapped_column(Text, nullable=False)
    coordinates: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    extraction_method: Mapped[str] = mapped_column(String(64), nullable=False)
    extraction_confidence: Mapped[float | None] = mapped_column(Float)
    comparison_state: Mapped[str] = mapped_column(
        String(32), default=ComparisonState.UNVERIFIED.value, nullable=False
    )
    review_state: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)

    __table_args__ = (
        CheckConstraint("page_number >= 1", name="document_claim_page_positive"),
        CheckConstraint(
            "extraction_confidence IS NULL OR "
            "(extraction_confidence >= 0 AND extraction_confidence <= 1)",
            name="document_claim_confidence_range",
        ),
        CheckConstraint(
            "comparison_state IN "
            "('matched', 'conflicting', 'marketing_only', 'official_only', "
            "'unverified', 'needs_review')",
            name="document_claim_comparison_state_valid",
        ),
        Index(
            "ix_document_claims_org_project_state",
            "organization_id",
            "project_id",
            "comparison_state",
        ),
    )


__all__ = ["Document", "DocumentClaim", "DocumentExtraction", "DocumentVersion"]
