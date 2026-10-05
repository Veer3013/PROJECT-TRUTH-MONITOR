from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, synonym

from app.db import Base, UTCDateTime, utcnow
from app.models.base import (
    CreatedAtMixin,
    OrganizationOwnedMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    new_uuid,
)
from app.models.enums import ReportState


class Report(UUIDPrimaryKeyMixin, OrganizationOwnedMixin, TimestampMixin, Base):
    __tablename__ = "reports"

    title: Mapped[str] = mapped_column(String(300), nullable=False)
    report_type: Mapped[str] = mapped_column(String(64), default="weekly_digest", nullable=False)
    state: Mapped[str] = mapped_column(
        String(32), default=ReportState.DRAFT.value, nullable=False, index=True
    )
    period_start: Mapped[date | None] = mapped_column(Date)
    period_end: Mapped[date | None] = mapped_column(Date)
    generated_by_user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    approved_by_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    storage_key: Mapped[str | None] = mapped_column(String(1024))
    mime_type: Mapped[str | None] = mapped_column(String(255))
    sha256: Mapped[str | None] = mapped_column(String(64))
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True)
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    product_disclaimer: Mapped[str] = mapped_column(Text, nullable=False)
    summary_metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    limitations: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    deduplication_key: Mapped[str] = mapped_column(
        String(255), default=new_uuid, nullable=False
    )

    status = synonym("state")

    __table_args__ = (
        UniqueConstraint("organization_id", "deduplication_key", name="uq_report_dedup"),
        CheckConstraint(
            "period_start IS NULL OR period_end IS NULL OR period_end >= period_start",
            name="report_period_valid",
        ),
        CheckConstraint("size_bytes IS NULL OR size_bytes >= 0", name="report_size_nonnegative"),
        Index("ix_reports_org_state_created", "organization_id", "state", "created_at"),
    )


class ReportItem(UUIDPrimaryKeyMixin, OrganizationOwnedMixin, CreatedAtMixin, Base):
    __tablename__ = "report_items"

    report_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("reports.id", ondelete="CASCADE"), nullable=False, index=True
    )
    project_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="SET NULL"), index=True
    )
    alert_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("alerts.id", ondelete="SET NULL"), index=True
    )
    detected_change_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("detected_changes.id", ondelete="SET NULL"), index=True
    )
    signal_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("signals.id", ondelete="SET NULL"), index=True
    )
    item_type: Mapped[str] = mapped_column(String(64), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    narrative: Mapped[str | None] = mapped_column(Text)
    evidence_manifest: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    limitations: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    included_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utcnow, nullable=False
    )

    __table_args__ = (
        UniqueConstraint("report_id", "ordinal", name="uq_report_item_ordinal"),
        CheckConstraint("ordinal >= 0", name="report_item_ordinal_nonnegative"),
        Index("ix_report_items_org_report", "organization_id", "report_id", "ordinal"),
    )


class UserCorrection(UUIDPrimaryKeyMixin, OrganizationOwnedMixin, CreatedAtMixin, Base):
    __tablename__ = "user_corrections"

    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    field_key: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    previous_value: Mapped[str | None] = mapped_column(Text)
    corrected_value: Mapped[str | None] = mapped_column(Text)
    reason_code: Mapped[str] = mapped_column(String(100), nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False, index=True)
    submitted_by_user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    reviewed_by_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    related_alert_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("alerts.id", ondelete="SET NULL"), index=True
    )
    related_conflict_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("source_conflicts.id", ondelete="SET NULL"), index=True
    )
    source_snapshot_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("source_snapshots.id", ondelete="SET NULL"), index=True
    )
    is_reversed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    reverses_correction_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("user_corrections.id", ondelete="SET NULL"), index=True
    )

    __table_args__ = (
        Index(
            "ix_user_corrections_org_project_status",
            "organization_id",
            "project_id",
            "status",
        ),
    )


class EntityMergeProposal(UUIDPrimaryKeyMixin, OrganizationOwnedMixin, TimestampMixin, Base):
    __tablename__ = "entity_merge_proposals"

    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source_entity_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    target_entity_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False, index=True)
    confidence: Mapped[float | None] = mapped_column(Float)
    reason_codes: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    proposed_by_user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    reviewed_by_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    reversal_of_proposal_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("entity_merge_proposals.id", ondelete="SET NULL"), index=True
    )
    deduplication_key: Mapped[str] = mapped_column(
        String(255), default=new_uuid, nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "organization_id", "deduplication_key", name="uq_entity_merge_proposal_dedup"
        ),
        CheckConstraint(
            "source_entity_id <> target_entity_id", name="entity_merge_distinct_entities"
        ),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="entity_merge_confidence_range",
        ),
        Index(
            "ix_entity_merge_org_status_type", "organization_id", "status", "entity_type"
        ),
    )


class AuditEvent(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "audit_events"

    organization_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="SET NULL"), index=True
    )
    actor_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    action: Mapped[str | None] = mapped_column(String(160), index=True)
    resource_type: Mapped[str | None] = mapped_column(String(100), index=True)
    resource_id: Mapped[str | None] = mapped_column(String(160), index=True)
    request_id: Mapped[str | None] = mapped_column(String(160), index=True)
    ip_address: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(512))
    before_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    after_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utcnow, nullable=False, index=True
    )

    __table_args__ = (
        Index("ix_audit_events_org_created", "organization_id", "created_at"),
        Index("ix_audit_events_resource", "resource_type", "resource_id", "created_at"),
    )


class DeletionTombstone(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "deletion_tombstones"

    organization_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="SET NULL"), index=True
    )
    resource_type: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(160), nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(160), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    deleted_by_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    deleted_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utcnow, nullable=False, index=True
    )
    reason_code: Mapped[str | None] = mapped_column(String(100))
    minimal_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    retention_until: Mapped[datetime | None] = mapped_column(UTCDateTime())

    __table_args__ = (
        UniqueConstraint(
            "organization_id", "resource_type", "resource_id", name="uq_deletion_tombstone_resource"
        ),
        Index("ix_deletion_tombstones_org_deleted", "organization_id", "deleted_at"),
    )


__all__ = [
    "AuditEvent",
    "DeletionTombstone",
    "EntityMergeProposal",
    "Report",
    "ReportItem",
    "UserCorrection",
]
