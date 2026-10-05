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
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, UTCDateTime, utcnow
from app.models.base import OrganizationOwnedMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import MembershipRole, MonitoringCadence


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(512), nullable=False)
    display_name: Mapped[str] = mapped_column(String(160), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    memberships: Mapped[list[OrganizationMembership]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class Organization(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "organizations"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), nullable=False, unique=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    retention_upload_days: Mapped[int] = mapped_column(Integer, default=90, nullable=False)
    retention_report_days: Mapped[int] = mapped_column(Integer, default=90, nullable=False)

    memberships: Mapped[list[OrganizationMembership]] = relationship(
        back_populates="organization", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint(
            "retention_upload_days >= 1", name="organization_retention_upload_positive"
        ),
        CheckConstraint(
            "retention_report_days >= 1", name="organization_retention_report_positive"
        ),
    )


class OrganizationMembership(
    UUIDPrimaryKeyMixin, OrganizationOwnedMixin, TimestampMixin, Base
):
    __tablename__ = "organization_memberships"

    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(
        String(32), default=MembershipRole.VIEWER.value, nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    user: Mapped[User] = relationship(back_populates="memberships")
    organization: Mapped[Organization] = relationship(back_populates="memberships")

    __table_args__ = (
        UniqueConstraint("organization_id", "user_id", name="uq_membership_org_user"),
        CheckConstraint(
            "role IN ('admin', 'reviewer', 'analyst', 'viewer')",
            name="membership_role_valid",
        ),
        Index("ix_memberships_org_active", "organization_id", "is_active"),
    )


class Developer(UUIDPrimaryKeyMixin, OrganizationOwnedMixin, TimestampMixin, Base):
    __tablename__ = "developers"

    canonical_name: Mapped[str] = mapped_column(String(240), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(240), nullable=False)
    registration_identifier: Mapped[str | None] = mapped_column(String(160), index=True)
    website_url: Mapped[str | None] = mapped_column(String(2048))
    identity_status: Mapped[str] = mapped_column(String(32), default="unverified", nullable=False)
    merged_into_developer_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("developers.id", ondelete="SET NULL")
    )

    aliases: Mapped[list[DeveloperAlias]] = relationship(
        back_populates="developer",
        cascade="all, delete-orphan",
        foreign_keys="DeveloperAlias.developer_id",
    )

    __table_args__ = (
        UniqueConstraint("organization_id", "normalized_name", name="uq_developer_org_name"),
        Index("ix_developers_org_identity", "organization_id", "identity_status"),
    )


class DeveloperAlias(UUIDPrimaryKeyMixin, OrganizationOwnedMixin, TimestampMixin, Base):
    __tablename__ = "developer_aliases"

    developer_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("developers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("sources.id", ondelete="SET NULL"), index=True
    )
    alias_name: Mapped[str] = mapped_column(String(240), nullable=False)
    normalized_alias: Mapped[str] = mapped_column(String(240), nullable=False)
    alias_type: Mapped[str] = mapped_column(String(32), default="source_name", nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float)
    review_state: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)

    developer: Mapped[Developer] = relationship(
        back_populates="aliases", foreign_keys=[developer_id]
    )

    __table_args__ = (
        UniqueConstraint(
            "organization_id", "developer_id", "normalized_alias", name="uq_developer_alias"
        ),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="developer_alias_confidence_range",
        ),
    )


class Location(UUIDPrimaryKeyMixin, OrganizationOwnedMixin, TimestampMixin, Base):
    __tablename__ = "locations"

    country: Mapped[str] = mapped_column(String(100), default="India", nullable=False)
    state: Mapped[str] = mapped_column(String(160), nullable=False)
    city: Mapped[str] = mapped_column(String(160), nullable=False)
    locality_or_sector: Mapped[str] = mapped_column(String(240), default="", nullable=False)
    postal_code: Mapped[str | None] = mapped_column(String(32))
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)

    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "country",
            "state",
            "city",
            "locality_or_sector",
            name="uq_location_org_identity",
        ),
        Index("ix_locations_org_city_locality", "organization_id", "city", "locality_or_sector"),
        CheckConstraint(
            "latitude IS NULL OR (latitude >= -90 AND latitude <= 90)",
            name="location_latitude_range",
        ),
        CheckConstraint(
            "longitude IS NULL OR (longitude >= -180 AND longitude <= 180)",
            name="location_longitude_range",
        ),
    )


