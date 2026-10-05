from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
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
    event,
    inspect,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, UTCDateTime, utcnow
from app.models.base import (
    CreatedAtMixin,
    OrganizationOwnedMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    new_uuid,
)
from app.models.enums import MonitoringCadence


class MonitoringJob(UUIDPrimaryKeyMixin, OrganizationOwnedMixin, TimestampMixin, Base):
    __tablename__ = "monitoring_jobs"

    source_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("sources.id", ondelete="CASCADE"), nullable=False, index=True
    )
    project_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    watchlist_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("watchlists.id", ondelete="CASCADE"), index=True
    )
    job_type: Mapped[str] = mapped_column(String(64), default="source_refresh", nullable=False)
    cadence: Mapped[str] = mapped_column(
        String(16), default=MonitoringCadence.WEEKLY.value, nullable=False
    )
    state: Mapped[str] = mapped_column(String(32), default="scheduled", nullable=False)
    next_run_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True)
    last_run_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_retries: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    deduplication_key: Mapped[str] = mapped_column(
        String(255), default=new_uuid, nullable=False
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "organization_id", "deduplication_key", name="uq_monitoring_job_dedup"
        ),
        CheckConstraint("retry_count >= 0", name="monitoring_job_retry_nonnegative"),
        CheckConstraint("max_retries >= 0", name="monitoring_job_max_retries_nonnegative"),
        CheckConstraint(
            "cadence IN ('manual', 'daily', 'weekly')", name="monitoring_job_cadence_valid"
        ),
        Index("ix_monitoring_jobs_org_next_run", "organization_id", "next_run_at"),
        Index("ix_monitoring_jobs_org_state", "organization_id", "state"),
    )


class MonitoringRun(UUIDPrimaryKeyMixin, OrganizationOwnedMixin, CreatedAtMixin, Base):
    __tablename__ = "monitoring_runs"

    monitoring_job_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("monitoring_jobs.id", ondelete="SET NULL"), index=True
    )
    source_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("sources.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    project_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="SET NULL"), index=True
    )
    triggered_by_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    trigger_type: Mapped[str] = mapped_column(String(32), default="scheduled", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="queued", nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    deduplication_key: Mapped[str] = mapped_column(
        String(255), default=new_uuid, nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    fetched_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    snapshot_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    access_completeness: Mapped[str] = mapped_column(
        String(32), default="unavailable", nullable=False
    )
    error_category: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    run_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON, default=dict, nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "organization_id", "deduplication_key", name="uq_monitoring_run_dedup"
        ),
        CheckConstraint(
            "attempt_number >= 1", name="monitoring_run_attempt_number_positive"
        ),
        CheckConstraint("fetched_count >= 0", name="monitoring_run_fetched_nonnegative"),
        CheckConstraint("snapshot_count >= 0", name="monitoring_run_snapshot_nonnegative"),
        Index("ix_monitoring_runs_org_status", "organization_id", "status"),
        Index("ix_monitoring_runs_org_created", "organization_id", "created_at"),
    )


class SourceSnapshot(UUIDPrimaryKeyMixin, OrganizationOwnedMixin, CreatedAtMixin, Base):
    __tablename__ = "source_snapshots"

    source_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("sources.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    project_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="RESTRICT"), index=True
    )
    monitoring_run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("monitoring_runs.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    entity_type: Mapped[str] = mapped_column(String(64), default="project", nullable=False)
    entity_id: Mapped[str] = mapped_column(String(36), nullable=False)
    fetch_status: Mapped[str] = mapped_column(String(32), nullable=False)
    requested_url: Mapped[str | None] = mapped_column(String(2048))
    final_url: Mapped[str | None] = mapped_column(String(2048))
    http_status: Mapped[int | None] = mapped_column(Integer)
    fetched_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utcnow, nullable=False
    )
    source_date: Mapped[datetime | None] = mapped_column(UTCDateTime())
    content_type: Mapped[str | None] = mapped_column(String(255))
    content_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    parser_name: Mapped[str] = mapped_column(String(160), nullable=False)
    parser_version: Mapped[str] = mapped_column(String(64), nullable=False)
    access_completeness: Mapped[str] = mapped_column(String(32), nullable=False)
    structured_facts: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    evidence_references: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, default=list, nullable=False
    )
    validation_status: Mapped[str] = mapped_column(String(32), nullable=False)
    validation_warnings: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    raw_payload_storage_key: Mapped[str | None] = mapped_column(String(1024))
    raw_payload_hash: Mapped[str | None] = mapped_column(String(128))
    response_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    deduplication_key: Mapped[str] = mapped_column(
        String(255), default=new_uuid, nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "organization_id", "deduplication_key", name="uq_source_snapshot_dedup"
        ),
        Index(
            "ix_source_snapshots_source_entity_fetched",
            "organization_id",
            "source_id",
            "entity_type",
            "entity_id",
            "fetched_at",
        ),
        Index(
            "ix_source_snapshots_project_fetched", "organization_id", "project_id", "fetched_at"
        ),
        CheckConstraint(
            "latency_ms IS NULL OR latency_ms >= 0", name="source_snapshot_latency_nonnegative"
        ),
    )


