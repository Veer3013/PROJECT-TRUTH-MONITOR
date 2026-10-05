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
    Numeric,
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
from app.models.enums import ReviewState, Severity


class DetectedChange(UUIDPrimaryKeyMixin, OrganizationOwnedMixin, CreatedAtMixin, Base):
    __tablename__ = "detected_changes"

    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    source_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("sources.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    old_snapshot_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("source_snapshots.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    new_snapshot_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("source_snapshots.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    field_key: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    old_raw_value: Mapped[str | None] = mapped_column(Text)
    old_canonical_value: Mapped[str | None] = mapped_column(Text)
    new_raw_value: Mapped[str | None] = mapped_column(Text)
    new_canonical_value: Mapped[str | None] = mapped_column(Text)
    change_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    magnitude: Mapped[float | None] = mapped_column(Numeric(24, 8))
    magnitude_unit: Mapped[str | None] = mapped_column(String(64))
    magnitude_days: Mapped[int | None] = mapped_column(Integer)
    source_reliability: Mapped[float | None] = mapped_column(Float)
    source_access_completeness: Mapped[str | None] = mapped_column(String(32))
    identity_confidence: Mapped[float | None] = mapped_column(Float)
    extraction_confidence: Mapped[float | None] = mapped_column(Float)
    change_confidence: Mapped[float | None] = mapped_column(Float)
    confidence_dimensions: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    severity: Mapped[str] = mapped_column(
        String(16), default=Severity.INFO.value, nullable=False, index=True
    )
    reason_codes: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    requires_review: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    review_state: Mapped[str] = mapped_column(
        String(32), default=ReviewState.PENDING.value, nullable=False
    )
    parser_compatible: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    detected_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utcnow, nullable=False
    )
    deduplication_key: Mapped[str] = mapped_column(
        String(255), default=new_uuid, nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "organization_id", "deduplication_key", name="uq_detected_change_dedup"
        ),
        CheckConstraint(
            "old_snapshot_id <> new_snapshot_id", name="detected_change_distinct_snapshots"
        ),
        CheckConstraint(
            "source_reliability IS NULL OR (source_reliability >= 0 AND source_reliability <= 1)",
            name="detected_change_source_reliability_range",
        ),
        CheckConstraint(
            "identity_confidence IS NULL OR (identity_confidence >= 0 AND identity_confidence <= 1)",
            name="detected_change_identity_confidence_range",
        ),
        CheckConstraint(
            "extraction_confidence IS NULL OR "
            "(extraction_confidence >= 0 AND extraction_confidence <= 1)",
            name="detected_change_extraction_confidence_range",
        ),
        CheckConstraint(
            "change_confidence IS NULL OR (change_confidence >= 0 AND change_confidence <= 1)",
            name="detected_change_change_confidence_range",
        ),
        Index(
            "ix_detected_changes_org_project_detected",
            "organization_id",
            "project_id",
            "detected_at",
        ),
        Index(
            "ix_detected_changes_org_review", "organization_id", "review_state", "severity"
        ),
    )


class ChangeEvidence(UUIDPrimaryKeyMixin, OrganizationOwnedMixin, CreatedAtMixin, Base):
    __tablename__ = "change_evidence"

    detected_change_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("detected_changes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    snapshot_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("source_snapshots.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    snapshot_field_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("snapshot_fields.id", ondelete="SET NULL"), index=True
    )
    evidence_side: Mapped[str] = mapped_column(String(16), nullable=False)
    evidence_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source_class: Mapped[str] = mapped_column(String(64), nullable=False)
    source_url: Mapped[str | None] = mapped_column(String(2048))
    evidence_reference: Mapped[str | None] = mapped_column(String(1024))
    excerpt: Mapped[str | None] = mapped_column(Text)
    selector: Mapped[str | None] = mapped_column(Text)
    page_number: Mapped[int | None] = mapped_column(Integer)
    coordinates: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    content_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    extraction_method: Mapped[str | None] = mapped_column(String(64))
    evidence_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON, default=dict, nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "detected_change_id",
            "snapshot_id",
            "evidence_side",
            "evidence_type",
            name="uq_change_evidence_identity",
        ),
        CheckConstraint(
            "evidence_side IN ('old', 'new', 'context')", name="change_evidence_side_valid"
        ),
        CheckConstraint(
            "page_number IS NULL OR page_number >= 1", name="change_evidence_page_positive"
        ),
    )


class Signal(UUIDPrimaryKeyMixin, OrganizationOwnedMixin, CreatedAtMixin, Base):
    __tablename__ = "signals"

    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    primary_detected_change_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("detected_changes.id", ondelete="SET NULL"), index=True
    )
    rule_code: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    rule_version: Mapped[str] = mapped_column(String(64), nullable=False)
    supporting_change_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    observed_facts: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float)
    confidence_dimensions: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    limitations: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    required_review_level: Mapped[str] = mapped_column(
        String(32), default="none", nullable=False
    )
    state: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    detected_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utcnow, nullable=False
    )
    deduplication_key: Mapped[str] = mapped_column(
        String(255), default=new_uuid, nullable=False
    )

    __table_args__ = (
        UniqueConstraint("organization_id", "deduplication_key", name="uq_signal_dedup"),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="signal_confidence_range",
        ),
        Index("ix_signals_org_project_detected", "organization_id", "project_id", "detected_at"),
        Index("ix_signals_org_rule", "organization_id", "rule_code", "state"),
    )