class Project(UUIDPrimaryKeyMixin, OrganizationOwnedMixin, TimestampMixin, Base):
    __tablename__ = "projects"

    developer_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("developers.id", ondelete="SET NULL"), index=True
    )
    location_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("locations.id", ondelete="SET NULL"), index=True
    )
    parent_project_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="SET NULL"), index=True
    )
    merged_into_project_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="SET NULL"), index=True
    )
    registration_source_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("sources.id", ondelete="SET NULL"), index=True
    )
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(300), nullable=False)
    registration_number: Mapped[str | None] = mapped_column(String(160), index=True)
    phase_name: Mapped[str | None] = mapped_column(String(200))
    project_type: Mapped[str | None] = mapped_column(String(100))
    official_url: Mapped[str | None] = mapped_column(String(2048))
    identity_status: Mapped[str] = mapped_column(String(32), default="unverified", nullable=False)
    is_provisional: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    current_status: Mapped[str | None] = mapped_column(String(100))
    proposed_completion_date: Mapped[date | None] = mapped_column(Date())
    revised_completion_date: Mapped[date | None] = mapped_column(Date())
    lapsed_flag: Mapped[bool | None] = mapped_column(Boolean)
    qpr_missing_flag: Mapped[bool | None] = mapped_column(Boolean)
    last_verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    tracking_started_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utcnow, nullable=False
    )
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    parent_project: Mapped[Project | None] = relationship(
        remote_side="Project.id", back_populates="phases", foreign_keys=[parent_project_id]
    )
    phases: Mapped[list[Project]] = relationship(
        back_populates="parent_project", foreign_keys=[parent_project_id]
    )
    aliases: Mapped[list[ProjectAlias]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index(
            "uq_projects_org_registration",
            "organization_id",
            "registration_number",
            unique=True,
            sqlite_where=text(
                "registration_number IS NOT NULL AND identity_status = 'confirmed'"
            ),
            postgresql_where=text(
                "registration_number IS NOT NULL AND identity_status = 'confirmed'"
            ),
        ),
        Index("ix_projects_org_identity", "organization_id", "identity_status"),
        Index("ix_projects_org_parent", "organization_id", "parent_project_id"),
    )


class ProjectAlias(UUIDPrimaryKeyMixin, OrganizationOwnedMixin, TimestampMixin, Base):
    __tablename__ = "project_aliases"

    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("sources.id", ondelete="SET NULL"), index=True
    )
    alias_name: Mapped[str] = mapped_column(String(300), nullable=False)
    normalized_alias: Mapped[str] = mapped_column(String(300), nullable=False)
    alias_type: Mapped[str] = mapped_column(String(32), default="source_name", nullable=False)
    original_value: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(Float)
    review_state: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)

    project: Mapped[Project] = relationship(back_populates="aliases")

    __table_args__ = (
        UniqueConstraint(
            "organization_id", "project_id", "normalized_alias", name="uq_project_alias"
        ),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="project_alias_confidence_range",
        ),
    )


class Watchlist(UUIDPrimaryKeyMixin, OrganizationOwnedMixin, TimestampMixin, Base):
    __tablename__ = "watchlists"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    monitoring_cadence: Mapped[str] = mapped_column(
        String(16), default=MonitoringCadence.WEEKLY.value, nullable=False
    )
    alert_preferences: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_by_user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    items: Mapped[list[WatchlistItem]] = relationship(
        back_populates="watchlist", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("organization_id", "name", name="uq_watchlist_org_name"),
        CheckConstraint(
            "monitoring_cadence IN ('manual', 'daily', 'weekly')",
            name="watchlist_cadence_valid",
        ),
        Index("ix_watchlists_org_active", "organization_id", "is_active"),
    )


