from __future__ import annotations

import mimetypes
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from app.domain import (
    AccessCompleteness,
    AccessStatus,
    ErrorCategory,
    FetchRequestContext,
    FetchResult,
    SourceAccessPolicy,
    SourceHealth,
    SourceHealthStatus,
)
from app.domain.source import utc_now
from app.services.source_policy import SourcePolicyError, validate_policy_for_mode

from .base import CollectorMetrics, CollectorPolicyError, SourceCollector


@dataclass(frozen=True, slots=True)
class FixtureResponse:
    content: bytes | str | Path | None
    content_type: str | None = None
    http_status: int = 200
    access_status: AccessStatus = AccessStatus.SUCCESS
    completeness: AccessCompleteness = AccessCompleteness.COMPLETE
    final_url: str | None = None
    source_reported_date: date | datetime | None = None
    error_category: ErrorCategory | None = None
    response_metadata: Mapping[str, str] | None = None


class FixtureCollector(SourceCollector):
    collector_name = "fixture"

    def __init__(
        self,
        fixtures: Mapping[str, FixtureResponse | bytes | str | Path],
        *,
        source_id: str = "fixture",
        fixture_root: Path | None = None,
    ) -> None:
        self._fixtures = dict(fixtures)
        self.source_id = source_id
        self.fixture_root = fixture_root.resolve() if fixture_root else None
        self.metrics = CollectorMetrics()

    def can_handle(self, source: object) -> bool:
        value = getattr(source, "requested_url", None) or getattr(source, "url", None) or str(source)
        key = getattr(source, "fixture_key", None) or value
        return key in self._fixtures or str(value).startswith("fixture://")

    def validate_policy(self, source_policy: SourceAccessPolicy) -> None:
        try:
            validate_policy_for_mode(source_policy, "fixture")
        except SourcePolicyError as exc:
            raise CollectorPolicyError(exc.code, str(exc)) from exc

    def _read_content(self, value: bytes | str | Path | None) -> bytes | None:
        if value is None:
            return None
        if isinstance(value, bytes):
            return value
        if isinstance(value, Path):
            path = value.resolve()
            if self.fixture_root is not None and path != self.fixture_root and self.fixture_root not in path.parents:
                raise CollectorPolicyError("fixture_path_escape", "Fixture path is outside the configured fixture root.")
            return path.read_bytes()
        return value.encode("utf-8")

    async def fetch(self, request_context: FetchRequestContext) -> FetchResult:
        self.validate_policy(request_context.policy)
        self.metrics.attempts += 1
        key = request_context.fixture_key or request_context.requested_url
        fixture = self._fixtures.get(key)
        if fixture is None and request_context.requested_url.startswith("fixture://"):
            fixture = self._fixtures.get(request_context.requested_url.removeprefix("fixture://"))
        if fixture is None:
            self.metrics.consecutive_failures += 1
            self.metrics.degradation_incidents += 1
            return FetchResult(
                requested_url=request_context.requested_url,
                final_url=request_context.requested_url,
                source_id=request_context.source_id,
                http_status=404,
                fetched_at=utc_now(),
                content_type=None,
                access_status=AccessStatus.UNAVAILABLE,
                parser_eligible=False,
                completeness=AccessCompleteness.UNAVAILABLE,
                error_category=ErrorCategory.NOT_FOUND,
                error_detail="The requested frozen fixture does not exist.",
                policy_version=request_context.policy.policy_version,
            )
        explicit_response = isinstance(fixture, FixtureResponse)
        response = fixture if explicit_response else FixtureResponse(fixture)
        content = self._read_content(response.content)
        if content is not None and not explicit_response:
            sample = content[:250_000].decode("utf-8", errors="ignore")
            status_match = re.search(
                r'<meta\s+name=["\']fixture-http-status["\']\s+content=["\'](\d{3})["\']',
                sample,
                flags=re.IGNORECASE,
            )
            access_match = re.search(
                r'<meta\s+name=["\']fixture-access-status["\']\s+content=["\']([^"\']+)["\']',
                sample,
                flags=re.IGNORECASE,
            )
            fixture_status = int(status_match.group(1)) if status_match else response.http_status
            access_directive = access_match.group(1).strip().lower() if access_match else ""
            sample_lower = sample.casefold()
            if access_directive == "blocked" or "captcha" in sample_lower or "verify that you are human" in sample_lower:
                response = FixtureResponse(
                    None,
                    content_type="text/html",
                    http_status=fixture_status,
                    access_status=AccessStatus.CAPTCHA,
                    completeness=AccessCompleteness.BLOCKED,
                    error_category=ErrorCategory.CAPTCHA,
                    response_metadata={"fixture_access_directive": access_directive or "captcha"},
                )
                content = None
            elif access_directive == "unavailable" or fixture_status >= 500:
                response = FixtureResponse(
                    None,
                    content_type="text/html",
                    http_status=fixture_status,
                    access_status=AccessStatus.UNAVAILABLE,
                    completeness=AccessCompleteness.UNAVAILABLE,
                    error_category=ErrorCategory.NETWORK,
                    response_metadata={"fixture_access_directive": access_directive or "unavailable"},
                )
                content = None
        content_type = response.content_type
        if content_type is None:
            guessed, _ = mimetypes.guess_type(str(key))
            content_type = guessed or ("application/pdf" if content and content.startswith(b"%PDF-") else "text/html")
        parser_eligible = (
            content is not None
            and response.access_status in {AccessStatus.SUCCESS, AccessStatus.PARTIAL}
            and 200 <= response.http_status < 300
        )
        result = FetchResult(
            requested_url=request_context.requested_url,
            final_url=response.final_url or request_context.requested_url,
            source_id=request_context.source_id,
            http_status=response.http_status,
            fetched_at=utc_now(),
            content_type=content_type,
            access_status=response.access_status,
            parser_eligible=parser_eligible,
            completeness=response.completeness,
            content=content,
            source_reported_date=response.source_reported_date,
            response_metadata=response.response_metadata or {"fixture_key": str(key)},
            error_category=response.error_category,
            policy_version=request_context.policy.policy_version,
        )
        if result.is_successful:
            self.metrics.successes += 1
            self.metrics.consecutive_failures = 0
            self.metrics.last_successful_fetch = result.fetched_at
            self.metrics.last_content_hash = result.content_hash
            self.metrics.parser_eligible_fetches += int(result.parser_eligible)
        else:
            self.metrics.consecutive_failures += 1
            self.metrics.degradation_incidents += 1
        return result

    def health_check(self) -> SourceHealth:
        status = SourceHealthStatus.HEALTHY
        if self.metrics.consecutive_failures:
            status = SourceHealthStatus.DEGRADED
        return SourceHealth(
            source_id=self.source_id,
            status=status,
            last_successful_fetch=self.metrics.last_successful_fetch,
            fetch_success_rate=self.metrics.success_rate,
            average_latency_ms=self.metrics.average_latency_ms,
            consecutive_failures=self.metrics.consecutive_failures,
            last_content_hash=self.metrics.last_content_hash,
            degradation_incidents=self.metrics.degradation_incidents,
            detail=f"{len(self._fixtures)} frozen fixtures configured",
        )


FrozenFixtureCollector = FixtureCollector
