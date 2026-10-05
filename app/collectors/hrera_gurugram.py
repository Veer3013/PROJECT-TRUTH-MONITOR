from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urljoin, urlsplit

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
from app.services.source_policy import (
    Resolver,
    SourcePolicyError,
    UnsafeURLError,
    validate_policy_for_mode,
    validate_public_url,
)

from .base import CollectorMetrics, CollectorPolicyError, SourceCollector

_SAFE_RESPONSE_HEADERS = frozenset(
    {"etag", "last-modified", "cache-control", "content-language", "content-length", "retry-after"}
)
_CAPTCHA_MARKERS = ("captcha", "recaptcha", "verify you are human", "human verification")
_AUTH_MARKERS = ("type=\"password\"", "type='password'", "sign in to continue", "login required")


@dataclass(frozen=True, slots=True)
class _BufferedResponse:
    status_code: int
    headers: dict[str, str]
    encoding: str | None


class HRERAGurugramCollector(SourceCollector):
    """Optional policy-gated HTTP adapter for permitted HRERA public pages.

    It deliberately does not include CAPTCHA solving, browser impersonation, login,
    cookie harvesting or anti-bot bypass behavior.
    """

    collector_name = "hrera_gurugram"
    OFFICIAL_DOMAINS = ("haryanarera.gov.in",)

    def __init__(
        self,
        *,
        client: Any | None = None,
        resolver: Resolver | None = None,
        resolve_dns: bool = True,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        circuit_failure_threshold: int = 5,
        source_id: str = "hrera-gurugram",
    ) -> None:
        self._client = client
        self._resolver = resolver
        self._resolve_dns = resolve_dns
        self._sleeper = sleeper
        self._last_request_monotonic: float | None = None
        self._rate_lock = asyncio.Lock()
        self._circuit_failure_threshold = max(1, circuit_failure_threshold)
        self._circuit_open = False
        self.source_id = source_id
        self.metrics = CollectorMetrics()

    @staticmethod
    def _url_from_source(source: Any) -> str:
        return str(
            getattr(source, "requested_url", None)
            or getattr(source, "url", None)
            or getattr(source, "base_url", None)
            or source
        )

    def can_handle(self, source: Any) -> bool:
        url = self._url_from_source(source)
        hostname = (urlsplit(url).hostname or "").lower().rstrip(".")
        return any(hostname == domain or hostname.endswith(f".{domain}") for domain in self.OFFICIAL_DOMAINS)

    def validate_policy(self, source_policy: SourceAccessPolicy) -> None:
        try:
            validate_policy_for_mode(source_policy, "httpx")
        except SourcePolicyError as exc:
            raise CollectorPolicyError(exc.code, str(exc)) from exc
        if not any(
            configured == official or configured.endswith(f".{official}")
            for configured in source_policy.allowed_domains
            for official in self.OFFICIAL_DOMAINS
        ):
            raise CollectorPolicyError(
                "hrera_domain_not_approved",
                "The policy does not approve an official HRERA Haryana domain.",
            )

    async def _respect_rate_limit(self, seconds: float) -> None:
        async with self._rate_lock:
            now = time.monotonic()
            if self._last_request_monotonic is not None:
                remaining = seconds - (now - self._last_request_monotonic)
                if remaining > 0:
                    await self._sleeper(remaining)
            self._last_request_monotonic = time.monotonic()

    @staticmethod
    def _safe_metadata(response: Any) -> dict[str, str]:
        metadata = {
            name.lower(): value
            for name, value in response.headers.items()
            if name.lower() in _SAFE_RESPONSE_HEADERS
        }
        encoding = getattr(response, "encoding", None)
        if encoding:
            metadata["charset"] = encoding
        return metadata

    @staticmethod
    def _source_date(headers: Any) -> Any:
        value = headers.get("last-modified")
        if not value:
            return None
        try:
            parsed = parsedate_to_datetime(value)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except (TypeError, ValueError, OverflowError):
            return None

    def _failure_result(
        self,
        context: FetchRequestContext,
        *,
        final_url: str,
        http_status: int | None,
        access_status: AccessStatus,
        completeness: AccessCompleteness,
        category: ErrorCategory,
        detail: str,
        elapsed_ms: int,
        metadata: dict[str, str] | None = None,
    ) -> FetchResult:
        self.metrics.consecutive_failures += 1
        self.metrics.degradation_incidents += 1
        self.metrics.total_latency_ms += elapsed_ms
        if self.metrics.consecutive_failures >= self._circuit_failure_threshold:
            self._circuit_open = True
        return FetchResult(
            requested_url=context.requested_url,
            final_url=final_url,
            source_id=context.source_id,
            http_status=http_status,
            fetched_at=utc_now(),
            content_type=None,
            access_status=access_status,
            parser_eligible=False,
            completeness=completeness,
            response_metadata=metadata or {},
            error_category=category,
            error_detail=detail,
            elapsed_ms=elapsed_ms,
            policy_version=context.policy.policy_version,
        )

    async def fetch(self, request_context: FetchRequestContext) -> FetchResult:
        self.validate_policy(request_context.policy)
        self.metrics.attempts += 1
        started = time.monotonic()
        if self._circuit_open:
            return self._failure_result(
                request_context,
                final_url=request_context.requested_url,
                http_status=None,
                access_status=AccessStatus.UNAVAILABLE,
                completeness=AccessCompleteness.UNAVAILABLE,
                category=ErrorCategory.NETWORK,
                detail="The source circuit breaker is open; no request was attempted.",
                elapsed_ms=0,
            )
        try:
            validated = validate_public_url(
                request_context.requested_url,
                allowed_domains=request_context.policy.allowed_domains,
                allowed_schemes=request_context.policy.allowed_schemes,
                resolver=self._resolver,
                resolve_dns=self._resolve_dns,
            )
        except UnsafeURLError as exc:
            return self._failure_result(
                request_context,
                final_url=request_context.requested_url,
                http_status=None,
                access_status=AccessStatus.BLOCKED,
                completeness=AccessCompleteness.BLOCKED,
                category=ErrorCategory.POLICY_BLOCKED,
                detail=str(exc),
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.8",
            "User-Agent": "ProjectTruthMonitor/0.1 (+policy-reviewed monitoring)",
            **dict(request_context.headers),
        }
        if request_context.conditional_etag:
            headers["If-None-Match"] = request_context.conditional_etag
        if request_context.conditional_last_modified:
            headers["If-Modified-Since"] = request_context.conditional_last_modified
        created_client = self._client is None
        if created_client:
            try:
                import httpx
            except ImportError as exc:
                raise RuntimeError("httpx is required for live source collection") from exc
            client = httpx.AsyncClient(
                timeout=request_context.policy.request_timeout_seconds,
                follow_redirects=False,
            )
        else:
            client = self._client
        current_url = validated.normalized_url
        try:
            for redirect_number in range(request_context.policy.max_redirects + 1):
                try:
                    await self._respect_rate_limit(request_context.policy.rate_limit_seconds)
                    too_large = False
                    async with client.stream(
                        "GET", current_url, headers=headers, follow_redirects=False
                    ) as streamed:
                        response = _BufferedResponse(
                            status_code=streamed.status_code,
                            headers={str(key).lower(): str(value) for key, value in streamed.headers.items()},
                            encoding=streamed.encoding,
                        )
                        metadata = self._safe_metadata(response)
                        declared_length = response.headers.get("content-length")
                        if declared_length:
                            try:
                                too_large = int(declared_length) > request_context.policy.max_response_bytes
                            except ValueError:
                                too_large = False
                        chunks: list[bytes] = []
                        size = 0
                        if not too_large and response.status_code not in {301, 302, 303, 304, 307, 308}:
                            async for chunk in streamed.aiter_bytes():
                                size += len(chunk)
                                if size > request_context.policy.max_response_bytes:
                                    too_large = True
                                    chunks.clear()
                                    break
                                chunks.append(chunk)
                        body = b"".join(chunks)
                except Exception as exc:
                    category = ErrorCategory.TIMEOUT if exc.__class__.__name__.lower().endswith("timeout") else ErrorCategory.NETWORK
                    return self._failure_result(
                        request_context,
                        final_url=current_url,
                        http_status=None,
                        access_status=AccessStatus.UNAVAILABLE,
                        completeness=AccessCompleteness.UNAVAILABLE,
                        category=category,
                        detail="The source request failed safely; last verified facts remain unchanged.",
                        elapsed_ms=int((time.monotonic() - started) * 1000),
                    )
                if too_large:
                    return self._failure_result(
                        request_context,
                        final_url=current_url,
                        http_status=response.status_code,
                        access_status=AccessStatus.UNAVAILABLE,
                        completeness=AccessCompleteness.UNAVAILABLE,
                        category=ErrorCategory.RESPONSE_TOO_LARGE,
                        detail="The response exceeded the policy size limit.",
                        elapsed_ms=int((time.monotonic() - started) * 1000),
                        metadata=metadata,
                    )
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location or redirect_number >= request_context.policy.max_redirects:
                        return self._failure_result(
                            request_context,
                            final_url=current_url,
                            http_status=response.status_code,
                            access_status=AccessStatus.BLOCKED,
                            completeness=AccessCompleteness.BLOCKED,
                            category=ErrorCategory.POLICY_BLOCKED,
                            detail="The source redirect chain was invalid or exceeded policy.",
                            elapsed_ms=int((time.monotonic() - started) * 1000),
                            metadata=metadata,
                        )
                    target = urljoin(current_url, location)
                    try:
                        current_url = validate_public_url(
                            target,
                            allowed_domains=request_context.policy.allowed_domains,
                            allowed_schemes=request_context.policy.allowed_schemes,
                            resolver=self._resolver,
                            resolve_dns=self._resolve_dns,
                        ).normalized_url
                    except UnsafeURLError as exc:
                        return self._failure_result(
                            request_context,
                            final_url=current_url,
                            http_status=response.status_code,
                            access_status=AccessStatus.BLOCKED,
                            completeness=AccessCompleteness.BLOCKED,
                            category=ErrorCategory.POLICY_BLOCKED,
                            detail=f"Unsafe redirect blocked: {exc}",
                            elapsed_ms=int((time.monotonic() - started) * 1000),
                            metadata=metadata,
                        )
                    continue
                lower_sample = body[:250_000].decode("utf-8", errors="ignore").casefold()
                elapsed_ms = int((time.monotonic() - started) * 1000)
                if response.status_code == 304:
                    result = FetchResult(
                        requested_url=request_context.requested_url,
                        final_url=current_url,
                        source_id=request_context.source_id,
                        http_status=304,
                        fetched_at=utc_now(),
                        content_type=response.headers.get("content-type"),
                        access_status=AccessStatus.SUCCESS,
                        parser_eligible=False,
                        completeness=AccessCompleteness.COMPLETE,
                        response_metadata={**metadata, "not_modified": "true"},
                        elapsed_ms=elapsed_ms,
                        policy_version=request_context.policy.policy_version,
                    )
                    self.metrics.successes += 1
                    self.metrics.consecutive_failures = 0
                    self.metrics.last_successful_fetch = result.fetched_at
                    return result
                if response.status_code == 429:
                    return self._failure_result(
                        request_context,
                        final_url=current_url,
                        http_status=429,
                        access_status=AccessStatus.RATE_LIMITED,
                        completeness=AccessCompleteness.UNAVAILABLE,
                        category=ErrorCategory.RATE_LIMITED,
                        detail="The source rate limit was reached; no data was changed.",
                        elapsed_ms=elapsed_ms,
                        metadata=metadata,
                    )
                if any(marker in lower_sample for marker in _CAPTCHA_MARKERS):
                    return self._failure_result(
                        request_context,
                        final_url=current_url,
                        http_status=response.status_code,
                        access_status=AccessStatus.CAPTCHA,
                        completeness=AccessCompleteness.BLOCKED,
                        category=ErrorCategory.CAPTCHA,
                        detail="A CAPTCHA or human-verification control blocked automatic access.",
                        elapsed_ms=elapsed_ms,
                        metadata=metadata,
                    )
                if response.status_code == 401 or any(marker in lower_sample for marker in _AUTH_MARKERS):
                    return self._failure_result(
                        request_context,
                        final_url=current_url,
                        http_status=response.status_code,
                        access_status=AccessStatus.AUTHENTICATION_REQUIRED,
                        completeness=AccessCompleteness.BLOCKED,
                        category=ErrorCategory.AUTHENTICATION_REQUIRED,
                        detail="The source requires authentication; no bypass was attempted.",
                        elapsed_ms=elapsed_ms,
                        metadata=metadata,
                    )
                if response.status_code == 403:
                    return self._failure_result(
                        request_context,
                        final_url=current_url,
                        http_status=403,
                        access_status=AccessStatus.BLOCKED,
                        completeness=AccessCompleteness.BLOCKED,
                        category=ErrorCategory.ACCESS_RESTRICTED,
                        detail="The source denied access; no bypass was attempted.",
                        elapsed_ms=elapsed_ms,
                        metadata=metadata,
                    )
                if response.status_code == 404:
                    return self._failure_result(
                        request_context,
                        final_url=current_url,
                        http_status=404,
                        access_status=AccessStatus.UNAVAILABLE,
                        completeness=AccessCompleteness.UNAVAILABLE,
                        category=ErrorCategory.NOT_FOUND,
                        detail="The source page was not found; last verified facts remain unchanged.",
                        elapsed_ms=elapsed_ms,
                        metadata=metadata,
                    )
                if response.status_code >= 400:
                    return self._failure_result(
                        request_context,
                        final_url=current_url,
                        http_status=response.status_code,
                        access_status=AccessStatus.UNAVAILABLE,
                        completeness=AccessCompleteness.UNAVAILABLE,
                        category=ErrorCategory.NETWORK,
                        detail="The source returned an error; last verified facts remain unchanged.",
                        elapsed_ms=elapsed_ms,
                        metadata=metadata,
                    )
                content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                parser_eligible = content_type in {
                    "text/html",
                    "application/xhtml+xml",
                    "application/pdf",
                } or body.startswith(b"%PDF-") or b"<html" in body[:2_000].lower()
                if not parser_eligible:
                    return self._failure_result(
                        request_context,
                        final_url=current_url,
                        http_status=response.status_code,
                        access_status=AccessStatus.PARTIAL,
                        completeness=AccessCompleteness.PARTIAL,
                        category=ErrorCategory.INVALID_RESPONSE,
                        detail="The source response type is not eligible for a configured parser.",
                        elapsed_ms=elapsed_ms,
                        metadata=metadata,
                    )
                result = FetchResult(
                    requested_url=request_context.requested_url,
                    final_url=current_url,
                    source_id=request_context.source_id,
                    http_status=response.status_code,
                    fetched_at=utc_now(),
                    source_reported_date=self._source_date(response.headers),
                    content_type=content_type,
                    content=body,
                    access_status=AccessStatus.SUCCESS,
                    parser_eligible=True,
                    completeness=AccessCompleteness.COMPLETE,
                    response_metadata=metadata,
                    elapsed_ms=elapsed_ms,
                    policy_version=request_context.policy.policy_version,
                )
                self.metrics.successes += 1
                self.metrics.parser_eligible_fetches += 1
                self.metrics.total_latency_ms += elapsed_ms
                self.metrics.consecutive_failures = 0
                self.metrics.last_successful_fetch = result.fetched_at
                self.metrics.last_content_hash = result.content_hash
                return result
            raise AssertionError("redirect loop should always return")
        finally:
            if created_client:
                await client.aclose()

    def health_check(self) -> SourceHealth:
        if self._circuit_open:
            status = SourceHealthStatus.CIRCUIT_OPEN
        elif self.metrics.consecutive_failures:
            status = SourceHealthStatus.DEGRADED
        else:
            status = SourceHealthStatus.HEALTHY
        return SourceHealth(
            source_id=self.source_id,
            status=status,
            last_successful_fetch=self.metrics.last_successful_fetch,
            fetch_success_rate=self.metrics.success_rate,
            average_latency_ms=self.metrics.average_latency_ms,
            consecutive_failures=self.metrics.consecutive_failures,
            circuit_breaker_status="open" if self._circuit_open else "closed",
            last_content_hash=self.metrics.last_content_hash,
            degradation_incidents=self.metrics.degradation_incidents,
        )

    def reset_circuit(self) -> None:
        """Operator action after a reviewed policy/access/layout change."""

        self._circuit_open = False
        self.metrics.consecutive_failures = 0


HRERACollector = HRERAGurugramCollector