class WatchlistItem(UUIDPrimaryKeyMixin, OrganizationOwnedMixin, TimestampMixin, Base):
    __tablename__ = "watchlist_items"

    watchlist_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("watchlists.id", ondelete="CASCADE"), nullable=False, index=True
    )
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    official_url: Mapped[str | None] = mapped_column(String(2048))
    monitoring_cadence: Mapped[str | None] = mapped_column(String(16))
    input_identity: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(160))
    added_by_user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )

    watchlist: Mapped[Watchlist] = relationship(back_populates="items")

    __table_args__ = (
        UniqueConstraint(
            "organization_id", "watchlist_id", "project_id", name="uq_watchlist_item_identity"
        ),
        Index(
            "uq_watchlist_item_idempotency",
            "organization_id",
            "watchlist_id",
            "idempotency_key",
            unique=True,
            sqlite_where=text("idempotency_key IS NOT NULL"),
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
        CheckConstraint(
            "monitoring_cadence IS NULL OR monitoring_cadence IN ('manual', 'daily', 'weekly')",
            name="watchlist_item_cadence_valid",
        ),
        Index("ix_watchlist_items_org_status", "organization_id", "status"),
    )


class Source(UUIDPrimaryKeyMixin, OrganizationOwnedMixin, TimestampMixin, Base):
    __tablename__ = "sources"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    source_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source_class: Mapped[str] = mapped_column(String(64), nullable=False)
    base_url: Mapped[str | None] = mapped_column(String(2048))
    domain: Mapped[str] = mapped_column(String(253), nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    health_status: Mapped[str] = mapped_column(String(32), default="unknown", nullable=False)
    last_successful_fetch_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_content_hash: Mapped[str | None] = mapped_column(String(128))
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    circuit_breaker_state: Mapped[str] = mapped_column(String(32), default="closed", nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "domain",
            "name",
            "base_url",
            name="uq_source_org_domain_name_url",
        ),
        CheckConstraint(
            "consecutive_failures >= 0", name="source_consecutive_failures_nonnegative"
        ),
        Index("ix_sources_org_enabled", "organization_id", "is_enabled"),
        Index("ix_sources_org_domain", "organization_id", "domain"),
    )


class ProjectSourceLink(
    UUIDPrimaryKeyMixin, OrganizationOwnedMixin, TimestampMixin, Base
):
    __tablename__ = "project_source_links"

    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("sources.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_role: Mapped[str] = mapped_column(
        String(32), default="supporting", nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "project_id",
            "source_id",
            name="uq_project_source_link",
        ),
        CheckConstraint(
            "source_role IN ('primary_registration', 'supporting')",
            name="project_source_link_role_valid",
        ),
        Index(
            "ix_project_source_links_org_project_active",
            "organization_id",
            "project_id",
            "is_active",
        ),
    )


class SourceAccessPolicy(
    UUIDPrimaryKeyMixin, OrganizationOwnedMixin, TimestampMixin, Base
):
    __tablename__ = "source_access_policies"

    source_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("sources.id", ondelete="CASCADE"), nullable=False, index=True
    )
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    permitted_access_modes: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    rate_limit_requests: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    rate_limit_period_seconds: Mapped[float] = mapped_column(Float, default=3.0, nullable=False)
    robots_review_status: Mapped[str] = mapped_column(
        String(32), default="not_reviewed", nullable=False
    )
    terms_review_status: Mapped[str] = mapped_column(
        String(32), default="not_reviewed", nullable=False
    )
    evidence_retention_days: Mapped[int] = mapped_column(Integer, default=90, nullable=False)
    raw_content_may_be_stored: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    playwright_allowed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    ocr_allowed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    llm_allowed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    reviewer_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    notes: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint(
            "organization_id", "source_id", "policy_version", name="uq_source_policy_version"
        ),
        Index(
            "uq_source_policy_current",
            "organization_id",
            "source_id",
            unique=True,
            sqlite_where=text("is_current = 1"),
            postgresql_where=text("is_current"),
        ),
        CheckConstraint(
            "rate_limit_requests >= 1", name="source_policy_rate_requests_positive"
        ),
        CheckConstraint(
            "rate_limit_period_seconds > 0", name="source_policy_rate_period_positive"
        ),
        CheckConstraint(
            "evidence_retention_days >= 1", name="source_policy_retention_positive"
        ),
    )


__all__ = [
    "Developer",
    "DeveloperAlias",
    "Location",
    "Organization",
    "OrganizationMembership",
    "Project",
    "ProjectAlias",
    "Source",
    "SourceAccessPolicy",
    "User",
    "Watchlist",
    "WatchlistItem",
]
