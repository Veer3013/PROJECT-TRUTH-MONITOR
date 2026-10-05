from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import quote

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


def sanitize_upload_filename(filename: str | None) -> str:
    base = Path(filename or "document.pdf").name
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._")
    return (safe or "document.pdf")[:120]


class UserUploadedDocumentCollector(SourceCollector):
    collector_name = "user_uploaded_document"

    def __init__(self, *, source_id: str = "user-upload") -> None:
        self.source_id = source_id
        self.metrics = CollectorMetrics()

    def can_handle(self, source: object) -> bool:
        url = str(getattr(source, "requested_url", None) or getattr(source, "url", None) or source)
        return url.startswith("upload://") or getattr(source, "upload_content", None) is not None

    def validate_policy(self, source_policy: SourceAccessPolicy) -> None:
        try:
            validate_policy_for_mode(source_policy, "upload")
        except SourcePolicyError as exc:
            raise CollectorPolicyError(exc.code, str(exc)) from exc

    async def fetch(self, request_context: FetchRequestContext) -> FetchResult:
        self.validate_policy(request_context.policy)
        self.metrics.attempts += 1
        filename = sanitize_upload_filename(request_context.upload_filename)
        safe_ref = f"upload://{quote(filename)}"
        content = request_context.upload_content
        if not request_context.authorization_attested:
            self.metrics.consecutive_failures += 1
            return FetchResult(
                requested_url=safe_ref,
                final_url=safe_ref,
                source_id=request_context.source_id,
                http_status=None,
                fetched_at=utc_now(),
                content_type="application/pdf",
                access_status=AccessStatus.BLOCKED,
                parser_eligible=False,
                completeness=AccessCompleteness.BLOCKED,
                error_category=ErrorCategory.POLICY_BLOCKED,
                error_detail="Rights or authorization attestation is required for uploaded documents.",
                policy_version=request_context.policy.policy_version,
            )
        if not content or not content.startswith(b"%PDF-"):
            self.metrics.consecutive_failures += 1
            return FetchResult(
                requested_url=safe_ref,
                final_url=safe_ref,
                source_id=request_context.source_id,
                http_status=None,
                fetched_at=utc_now(),
                content_type="application/pdf",
                access_status=AccessStatus.ERROR,
                parser_eligible=False,
                completeness=AccessCompleteness.UNAVAILABLE,
                error_category=ErrorCategory.PDF_UNREADABLE,
                error_detail="The upload is not a valid PDF by file signature.",
                policy_version=request_context.policy.policy_version,
            )
        if len(content) > request_context.policy.max_response_bytes:
            self.metrics.consecutive_failures += 1
            return FetchResult(
                requested_url=safe_ref,
                final_url=safe_ref,
                source_id=request_context.source_id,
                http_status=None,
                fetched_at=utc_now(),
                content_type="application/pdf",
                access_status=AccessStatus.ERROR,
                parser_eligible=False,
                completeness=AccessCompleteness.UNAVAILABLE,
                error_category=ErrorCategory.RESPONSE_TOO_LARGE,
                error_detail="The PDF exceeds the configured upload size limit.",
                policy_version=request_context.policy.policy_version,
            )
        result = FetchResult(
            requested_url=safe_ref,
            final_url=safe_ref,
            source_id=request_context.source_id,
            http_status=200,
            fetched_at=utc_now(),
            content_type="application/pdf",
            content=content,
            access_status=AccessStatus.SUCCESS,
            parser_eligible=True,
            completeness=AccessCompleteness.COMPLETE,
            response_metadata={"safe_filename": filename, "size_bytes": len(content)},
            policy_version=request_context.policy.policy_version,
        )
        self.metrics.successes += 1
        self.metrics.parser_eligible_fetches += 1
        self.metrics.consecutive_failures = 0
        self.metrics.last_successful_fetch = result.fetched_at
        self.metrics.last_content_hash = result.content_hash
        return result

    def health_check(self) -> SourceHealth:
        return SourceHealth(
            source_id=self.source_id,
            status=SourceHealthStatus.HEALTHY,
            last_successful_fetch=self.metrics.last_successful_fetch,
            fetch_success_rate=self.metrics.success_rate,
            consecutive_failures=self.metrics.consecutive_failures,
            last_content_hash=self.metrics.last_content_hash,
            detail="User-authorized PDF ingestion is available.",
        )


UploadedDocumentCollector = UserUploadedDocumentCollector

