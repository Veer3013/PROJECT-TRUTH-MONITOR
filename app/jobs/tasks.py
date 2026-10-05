from __future__ import annotations

from dataclasses import dataclass

from app.collectors.base import SourceCollector
from app.domain import ErrorCategory, FetchRequestContext, FetchResult, ParsedSourceSnapshot
from app.parsers.base import LayoutChangeError, ParserError, SourceParser
from app.services.reliability import CircuitBreaker, RetryPolicy, retry_fetch
from app.services.scheduler import ActiveJobGate


@dataclass(frozen=True, slots=True)
class MonitoringTaskResult:
    fetch_result: FetchResult | None
    parsed_snapshot: ParsedSourceSnapshot | None
    attempts: int
    status: str
    error_category: ErrorCategory | None
    dead_letter_review: bool
    message: str | None = None


async def run_monitoring_task(
    *,
    job_key: str,
    context: FetchRequestContext,
    collector: SourceCollector,
    parser: SourceParser,
    active_jobs: ActiveJobGate,
    circuit_breaker: CircuitBreaker,
    retry_policy: RetryPolicy = RetryPolicy(),
) -> MonitoringTaskResult:
    if not active_jobs.start(job_key):
        return MonitoringTaskResult(None, None, 0, "duplicate_or_cancelled", None, False)
    try:
        if not circuit_breaker.allow_request():
            return MonitoringTaskResult(
                None,
                None,
                0,
                "circuit_open",
                ErrorCategory.NETWORK,
                True,
                "The source circuit is open; no factual alert was generated.",
            )
        outcome = await retry_fetch(lambda: collector.fetch(context), policy=retry_policy)
        fetch = outcome.value
        if not fetch.is_successful:
            circuit_breaker.record_failure()
            permanent = fetch.error_category in {
                ErrorCategory.UNSUPPORTED_URL,
                ErrorCategory.POLICY_BLOCKED,
                ErrorCategory.AUTHENTICATION_REQUIRED,
                ErrorCategory.CAPTCHA,
                ErrorCategory.ACCESS_RESTRICTED,
            }
            return MonitoringTaskResult(
                fetch,
                None,
                outcome.attempts,
                "blocked" if permanent else "failed",
                fetch.error_category,
                True,
                "Last verified facts remain unchanged.",
            )
        if fetch.http_status == 304:
            circuit_breaker.record_success()
            return MonitoringTaskResult(fetch, None, outcome.attempts, "not_modified", None, False)
        if not fetch.parser_eligible or not parser.can_parse(fetch):
            circuit_breaker.record_failure()
            return MonitoringTaskResult(
                fetch,
                None,
                outcome.attempts,
                "parser_ineligible",
                ErrorCategory.PARSER_FAILED,
                True,
                "No factual alert was generated.",
            )
        try:
            parsed = parser.parse(fetch)
        except LayoutChangeError as exc:
            circuit_breaker.record_failure()
            return MonitoringTaskResult(
                fetch,
                None,
                outcome.attempts,
                "layout_changed",
                ErrorCategory.LAYOUT_CHANGED,
                True,
                str(exc),
            )
        except ParserError as exc:
            circuit_breaker.record_failure()
            category = ErrorCategory.PDF_UNREADABLE if exc.code in {"encrypted_pdf", "corrupted_pdf"} else ErrorCategory.PARSER_FAILED
            return MonitoringTaskResult(fetch, None, outcome.attempts, "parse_failed", category, True, str(exc))
        if not parsed.is_usable:
            circuit_breaker.record_failure()
            return MonitoringTaskResult(
                fetch,
                parsed,
                outcome.attempts,
                "parse_needs_review",
                ErrorCategory.PARSER_FAILED,
                True,
                "Parsed output is not reliable enough for factual alerts.",
            )
        circuit_breaker.record_success()
        return MonitoringTaskResult(fetch, parsed, outcome.attempts, "succeeded", None, False)
    finally:
        active_jobs.finish(job_key)


class MonitoringTaskRunner:
    def __init__(
        self,
        *,
        active_jobs: ActiveJobGate | None = None,
        circuit_breaker: CircuitBreaker | None = None,
        retry_policy: RetryPolicy = RetryPolicy(),
    ) -> None:
        self.active_jobs = active_jobs or ActiveJobGate()
        self.circuit_breaker = circuit_breaker or CircuitBreaker()
        self.retry_policy = retry_policy

    async def run(
        self,
        *,
        job_key: str,
        context: FetchRequestContext,
        collector: SourceCollector,
        parser: SourceParser,
    ) -> MonitoringTaskResult:
        return await run_monitoring_task(
            job_key=job_key,
            context=context,
            collector=collector,
            parser=parser,
            active_jobs=self.active_jobs,
            circuit_breaker=self.circuit_breaker,
            retry_policy=self.retry_policy,
        )