class SnapshotField(UUIDPrimaryKeyMixin, OrganizationOwnedMixin, CreatedAtMixin, Base):
    __tablename__ = "snapshot_fields"

    snapshot_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("source_snapshots.id", ondelete="CASCADE"), nullable=False, index=True
    )
    field_key: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    occurrence_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    raw_label: Mapped[str | None] = mapped_column(Text)
    raw_value: Mapped[str | None] = mapped_column(Text)
    canonical_value: Mapped[str | None] = mapped_column(Text)
    normalized_text: Mapped[str | None] = mapped_column(Text)
    normalized_number: Mapped[float | None] = mapped_column(Numeric(24, 8))
    normalized_date: Mapped[date | None] = mapped_column(Date)
    value_type: Mapped[str] = mapped_column(String(32), default="text", nullable=False)
    unit: Mapped[str | None] = mapped_column(String(64))
    selector: Mapped[str | None] = mapped_column(Text)
    page_number: Mapped[int | None] = mapped_column(Integer)
    coordinates: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    extraction_method: Mapped[str] = mapped_column(String(64), nullable=False)
    extraction_confidence: Mapped[float | None] = mapped_column(Float)
    parser_name: Mapped[str] = mapped_column(String(160), nullable=False)
    parser_version: Mapped[str] = mapped_column(String(64), nullable=False)
    validation_warnings: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "snapshot_id", "field_key", "occurrence_index", name="uq_snapshot_field_occurrence"
        ),
        CheckConstraint(
            "occurrence_index >= 0", name="snapshot_field_occurrence_nonnegative"
        ),
        CheckConstraint(
            "page_number IS NULL OR page_number >= 1", name="snapshot_field_page_positive"
        ),
        CheckConstraint(
            "extraction_confidence IS NULL OR "
            "(extraction_confidence >= 0 AND extraction_confidence <= 1)",
            name="snapshot_field_confidence_range",
        ),
        Index("ix_snapshot_fields_org_key", "organization_id", "field_key"),
    )


class ImmutableSnapshotError(RuntimeError):
    """Raised when code attempts to mutate a persisted source fact."""


def _reject_persisted_column_changes(target: object, label: str) -> None:
    state = inspect(target)
    if not state.persistent:
        return
    changed = [
        attribute.key
        for attribute in state.mapper.column_attrs
        if state.attrs[attribute.key].history.has_changes()
    ]
    if changed:
        fields = ", ".join(sorted(changed))
        raise ImmutableSnapshotError(
            f"{label} rows are immutable; create a new snapshot or extraction ({fields})"
        )


@event.listens_for(SourceSnapshot, "before_update")
def _prevent_source_snapshot_update(mapper: object, connection: object, target: SourceSnapshot) -> None:
    del mapper, connection
    _reject_persisted_column_changes(target, "SourceSnapshot")


@event.listens_for(SnapshotField, "before_update")
def _prevent_snapshot_field_update(mapper: object, connection: object, target: SnapshotField) -> None:
    del mapper, connection
    _reject_persisted_column_changes(target, "SnapshotField")


__all__ = [
    "ImmutableSnapshotError",
    "MonitoringJob",
    "MonitoringRun",
    "SnapshotField",
    "SourceSnapshot",
]
