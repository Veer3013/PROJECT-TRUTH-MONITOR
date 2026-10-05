from __future__ import annotations

from enum import StrEnum


class MembershipRole(StrEnum):
    ADMIN = "admin"
    REVIEWER = "reviewer"
    ANALYST = "analyst"
    VIEWER = "viewer"


class MonitoringCadence(StrEnum):
    MANUAL = "manual"
    DAILY = "daily"
    WEEKLY = "weekly"


class ReviewState(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    DISMISSED = "dismissed"
    CORRECTED = "corrected"
    EXPECTED = "expected"
    UNRESOLVED = "unresolved"


class Severity(StrEnum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ConfidenceBand(StrEnum):
    CONFIRMED = "confirmed"
    HIGH = "high_confidence"
    PROBABLE = "probable"
    NEEDS_REVIEW = "needs_review"
    UNVERIFIED = "unverified"
    CONFLICTING = "conflicting_sources"


class ReportState(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    EXPIRED = "expired"
    DELETED = "deleted"


class ComparisonState(StrEnum):
    MATCHED = "matched"
    CONFLICTING = "conflicting"
    MARKETING_ONLY = "marketing_only"
    OFFICIAL_ONLY = "official_only"
    UNVERIFIED = "unverified"
    NEEDS_REVIEW = "needs_review"


__all__ = [
    "ComparisonState",
    "ConfidenceBand",
    "MembershipRole",
    "MonitoringCadence",
    "ReportState",
    "ReviewState",
    "Severity",
]