class Alert(UUIDPrimaryKeyMixin, OrganizationOwnedMixin, TimestampMixin, Base):
    __tablename__ = "alerts"

    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    signal_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("signals.id", ondelete="SET NULL"), index=True
    )
    detected_change_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("detected_changes.id", ondelete="SET NULL"), index=True
    )
    source_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("sources.id", ondelete="SET NULL"), index=True
    )
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    neutral_description: Mapped[str] = mapped_column(Text, nullable=False)
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    magnitude: Mapped[str | None] = mapped_column(String(160))
    state: Mapped[str] = mapped_column(String(32), default="open", nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    required_review_level: Mapped[str] = mapped_column(
        String(32), default="none", nullable=False
    )
    requires_review: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    source_class: Mapped[str | None] = mapped_column(String(64))
    freshness: Mapped[str | None] = mapped_column(String(64))
    limitation: Mapped[str | None] = mapped_column(Text)
    suggested_review_action: Mapped[str | None] = mapped_column(Text)
    first_seen_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utcnow, nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utcnow, nullable=False
    )
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    deduplication_key: Mapped[str] = mapped_column(
        String(255), default=new_uuid, nullable=False
    )

    status = synonym("state")

    __table_args__ = (
        UniqueConstraint("organization_id", "deduplication_key", name="uq_alert_dedup"),
        Index("ix_alerts_org_state", "organization_id", "state", "severity"),
        Index("ix_alerts_org_project_state", "organization_id", "project_id", "state"),
        Index(
            "ix_alerts_org_review_required", "organization_id", "requires_review", "state"
        ),
    )


class AlertReview(UUIDPrimaryKeyMixin, OrganizationOwnedMixin, CreatedAtMixin, Base):
    __tablename__ = "alert_reviews"

    alert_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("alerts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    reviewer_user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(100), nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    before_reference: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    after_reference: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    is_reversible: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    reverses_review_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("alert_reviews.id", ondelete="SET NULL"), index=True
    )
    reviewed_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utcnow, nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "decision IN "
            "('confirm', 'dismiss', 'correct', 'request_another_source', "
            "'mark_expected', 'keep_unresolved', 'reverse')",
            name="alert_review_decision_valid",
        ),
        Index("ix_alert_reviews_org_reviewed", "organization_id", "reviewed_at"),
    )


class SourceConflict(UUIDPrimaryKeyMixin, OrganizationOwnedMixin, TimestampMixin, Base):
    __tablename__ = "source_conflicts"

    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    field_key: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    source_a_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("sources.id", ondelete="SET NULL"), index=True
    )
    source_b_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("sources.id", ondelete="SET NULL"), index=True
    )
    source_a_class: Mapped[str] = mapped_column(String(64), nullable=False)
    source_b_class: Mapped[str] = mapped_column(String(64), nullable=False)
    source_a_value: Mapped[str | None] = mapped_column(Text)
    source_b_value: Mapped[str | None] = mapped_column(Text)
    source_a_reference: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    source_b_reference: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    source_a_freshness_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    source_b_freshness_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    comparison_state: Mapped[str] = mapped_column(String(32), default="conflicting", nullable=False)
    is_material: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    state: Mapped[str] = mapped_column(String(32), default="open", nullable=False, index=True)
    detected_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utcnow, nullable=False
    )
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    resolved_by_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    resolution_reason: Mapped[str | None] = mapped_column(Text)
    selected_value: Mapped[str | None] = mapped_column(Text)
    deduplication_key: Mapped[str] = mapped_column(
        String(255), default=new_uuid, nullable=False
    )

    status = synonym("state")

    __table_args__ = (
        UniqueConstraint(
            "organization_id", "deduplication_key", name="uq_source_conflict_dedup"
        ),
        Index(
            "ix_source_conflicts_org_project_state",
            "organization_id",
            "project_id",
            "state",
        ),
    )


class MarketSnapshot(UUIDPrimaryKeyMixin, OrganizationOwnedMixin, CreatedAtMixin, Base):
    __tablename__ = "market_snapshots"

    location_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("locations.id", ondelete="SET NULL"), index=True
    )
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    projects_monitored: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    active_projects: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    lapsed_projects: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    qpr_flagged_projects: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    recent_reviewed_completion_changes: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    new_registrations_or_phases: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    developer_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    source_health_summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    basis_note: Mapped[str] = mapped_column(
        Text,
        default=(
            "Based only on projects in this monitored dataset - not the complete Gurugram market."
        ),
        nullable=False,
    )
    generated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utcnow, nullable=False
    )
    deduplication_key: Mapped[str] = mapped_column(
        String(255), default=new_uuid, nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "organization_id", "deduplication_key", name="uq_market_snapshot_dedup"
        ),
        CheckConstraint("period_end >= period_start", name="market_snapshot_period_valid"),
        CheckConstraint(
            "projects_monitored >= 0 AND active_projects >= 0 AND lapsed_projects >= 0 "
            "AND qpr_flagged_projects >= 0 AND recent_reviewed_completion_changes >= 0 "
            "AND new_registrations_or_phases >= 0 AND developer_count >= 0",
            name="market_snapshot_counts_nonnegative",
        ),
        Index(
            "ix_market_snapshots_org_period", "organization_id", "period_end", "location_id"
        ),
    )


__all__ = [
    "Alert",
    "AlertReview",
    "ChangeEvidence",
    "DetectedChange",
    "MarketSnapshot",
    "Signal",
    "SourceConflict",
]
