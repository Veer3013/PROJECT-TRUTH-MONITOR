from __future__ import annotations

from enum import StrEnum


class AccessStatus(StrEnum):
    SUCCESS = "success"
    PARTIAL = "partial"
    BLOCKED = "blocked"
    UNAVAILABLE = "unavailable"
    AUTHENTICATION_REQUIRED = "authentication_required"
    CAPTCHA = "captcha"
    RATE_LIMITED = "rate_limited"
    ERROR = "error"


class AccessCompleteness(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    BLOCKED = "blocked"
    UNAVAILABLE = "unavailable"


class ErrorCategory(StrEnum):
    UNSUPPORTED_URL = "unsupported_url"
    POLICY_BLOCKED = "policy_blocked"
    AUTHENTICATION_REQUIRED = "authentication_required"
    CAPTCHA = "captcha"
    ACCESS_RESTRICTED = "access_restricted"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    NETWORK = "network"
    RESPONSE_TOO_LARGE = "response_too_large"
    INVALID_RESPONSE = "invalid_response"
    LAYOUT_CHANGED = "layout_changed"
    PARSER_FAILED = "parser_failed"
    PDF_UNREADABLE = "pdf_unreadable"
    NOT_FOUND = "not_found"
    UNKNOWN = "unknown"


class SourceHealthStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    DISABLED = "disabled"
    CIRCUIT_OPEN = "circuit_open"


class ValidationStatus(StrEnum):
    VALID = "valid"
    PARTIAL = "partial"
    INVALID_LAYOUT = "invalid_layout"
    UNREADABLE = "unreadable"
    NEEDS_REVIEW = "needs_review"


class SourceClass(StrEnum):
    OFFICIAL_REGULATOR = "official_regulator"
    OFFICIAL_DEVELOPER = "official_developer"
    MARKETING_BROCHURE = "marketing_brochure"
    USER_AUTHORIZED = "user_authorized"
    REVIEWED_CORRECTION = "reviewed_correction"
    FIXTURE = "fixture"
    UNKNOWN = "unknown"


class ChangeType(StrEnum):
    VALUE_ADDED = "value_added"
    VALUE_CHANGED = "value_changed"
    VALUE_REMOVED = "value_removed"
    DATE_MOVED_EARLIER = "date_moved_earlier"
    DATE_EXTENDED = "date_extended"
    STATUS_CHANGED = "status_changed"
    FLAG_ADDED = "flag_added"
    FLAG_REMOVED = "flag_removed"
    DOCUMENT_LINK_ADDED = "document_link_added"
    DOCUMENT_LINK_REMOVED = "document_link_removed"
    VALUE_REVERTED = "value_reverted"
    PARSER_VERSION_INCOMPATIBLE = "parser_version_incompatible"
    SOURCE_CONFLICT = "source_conflict"
    SOURCE_DEGRADED = "source_degraded"


class Severity(StrEnum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ReviewRequirement(StrEnum):
    NONE = "none"
    ANALYST = "analyst"
    REVIEWER = "reviewer"
    ADMIN = "admin"


class ReviewState(StrEnum):
    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    CONFIRMED = "confirmed"
    DISMISSED = "dismissed"
    CORRECTED = "corrected"
    EXPECTED = "expected"
    UNRESOLVED = "unresolved"


class ConfidenceBand(StrEnum):
    CONFIRMED = "Confirmed"
    HIGH = "High confidence"
    PROBABLE = "Probable"
    NEEDS_REVIEW = "Needs review"
    UNVERIFIED = "Unverified"
    CONFLICTING = "Conflicting sources"


class ConflictState(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"


class ComparisonState(StrEnum):
    MATCHED = "MATCHED"
    CONFLICTING = "CONFLICTING"
    MARKETING_ONLY = "MARKETING_ONLY"
    OFFICIAL_ONLY = "OFFICIAL_ONLY"
    UNVERIFIED = "UNVERIFIED"
    NEEDS_REVIEW = "NEEDS_REVIEW"

