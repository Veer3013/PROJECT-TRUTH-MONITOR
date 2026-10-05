from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.collectors import SourceCollector
from app.domain import (
    ErrorCategory,
    FetchResult,
    ParsedSourceSnapshot,
    SourceAccessPolicy,
    SourceClass,
)
from app.domain.changes import DetectedChange
from app.domain.signals import Signal
from app.parsers import LayoutChangeError, ParserError, SourceParser
from app.services.diff import DeterministicDiffEngine
from app.services.evidence import EvidenceBuilder, EvidenceCard
from app.services.reliability import RetryPolicy, retry_fetch
from app.services.signals import DeterministicSignalEngine
from app.services.snapshots import ImmutableSnapshot, SnapshotService


@dataclass(frozen=True, slots=True)
class MonitoringPipelineResult:
    status: str
    fetch_result: FetchResult | None
    parsed_snapshot: ParsedSourceSnapshot | None
    captured_snapshot: ImmutableSnapshot | None
    changes: tuple[DetectedChange, ...]
    signals: tuple[Signal, ...]
    evidence_cards: tuple[EvidenceCard, ...]
    attempts: int
    error_category: ErrorCategory | None = None
    message: str | None = None

    @property
    def factual_alerts_suppressed(self) -> bool:
        return self.status in {
            "source_incomplete",
            "layout_changed",
            "parse_failed",
            "parse_unusable",
            "parser_incompatible",
        }


class MonitoringPipeline:
    """Framework-neutral source-to-evidence orchestration.

    Database runs/audits remain the caller's transaction responsibility. This
    service owns deterministic ordering and the rule that failed access never
    replaces the last verified facts.
    """

    def __init__(
        self,
        snapshot_service: SnapshotService,
        *,
        diff_engine: DeterministicDiffEngine | None = None,
        signal_engine: DeterministicSignalEngine | None = None,
        evidence_builder: EvidenceBuilder | None = None,
        retry_policy: RetryPolicy = RetryPolicy(),
    ) -> None:
        self.snapshot_service = snapshot_service
        self.diff_engine = diff_engine or DeterministicDiffEngine()
        self.signal_engine = signal_engine or DeterministicSignalEngine()
        self.evidence_builder = evidence_builder or EvidenceBuilder()
        self.retry_policy = retry_policy

    async def run(
        self,
        *,
        project_id: str,
        monitoring_run_id: str,
        request_context: Any,
        collector: SourceCollector,
        parser: SourceParser,
        policy: SourceAccessPolicy,
        source_class: SourceClass,
        source_url_permitted: bool = True,
    ) -> MonitoringPipelineResult:
        previous = self.snapshot_service.repository.latest(
            project_id, request_context.source_id, verified_only=True
        )
        fetch_outcome = await retry_fetch(
            lambda: collector.fetch(request_context), policy=self.retry_policy
        )
        fetch = fetch_outcome.value
        if not fetch.is_successful:
            changes: tuple[DetectedChange, ...] = ()
            signals: tuple[Signal, ...] = ()
            if previous is not None:
                degradation = self.diff_engine.source_degradation(
                    previous,
                    fetch,
                    monitoring_reference=f"run:{monitoring_run_id}",
                )
                changes = (degradation,)
                signals = self.signal_engine.generate(changes)
            return MonitoringPipelineResult(
                "source_incomplete",
                fetch,
                None,
                None,
                changes,
                signals,
                (),
                fetch_outcome.attempts,
                fetch.error_category,
                "Last verified facts remain visible; this check is not evidence of no change.",
            )
        if fetch.http_status == 304:
            return MonitoringPipelineResult(
                "not_modified", fetch, None, previous, (), (), (), fetch_outcome.attempts
            )
        try:
            parsed = parser.parse(fetch)
        except LayoutChangeError as exc:
            return MonitoringPipelineResult(
                "layout_changed",
                fetch,
                None,
                None,
                (),
                (),
                (),
                fetch_outcome.attempts,
                ErrorCategory.LAYOUT_CHANGED,
                str(exc),
            )
        except ParserError as exc:
            return MonitoringPipelineResult(
                "parse_failed",
                fetch,
                None,
                None,
                (),
                (),
                (),
                fetch_outcome.attempts,
                ErrorCategory.PDF_UNREADABLE if exc.code in {"encrypted_pdf", "corrupted_pdf"} else ErrorCategory.PARSER_FAILED,
                str(exc),
            )
        if not parsed.is_usable:
            return MonitoringPipelineResult(
                "parse_unusable",
                fetch,
                parsed,
                None,
                (),
                (),
                (),
                fetch_outcome.attempts,
                ErrorCategory.PARSER_FAILED,
                "Parser validation did not produce usable source facts; last verified facts remain unchanged.",
            )
        capture = self.snapshot_service.capture(
            project_id=project_id,
            monitoring_run_id=monitoring_run_id,
            fetch_result=fetch,
            parsed=parsed,
            policy=policy,
        )
        current = capture.snapshot
        if current is None:
            return MonitoringPipelineResult(
                "parse_unusable",
                fetch,
                parsed,
                None,
                (),
                (),
                (),
                fetch_outcome.attempts,
                ErrorCategory.PARSER_FAILED,
                capture.reason,
            )
        if previous is None:
            return MonitoringPipelineResult(
                "baseline_captured", fetch, parsed, current, (), (), (), fetch_outcome.attempts
            )
        history = self.snapshot_service.repository.history(project_id, request_context.source_id)
        prior_history = tuple(item for item in history if item.id not in {previous.id, current.id})
        changes = self.diff_engine.compare(
            previous,
            current,
            historical_snapshots=prior_history,
        )
        signals = self.signal_engine.generate(changes)
        cards: list[EvidenceCard] = []
        for change in changes:
            if change.field_key in {"__source__"}:
                continue
            cards.append(
                self.evidence_builder.build(
                    change,
                    old_snapshot=previous,
                    new_snapshot=current,
                    source_class=source_class,
                    source_url=fetch.final_url,
                    source_url_permitted=source_url_permitted,
                    source_date=fetch.source_reported_date,
                    captured_at=fetch.fetched_at,
                )
            )
        status = (
            "parser_incompatible"
            if any(change.change_type.value == "parser_version_incompatible" for change in changes)
            else "partial"
            if not current.is_verified
            else "changed"
            if changes
            else "no_change"
        )
        return MonitoringPipelineResult(
            status,
            fetch,
            parsed,
            current,
            changes,
            signals,
            tuple(cards),
            fetch_outcome.attempts,
        )
