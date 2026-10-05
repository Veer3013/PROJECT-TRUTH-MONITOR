from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from types import MappingProxyType
from typing import Any

from .enums import (
    AccessCompleteness,
    AccessStatus,
    ErrorCategory,
    SourceClass,
    SourceHealthStatus,
)


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class SourceAccessPolicy:
    source_name: str
    source_type: str
    base_url: str
    allowed_domains: tuple[str, ...]
    enabled: bool = True
    permitted_access_modes: frozenset[str] = frozenset({"httpx"})
    rate_limit_seconds: float = 3.0
    robots_review_status: str = "pending"
    terms_review_status: str = "pending"
    evidence_retention_policy: str = "structured_only"
    may_store_raw_content: bool = False
    playwright_allowed: bool = False
    ocr_allowed: bool = False
    llm_processing_allowed: bool = False
    policy_version: str = "1.0"
    reviewer: str | None = None
    reviewed_at: datetime | None = None
    max_response_bytes: int = 8 * 1024 * 1024
    request_timeout_seconds: float = 15.0
    max_redirects: int = 3
    allowed_schemes: frozenset[str] = frozenset({"https"})
    source_class: SourceClass = SourceClass.UNKNOWN

    def __post_init__(self) -> None:
        domains = tuple(
            sorted({domain.strip().lower().rstrip(".") for domain in self.allowed_domains if domain.strip()})
        )
        object.__setattr__(self, "allowed_domains", domains)
        object.__setattr__(self, "permitted_access_modes", frozenset(self.permitted_access_modes))
        object.__setattr__(self, "allowed_schemes", frozenset(s.lower() for s in self.allowed_schemes))
        if self.rate_limit_seconds < 0:
            raise ValueError("rate_limit_seconds cannot be negative")
        if self.max_response_bytes < 1:
            raise ValueError("max_response_bytes must be positive")
        if self.request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be positive")
        if not domains and "upload" not in self.permitted_access_modes and "fixture" not in self.permitted_access_modes:
            raise ValueError("A network source policy requires at least one allowed domain")


@dataclass(frozen=True, slots=True)
class FetchRequestContext:
    source_id: str
    requested_url: str
    policy: SourceAccessPolicy
    headers: Mapping[str, str] = field(default_factory=dict)
    fixture_key: str | None = None
    upload_content: bytes | None = field(default=None, repr=False)
    upload_filename: str | None = None
    conditional_etag: str | None = None
    conditional_last_modified: str | None = None
    monitoring_run_id: str | None = None
    authorization_attested: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "headers", MappingProxyType(dict(self.headers)))


@dataclass(frozen=True, slots=True)
class FetchResult:
    requested_url: str
    final_url: str
    source_id: str
    http_status: int | None
    fetched_at: datetime
    content_type: str | None
    access_status: AccessStatus
    parser_eligible: bool
    completeness: AccessCompleteness
    content: bytes | None = field(default=None, repr=False)
    content_hash: str | None = None
    source_reported_date: date | datetime | None = None
    response_metadata: Mapping[str, Any] = field(default_factory=dict)
    error_category: ErrorCategory | None = None
    error_detail: str | None = None
    elapsed_ms: int | None = None
    policy_version: str | None = None

    def __post_init__(self) -> None:
        fetched_at = self.fetched_at
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=UTC)
            object.__setattr__(self, "fetched_at", fetched_at)
        if self.content is not None and self.content_hash is None:
            object.__setattr__(self, "content_hash", hashlib.sha256(self.content).hexdigest())
        object.__setattr__(self, "response_metadata", MappingProxyType(dict(self.response_metadata)))
        if self.parser_eligible and self.content is None:
            raise ValueError("parser_eligible fetch results must include content")
        if self.access_status in {
            AccessStatus.BLOCKED,
            AccessStatus.UNAVAILABLE,
            AccessStatus.AUTHENTICATION_REQUIRED,
            AccessStatus.CAPTCHA,
            AccessStatus.ERROR,
        } and self.parser_eligible:
            raise ValueError("blocked or unavailable content cannot be parser eligible")

    @property
    def status_code(self) -> int | None:
        """Compatibility alias for HTTP clients and older call sites."""

        return self.http_status

    @property
    def text(self) -> str:
        if self.content is None:
            return ""
        charset = str(self.response_metadata.get("charset") or "utf-8")
        return self.content.decode(charset, errors="replace")

    @property
    def is_successful(self) -> bool:
        return self.access_status in {AccessStatus.SUCCESS, AccessStatus.PARTIAL}


@dataclass(frozen=True, slots=True)
class SourceHealth:
    source_id: str
    status: SourceHealthStatus
    checked_at: datetime = field(default_factory=utc_now)
    last_successful_fetch: datetime | None = None
    fetch_success_rate: float | None = None
    parser_success_rate: float | None = None
    average_latency_ms: float | None = None
    consecutive_failures: int = 0
    circuit_breaker_status: str = "closed"
    last_content_hash: str | None = None
    parser_version: str | None = None
    policy_version: str | None = None
    degradation_incidents: int = 0
    jobs_awaiting_retry: int = 0
    detail: str | None = None


# Names from the product specification, kept concise at import sites.
RequestContext = FetchRequestContext
SourcePolicy = SourceAccessPolicy
