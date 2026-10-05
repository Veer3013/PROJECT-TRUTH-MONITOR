from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select

from app.api.deps import AnalystActor, CSRFProtected, CurrentActor, DBSession
from app.api.utils import add_audit_event, commit_or_rollback
from app.api.v1.sources import get_tenant_source
from app.collectors.base import CollectorError, CollectorPolicyError
from app.collectors.developer_page import DeveloperPageCollector
from app.collectors.fixtures import FixtureCollector
from app.collectors.hrera_gurugram import HRERAGurugramCollector
from app.core.config import ROOT_DIR, get_settings
from app.core.errors import USER_MESSAGES
from app.core.rate_limit import rate_limiter
from app.domain import (
    ChangeType,
    ReviewState,
    Severity,
    SourceClass,
)
from app.domain import SourceAccessPolicy as DomainSourceAccessPolicy
from app.domain.changes import DetectedChange as DomainDetectedChange
from app.domain.source import FetchRequestContext
from app.jobs.tasks import MonitoringTaskResult, run_monitoring_task
from app.models import (
    Alert,
    ChangeEvidence,
    DetectedChange,
    MonitoringJob,
    MonitoringRun,
    Project,
    ProjectSourceLink,
    Signal,
    SnapshotField,
    Source,
    SourceAccessPolicy,
    SourceSnapshot,
    WatchlistItem,
)
from app.parsers.fixture_html import FixtureHTMLParser
from app.parsers.hrera_html import HRERAHTMLParser
from app.parsers.pdf_text import PDFTextParser
from app.schemas.sources import MonitoringRunResponse, RefreshRequest
from app.services.conflicts import (
    FactObservation,
    SourceConflictRecord,
    detect_source_conflicts,
    persist_source_conflicts,
)
from app.services.diff import detect_changes, persist_detected_changes
from app.services.impact_router import suggested_review_actions
from app.services.normalization import (
    parse_boolean,
    parse_date,
    registration_identity_key,
)
from app.services.reliability import CircuitBreaker, CircuitState
from app.services.scheduler import ActiveJobGate
from app.services.signals import generate_signals

router = APIRouter(tags=["Monitoring"])
logger = logging.getLogger("projecttruth.monitoring")

_ACTIVE_MONITORING_JOBS = ActiveJobGate()
_SOURCE_CIRCUITS: dict[str, CircuitBreaker] = {}
_SOURCE_COLLECTORS: dict[str, Any] = {}


def _circuit_for(source: Source) -> CircuitBreaker:
    """Return the process-local breaker, seeded from persistent source health."""

    key = f"{source.organization_id}:{source.id}"
    breaker = _SOURCE_CIRCUITS.get(key)
    if breaker is None:
        breaker = CircuitBreaker()
        for _ in range(max(0, source.consecutive_failures)):
            breaker.record_failure()
        _SOURCE_CIRCUITS[key] = breaker
    elif source.consecutive_failures == 0 and source.circuit_breaker_state == "closed":
        if breaker.state != CircuitState.CLOSED:
            breaker.reset_after_review()
    elif source.consecutive_failures > breaker.consecutive_failures:
        for _ in range(source.consecutive_failures - breaker.consecutive_failures):
            breaker.record_failure()
    return breaker


def _sync_source_circuit(source: Source, breaker: CircuitBreaker) -> None:
    source.consecutive_failures = breaker.consecutive_failures
    source.circuit_breaker_state = breaker.state.value


def _json_value(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_json_value(item) for item in value]
    return value


def _text_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (dict, list, tuple, set, frozenset)):
        return json.dumps(_json_value(value), sort_keys=True)
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _domain_policy(source: Source, policy: SourceAccessPolicy) -> DomainSourceAccessPolicy:
    settings = get_settings()
    try:
        source_class = SourceClass(source.source_class)
    except ValueError:
        source_class = SourceClass.UNKNOWN
    modes = frozenset(policy.permitted_access_modes)
    is_fixture = "fixture" in modes
    return DomainSourceAccessPolicy(
        source_name=source.name,
        source_type=source.source_type,
        base_url=source.base_url or "",
        allowed_domains=() if is_fixture else (source.domain,),
        enabled=policy.is_enabled and source.is_enabled,
        permitted_access_modes=modes,
        rate_limit_seconds=policy.rate_limit_period_seconds,
        robots_review_status=policy.robots_review_status,
        terms_review_status=policy.terms_review_status,
        evidence_retention_policy=f"{policy.evidence_retention_days}_days",
        may_store_raw_content=policy.raw_content_may_be_stored,
        playwright_allowed=policy.playwright_allowed,
        ocr_allowed=policy.ocr_allowed,
        llm_processing_allowed=policy.llm_allowed,
        policy_version=policy.policy_version,
        reviewer=policy.reviewer_user_id,
        reviewed_at=policy.reviewed_at,
        max_response_bytes=settings.source_max_response_mb * 1024 * 1024,
        request_timeout_seconds=settings.source_request_timeout,
        allowed_schemes=frozenset({"https", "http"}) if not is_fixture else frozenset({"fixture"}),
        source_class=source_class,
    )


def _fixture_map() -> tuple[dict[str, Path], Path]:
    roots = (
        (ROOT_DIR / "app" / "fixtures" / "html").resolve(),
        (ROOT_DIR / "tests" / "fixtures" / "html").resolve(),
    )
    for root in roots:
        paths = tuple(sorted(root.glob("*.html"))) if root.is_dir() else ()
        if paths:
            fixtures: dict[str, Path] = {}
            for path in paths:
                fixtures[path.stem] = path
                fixtures[path.name] = path
            return fixtures, root
    return {}, roots[0]


def _parser_for(source: Source, content_type: str | None):
    if "pdf" in (content_type or "").lower():
        return PDFTextParser()
    if source.source_type == "hrera_gurugram":
        return HRERAHTMLParser()
    return FixtureHTMLParser()


class _ParserRouter:
    """Select a deterministic parser after the response content type is known."""

    def __init__(self, source: Source) -> None:
        self.source = source

    def can_parse(self, fetch: Any) -> bool:
        return _parser_for(self.source, fetch.content_type).can_parse(fetch)

    def parse(self, fetch: Any) -> Any:
        return _parser_for(self.source, fetch.content_type).parse(fetch)


def _collector_for(source: Source):
    key = f"{getattr(source, 'organization_id', '')}:{source.id}:{source.source_type}"
    cached = _SOURCE_COLLECTORS.get(key)
    if cached is not None:
        return cached
    if source.source_type == "fixture":
        fixtures, fixture_root = _fixture_map()
        collector = FixtureCollector(
            fixtures,
            source_id=source.id,
            fixture_root=fixture_root,
        )
    elif source.source_type == "hrera_gurugram":
        if not get_settings().enable_live_hrera:
            raise CollectorPolicyError(
                "live_hrera_disabled",
                "Live HRERA collection is disabled. No factual data was changed.",
            )
        collector = HRERAGurugramCollector(source_id=source.id)
    elif source.source_type == "developer_page":
        collector = DeveloperPageCollector(source_id=source.id)
    else:
        raise CollectorPolicyError(
            "unsupported_source_type",
            f"{USER_MESSAGES['unsupported_url']} No factual data was changed.",
        )
    _SOURCE_COLLECTORS[key] = collector
    return collector


def _finish_failed_run(
    db: DBSession,
    *,
    run: MonitoringRun,
    source: Source,
    category: str,
    completeness: str,
    message: str,
    breaker: CircuitBreaker | None = None,
    attempts: int = 0,
) -> None:
    run.status = "failed"
    run.completed_at = datetime.now(UTC)
    run.error_category = category
    run.error_message = message
    run.access_completeness = completeness
    run.fetched_count = attempts
    if breaker is None:
        source.consecutive_failures += 1
    else:
        _sync_source_circuit(source, breaker)
    source.health_status = "degraded"
    if source.consecutive_failures >= 5:
        source.circuit_breaker_state = "open"
    logger.warning(
        "monitoring_failed category=%s circuit=%s",
        category,
        source.circuit_breaker_state,
        extra={
            "request_id": run.run_metadata.get("request_id"),
            "job_id": run.monitoring_job_id or run.id,
            "source_id": source.id,
        },
    )
    dedup = hashlib.sha256(f"{source.id}:{run.project_id}:{category}".encode()).hexdigest()
    signal = db.scalar(
        select(Signal).where(
            Signal.organization_id == run.organization_id,
            Signal.deduplication_key == dedup,
        )
    )
    if signal is None and run.project_id:
        signal = Signal(
            organization_id=run.organization_id,
            project_id=run.project_id,
            rule_code=(
                "SOURCE_UNAVAILABLE"
                if category
                in {"network", "timeout", "rate_limited", "captcha", "access_restricted"}
                else "SOURCE_MONITORING_DEGRADED"
            ),
            rule_version="2026.08.1",
            supporting_change_ids=[],
            observed_facts={"error_category": category, "monitoring_run_id": run.id},
            severity="high",
            confidence=1.0,
            confidence_dimensions={"source_access_completeness": completeness},
            limitations=["Last verified facts remain visible; this is not evidence of no change."],
            required_review_level="reviewer",
            state="active",
            deduplication_key=dedup,
        )
        db.add(signal)
        db.flush()
        db.add(
            Alert(
                organization_id=run.organization_id,
                project_id=run.project_id,
                signal_id=signal.id,
                source_id=source.id,
                title="Source monitoring incomplete",
                neutral_description=message,
                state="open",
                severity="high",
                required_review_level="reviewer",
                requires_review=True,
                source_class=source.source_class,
                freshness="last_verified_preserved",
                limitation="The source could not be completely verified.",
                suggested_review_action="Review source access or upload an authorized copy.",
                deduplication_key=f"alert:{dedup}",
            )
        )


def _task_failure_values(result: MonitoringTaskResult) -> tuple[str, str, str]:
    category = result.error_category.value if result.error_category else result.status
    completeness = (
        result.fetch_result.completeness.value
        if result.fetch_result is not None
        else "unavailable"
    )
    message = result.message or USER_MESSAGES.get(
        category,
        "The source could not be checked yet. Last verified facts remain visible with its date.",
    )
    return category, completeness, message


def _log_monitoring_success(
    run: MonitoringRun,
    source: Source,
    *,
    outcome: str,
    snapshot_id: str | None,
) -> None:
    logger.info(
        "monitoring_succeeded outcome=%s",
        outcome,
        extra={
            "request_id": run.run_metadata.get("request_id"),
            "job_id": run.monitoring_job_id or run.id,
            "source_id": source.id,
            "snapshot_id": snapshot_id,
        },
    )


def _update_project_cache(project: Project, facts: dict[str, Any], fetched_at: datetime) -> None:
    if "current_status" in facts:
        project.current_status = _text_value(facts["current_status"])
    if "proposed_completion_date" in facts:
        project.proposed_completion_date = parse_date(facts["proposed_completion_date"])
    if "revised_completion_date" in facts:
        project.revised_completion_date = parse_date(facts["revised_completion_date"])
    if "lapsed_flag" in facts:
        project.lapsed_flag = parse_boolean(facts["lapsed_flag"])
    if "qpr_missing_flag" in facts:
        project.qpr_missing_flag = parse_boolean(facts["qpr_missing_flag"])
    project.last_verified_at = fetched_at


def _persist_snapshot_fields(
    db: DBSession,
    *,
    organization_id: str,
    snapshot: SourceSnapshot,
    parsed: Any,
) -> None:
    occurrences: dict[str, int] = {}
    for field in parsed.fields:
        index = occurrences.get(field.canonical_field_key, 0)
        occurrences[field.canonical_field_key] = index + 1
        value = field.normalized_value
        normalized_number = (
            float(value)
            if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)
            else None
        )
        normalized_date = value if isinstance(value, date) and not isinstance(value, datetime) else None
        db.add(
            SnapshotField(
                organization_id=organization_id,
                snapshot_id=snapshot.id,
                field_key=field.canonical_field_key,
                occurrence_index=index,
                raw_label=field.raw_label,
                raw_value=field.raw_value,
                canonical_value=_text_value(value),
                normalized_text=_text_value(value),
                normalized_number=normalized_number,
                normalized_date=normalized_date,
                value_type=(
                    "date"
                    if normalized_date
                    else "number"
                    if normalized_number is not None
                    else "boolean"
                    if isinstance(value, bool)
                    else "text"
                ),
                unit=field.unit,
                selector=field.selector,
                page_number=field.page,
                coordinates={"box": list(field.coordinates)} if field.coordinates else None,
                extraction_method=field.extraction_method,
                extraction_confidence=field.extraction_confidence,
                parser_name=field.parser_name,
                parser_version=field.parser_version,
                validation_warnings=list(field.validation_warnings),
            )
        )


def _persist_latest_source_conflicts(
    db: DBSession,
    *,
    organization_id: str,
    project_id: str,
) -> tuple[tuple[Any, ...], tuple[SourceConflictRecord, ...]]:
    """Compare the latest valid capture from every configured source."""

    snapshots = list(
        db.scalars(
            select(SourceSnapshot)
            .where(
                SourceSnapshot.organization_id == organization_id,
                SourceSnapshot.project_id == project_id,
                SourceSnapshot.validation_status == "valid",
            )
            .order_by(SourceSnapshot.fetched_at.desc(), SourceSnapshot.created_at.desc())
        )
    )
    latest_by_source: dict[str, SourceSnapshot] = {}
    for snapshot in snapshots:
        latest_by_source.setdefault(snapshot.source_id, snapshot)
    if len(latest_by_source) < 2:
        return (), ()

    sources = {
        source.id: source
        for source in db.scalars(
            select(Source).where(
                Source.organization_id == organization_id,
                Source.id.in_(latest_by_source),
            )
        )
    }
    observations: list[FactObservation] = []
    for source_id, snapshot in latest_by_source.items():
        source = sources.get(source_id)
        if source is None:
            continue
        try:
            source_class = SourceClass(source.source_class)
        except ValueError:
            source_class = SourceClass.UNKNOWN
        for field_key, value in (snapshot.structured_facts or {}).items():
            observations.append(
                FactObservation(
                    project_id=project_id,
                    field_key=str(field_key),
                    value=value,
                    source_id=source.id,
                    source_class=source_class,
                    snapshot_id=snapshot.id,
                    captured_at=snapshot.fetched_at,
                    source_date=snapshot.source_date,
                    evidence_reference=f"snapshot:{snapshot.id}",
                )
            )
    conflicts = detect_source_conflicts(observations)
    return (
        persist_source_conflicts(
            db,
            organization_id=organization_id,
            conflicts=conflicts,
        ),
        conflicts,
    )


def _source_conflict_changes(
    conflicts: tuple[SourceConflictRecord, ...],
) -> tuple[DomainDetectedChange, ...]:
    changes: list[DomainDetectedChange] = []
    for conflict in conflicts:
        if conflict.reason_code not in {
            "official_vs_developer",
            "official_vs_marketing",
        }:
            continue
        primary = conflict.display_precedence
        secondary = next(
            (
                observation
                for observation in conflict.observations
                if primary is not None and observation.value != primary.value
            ),
            None,
        )
        if (
            primary is None
            or secondary is None
            or primary.snapshot_id is None
            or secondary.snapshot_id is None
            or primary.snapshot_id == secondary.snapshot_id
        ):
            continue
        deduplication_key = hashlib.sha256(
            f"source-conflict-change:{conflict.deduplication_key}".encode()
        ).hexdigest()
        extraction_confidence = min(
            primary.extraction_confidence or 1.0,
            secondary.extraction_confidence or 1.0,
        )
        changes.append(
            DomainDetectedChange(
                project_id=conflict.project_id,
                field_key=conflict.field_key,
                old_snapshot_id=primary.snapshot_id,
                new_snapshot_id=secondary.snapshot_id,
                old_raw_value=primary.raw_value or _text_value(primary.value),
                old_canonical_value=primary.value,
                new_raw_value=secondary.raw_value or _text_value(secondary.value),
                new_canonical_value=secondary.value,
                change_type=ChangeType.SOURCE_CONFLICT,
                magnitude=None,
                confidence_dimensions={
                    "source_reliability": "high",
                    "source_access_completeness": "complete",
                    "identity_confidence": "confirmed",
                    "extraction_confidence": extraction_confidence,
                    "change_confidence": extraction_confidence,
                    "freshness": "current",
                    "conflict_state": "open",
                },
                severity=Severity.HIGH if conflict.is_material else Severity.MEDIUM,
                reason_codes=(conflict.reason_code, "source_values_disagree"),
                requires_review=True,
                deduplication_key=deduplication_key,
                review_state=ReviewState.PENDING,
                change_id=deduplication_key[:32],
                source_id=secondary.source_id,
                limitations=(
                    "Both source values are preserved; a reviewer must select or resolve the value.",
                ),
            )
        )
    return tuple(changes)


def _signal_title(code: str) -> tuple[str, str]:
    titles = {
        "COMPLETION_DATE_EXTENDED": (
            "Completion date changed",
            "The captured official completion date moved later.",
        ),
        "COMPLETION_DATE_MOVED_EARLIER": (
            "Completion date changed",
            "The captured official completion date moved earlier.",
        ),
        "REPEATED_EXTENSION": (
            "Repeated completion-date change",
            "More than one later completion date has been observed in monitored history.",
        ),
        "PROJECT_MARKED_LAPSED": ("Project status flag changed", "A lapsed-related flag was observed."),
        "PROJECT_STATUS_CHANGED": ("Project status changed", "The captured project status changed."),
        "QPR_FLAG_OBSERVED": ("QPR-related flag observed", "A QPR-related source flag was observed."),
        "QPR_FLAG_CLEARED": ("QPR-related flag cleared", "A QPR-related source flag was cleared."),
        "IMPORTANT_FIELD_REMOVED": (
            "Previously displayed value removed",
            USER_MESSAGES["value_removed"],
        ),
        "VALUE_REVERSED": ("Value returned to an earlier state", "A previously seen value reappeared."),
        "EXACT_LINKED_NOTICE_ADDED": ("Official notice link added", "A source-linked notice was added."),
        "NEW_PHASE_OR_REGISTRATION": ("New phase or registration observed", "A registration or phase was added."),
        "OFFICIAL_VS_DEVELOPER_CONFLICT": (
            "Official and developer sources conflict",
            "The captured regulator and developer values differ; both were preserved for review.",
        ),
        "OFFICIAL_VS_MARKETING_CONFLICT": (
            "Official and marketing sources conflict",
            "The captured official and marketing values differ; both were preserved for review.",
        ),
    }
    return titles.get(code, (code.replace("_", " ").title(), "A source-backed change was observed."))


async def execute_watchlist_refresh(
    item_id: str,
    payload: RefreshRequest,
    request: Request,
    db: DBSession,
    actor: AnalystActor,
    csrf: CSRFProtected,
    *,
    source_id: str | None = None,
    monitoring_job_id: str | None = None,
    trigger_type: str = "manual",
    attempt_number: int = 1,
    enforce_rate_limit: bool = True,
) -> dict[str, object]:
    del csrf
    if enforce_rate_limit:
        settings = get_settings()
        rate_limiter.check(
            f"refresh:{actor.organization_id}:{actor.user_id}",
            limit=settings.refresh_rate_limit,
        )
    item = db.scalar(
        select(WatchlistItem).where(
            WatchlistItem.id == item_id,
            WatchlistItem.organization_id == actor.organization_id,
            WatchlistItem.status == "active",
        )
    )
    if item is None:
        raise HTTPException(status_code=404, detail={"code": "watchlist_item_not_found"})
    project = db.scalar(
        select(Project).where(
            Project.id == item.project_id,
            Project.organization_id == actor.organization_id,
            Project.deleted_at.is_(None),
        )
    )
    if project is None:
        raise HTTPException(status_code=404, detail={"code": "project_not_found"})
    selected_source_id = source_id or project.registration_source_id
    if not selected_source_id:
        raise HTTPException(
            status_code=409,
            detail={"code": "source_required", "message": "Add an approved source first."},
        )
    source = get_tenant_source(db, actor.organization_id, selected_source_id)
    linked_source = db.scalar(
        select(ProjectSourceLink.id).where(
            ProjectSourceLink.organization_id == actor.organization_id,
            ProjectSourceLink.project_id == project.id,
            ProjectSourceLink.source_id == source.id,
            ProjectSourceLink.is_active.is_(True),
        )
    )
    if linked_source is None and source.id != project.registration_source_id:
        raise HTTPException(
            status_code=404,
            detail={"code": "project_source_not_found"},
        )
    if monitoring_job_id is not None:
        matching_job = db.scalar(
            select(MonitoringJob).where(
                MonitoringJob.id == monitoring_job_id,
                MonitoringJob.organization_id == actor.organization_id,
                MonitoringJob.project_id == project.id,
                MonitoringJob.source_id == source.id,
            )
        )
        if (
            matching_job is None
            or str(matching_job.payload.get("watchlist_item_id", "")) != item.id
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "monitoring_job_scope_mismatch",
                    "message": "The scheduled job does not match this project, source, and watchlist item.",
                },
            )
    existing_run = db.scalar(
        select(MonitoringRun).where(
            MonitoringRun.organization_id == actor.organization_id,
            MonitoringRun.deduplication_key == payload.idempotency_key,
        )
    )
    if existing_run:
        return {
            "monitoring_run": MonitoringRunResponse.model_validate(existing_run),
            "duplicate": True,
            "changes_created": 0,
        }
    policy_model = db.scalar(
        select(SourceAccessPolicy).where(
            SourceAccessPolicy.organization_id == actor.organization_id,
            SourceAccessPolicy.source_id == source.id,
            SourceAccessPolicy.is_current.is_(True),
        )
    )
    if policy_model is None:
        raise HTTPException(
            status_code=409,
            detail={"code": "unknown_source", "message": "No approved source policy exists."},
        )
    run = MonitoringRun(
        organization_id=actor.organization_id,
        monitoring_job_id=monitoring_job_id,
        source_id=source.id,
        project_id=project.id,
        triggered_by_user_id=actor.user_id,
        trigger_type=trigger_type,
        status="running",
        attempt_number=attempt_number,
        deduplication_key=payload.idempotency_key,
        started_at=datetime.now(UTC),
        access_completeness="unavailable",
        run_metadata={
            "watchlist_item_id": item.id,
            "requested_source_id": source.id,
            "request_id": getattr(request.state, "request_id", None),
        },
    )
    db.add(run)
    db.flush()
    domain_policy = _domain_policy(source, policy_model)
    requested_url = source.base_url or item.official_url or ""
    fixture_key = payload.fixture_key
    if source.source_type == "fixture":
        fixture_key = fixture_key or requested_url.removeprefix("fixture://")
        requested_url = f"fixture://{fixture_key}"
    previous = db.scalar(
        select(SourceSnapshot)
        .where(
            SourceSnapshot.organization_id == actor.organization_id,
            SourceSnapshot.project_id == project.id,
            SourceSnapshot.source_id == source.id,
            SourceSnapshot.validation_status == "valid",
        )
        .order_by(SourceSnapshot.fetched_at.desc())
    )
    previous_metadata = previous.response_metadata if previous is not None else {}
    context = FetchRequestContext(
        source_id=source.id,
        requested_url=requested_url,
        fixture_key=fixture_key,
        policy=domain_policy,
        monitoring_run_id=run.id,
        conditional_etag=(
            str(previous_metadata["etag"])
            if previous_metadata.get("etag") is not None
            else None
        ),
        conditional_last_modified=(
            str(previous_metadata["last-modified"])
            if previous_metadata.get("last-modified") is not None
            else None
        ),
    )
    breaker = _circuit_for(source)
    try:
        collector = _collector_for(source)
        task_result = await run_monitoring_task(
            job_key=f"{actor.organization_id}:{source.id}:{payload.idempotency_key}",
            context=context,
            collector=collector,
            parser=_ParserRouter(source),
            active_jobs=_ACTIVE_MONITORING_JOBS,
            circuit_breaker=breaker,
        )
    except (CollectorError, ValueError) as exc:
        breaker.record_failure()
        category = getattr(exc, "code", "policy_blocked")
        message = str(exc)
        _finish_failed_run(
            db,
            run=run,
            source=source,
            category=category,
            completeness="blocked",
            message=message,
            breaker=breaker,
        )
        add_audit_event(
            db,
            actor=actor,
            action="monitoring.failed",
            resource_type="monitoring_run",
            resource_id=run.id,
            request=request,
            metadata={"error_category": category},
        )
        commit_or_rollback(db)
        return {
            "monitoring_run": MonitoringRunResponse.model_validate(run),
            "duplicate": False,
            "changes_created": 0,
            "message": message,
        }
    run.run_metadata = {**run.run_metadata, "fetch_attempts": task_result.attempts}
    if task_result.status not in {"succeeded", "not_modified"}:
        category, completeness, message = _task_failure_values(task_result)
        _finish_failed_run(
            db,
            run=run,
            source=source,
            category=category,
            completeness=completeness,
            message=message,
            breaker=breaker,
            attempts=task_result.attempts,
        )
        add_audit_event(
            db,
            actor=actor,
            action="monitoring.degraded",
            resource_type="monitoring_run",
            resource_id=run.id,
            request=request,
            metadata={"error_category": category},
        )
        commit_or_rollback(db)
        return {
            "monitoring_run": MonitoringRunResponse.model_validate(run),
            "duplicate": False,
            "changes_created": 0,
            "message": message,
        }
    fetch = task_result.fetch_result
    if fetch is None:
        raise RuntimeError("Successful monitoring task did not return a fetch result")
    if task_result.status == "not_modified":
        if previous is None:
            message = "The source returned not-modified without a verified baseline."
            _finish_failed_run(
                db,
                run=run,
                source=source,
                category="not_modified_without_baseline",
                completeness=fetch.completeness.value,
                message=message,
                breaker=breaker,
                attempts=task_result.attempts,
            )
            commit_or_rollback(db)
            return {
                "monitoring_run": MonitoringRunResponse.model_validate(run),
                "duplicate": False,
                "changes_created": 0,
                "message": message,
            }
        _sync_source_circuit(source, breaker)
        source.health_status = "healthy"
        source.last_successful_fetch_at = fetch.fetched_at
        run.status = "succeeded"
        run.completed_at = datetime.now(UTC)
        run.fetched_count = task_result.attempts
        run.snapshot_count = 0
        run.access_completeness = fetch.completeness.value
        add_audit_event(
            db,
            actor=actor,
            action="monitoring.not_modified",
            resource_type="monitoring_run",
            resource_id=run.id,
            request=request,
            metadata={"last_snapshot_id": previous.id},
        )
        _log_monitoring_success(
            run,
            source,
            outcome="not_modified",
            snapshot_id=previous.id,
        )
        commit_or_rollback(db)
        return {
            "monitoring_run": MonitoringRunResponse.model_validate(run),
            "duplicate": True,
            "snapshot_id": previous.id,
            "changes_created": 0,
        }
    parsed = task_result.parsed_snapshot
    if parsed is None:
        raise RuntimeError("Successful monitoring task did not return parsed output")
    expected_registration = registration_identity_key(project.registration_number)
    observed_registration = registration_identity_key(
        parsed.structured_facts.get("registration_number")
    )
    if expected_registration and observed_registration != expected_registration:
        message = (
            "The captured registration number does not match this project. "
            "No snapshot, change, or cached fact was created."
        )
        _finish_failed_run(
            db,
            run=run,
            source=source,
            category="project_identity_mismatch",
            completeness=fetch.completeness.value,
            message=message,
            breaker=breaker,
            attempts=task_result.attempts,
        )
        add_audit_event(
            db,
            actor=actor,
            action="monitoring.identity_mismatch",
            resource_type="monitoring_run",
            resource_id=run.id,
            request=request,
            metadata={
                "expected_registration": project.registration_number,
                "observed_registration": parsed.structured_facts.get("registration_number"),
                "source_id": source.id,
            },
        )
        commit_or_rollback(db)
        return {
            "monitoring_run": MonitoringRunResponse.model_validate(run),
            "duplicate": False,
            "changes_created": 0,
            "message": message,
        }
    if (
        previous
        and previous.content_hash == fetch.content_hash
        and previous.parser_name == parsed.parser_name
        and previous.parser_version == parsed.parser_version
    ):
        run.status = "succeeded"
        run.completed_at = datetime.now(UTC)
        run.fetched_count = task_result.attempts
        run.snapshot_count = 0
        run.access_completeness = fetch.completeness.value
        source.last_successful_fetch_at = fetch.fetched_at
        _sync_source_circuit(source, breaker)
        source.health_status = "healthy"
        add_audit_event(
            db,
            actor=actor,
            action="monitoring.no_change",
            resource_type="monitoring_run",
            resource_id=run.id,
            request=request,
            metadata={"last_snapshot_id": previous.id},
        )
        _log_monitoring_success(
            run,
            source,
            outcome="unchanged_content",
            snapshot_id=previous.id,
        )
        commit_or_rollback(db)
        return {
            "monitoring_run": MonitoringRunResponse.model_validate(run),
            "duplicate": True,
            "snapshot_id": previous.id,
            "changes_created": 0,
        }
    snapshot_dedup = hashlib.sha256(
        f"{project.id}:{source.id}:{fetch.content_hash}:{parsed.parser_name}:{parsed.parser_version}".encode()
    ).hexdigest()
    duplicate_snapshot = db.scalar(
        select(SourceSnapshot).where(
            SourceSnapshot.organization_id == actor.organization_id,
            SourceSnapshot.deduplication_key == snapshot_dedup,
        )
    )
    if duplicate_snapshot is not None:
        run.status = "succeeded"
        run.completed_at = datetime.now(UTC)
        run.fetched_count = task_result.attempts
        run.snapshot_count = 0
        run.access_completeness = fetch.completeness.value
        source.last_successful_fetch_at = fetch.fetched_at
        _sync_source_circuit(source, breaker)
        source.health_status = "healthy"
        add_audit_event(
            db,
            actor=actor,
            action="monitoring.duplicate_snapshot_suppressed",
            resource_type="monitoring_run",
            resource_id=run.id,
            request=request,
            metadata={"snapshot_id": duplicate_snapshot.id},
        )
        _log_monitoring_success(
            run,
            source,
            outcome="duplicate_snapshot",
            snapshot_id=duplicate_snapshot.id,
        )
        commit_or_rollback(db)
        return {
            "monitoring_run": MonitoringRunResponse.model_validate(run),
            "duplicate": True,
            "snapshot_id": duplicate_snapshot.id,
            "changes_created": 0,
        }
    snapshot = SourceSnapshot(
        organization_id=actor.organization_id,
        source_id=source.id,
        project_id=project.id,
        monitoring_run_id=run.id,
        entity_type="project",
        entity_id=project.id,
        fetch_status=fetch.access_status.value,
        requested_url=fetch.requested_url,
        final_url=fetch.final_url,
        http_status=fetch.http_status,
        fetched_at=fetch.fetched_at,
        source_date=(
            fetch.source_reported_date
            if isinstance(fetch.source_reported_date, datetime)
            else None
        ),
        content_type=fetch.content_type,
        content_hash=fetch.content_hash or "",
        parser_name=parsed.parser_name,
        parser_version=parsed.parser_version,
        access_completeness=fetch.completeness.value,
        structured_facts=_json_value(parsed.structured_facts),
        evidence_references=[
            {
                "selector": evidence.selector,
                "page": evidence.page,
                "excerpt": evidence.excerpt,
                "content_hash": evidence.content_hash,
            }
            for evidence in parsed.evidence_references
        ],
        validation_status=parsed.validation_status.value,
        validation_warnings=list(parsed.validation_warnings),
        raw_payload_hash=fetch.content_hash if policy_model.raw_content_may_be_stored else None,
        response_metadata=dict(fetch.response_metadata),
        latency_ms=fetch.elapsed_ms,
        deduplication_key=snapshot_dedup,
    )
    db.add(snapshot)
    db.flush()
    _persist_snapshot_fields(
        db, organization_id=actor.organization_id, snapshot=snapshot, parsed=parsed
    )
    persisted_conflicts, source_conflict_records = _persist_latest_source_conflicts(
        db,
        organization_id=actor.organization_id,
        project_id=project.id,
    )
    domain_changes: tuple[DomainDetectedChange, ...] = ()
    if previous is not None:
        history = list(
            db.scalars(
                select(SourceSnapshot)
                .where(
                    SourceSnapshot.organization_id == actor.organization_id,
                    SourceSnapshot.project_id == project.id,
                    SourceSnapshot.source_id == source.id,
                    SourceSnapshot.id.notin_([previous.id, snapshot.id]),
                )
                .order_by(SourceSnapshot.fetched_at)
            )
        )
        domain_changes = detect_changes(previous, snapshot, history=history)
    domain_changes = (*domain_changes, *_source_conflict_changes(source_conflict_records))
    existing_change_keys = {
        change.deduplication_key
        for change in domain_changes
        if db.scalar(
            select(DetectedChange.id).where(
                DetectedChange.organization_id == actor.organization_id,
                DetectedChange.deduplication_key == change.deduplication_key,
            )
        )
    }
    stored_changes = persist_detected_changes(
        db,
        organization_id=actor.organization_id,
        changes=domain_changes,
    )
    persisted_changes: dict[str, DetectedChange] = {
        record.deduplication_key: record for record in stored_changes
    }
    for change in domain_changes:
        record = persisted_changes[change.deduplication_key]
        if change.deduplication_key in existing_change_keys:
            continue
        evidence_snapshots = (
            ("old", db.get(SourceSnapshot, change.old_snapshot_id)),
            ("new", db.get(SourceSnapshot, change.new_snapshot_id)),
        )
        for side, evidence_snapshot in evidence_snapshots:
            if evidence_snapshot is None:
                continue
            evidence_source = db.get(Source, evidence_snapshot.source_id)
            db.add(
                ChangeEvidence(
                    organization_id=actor.organization_id,
                    detected_change_id=record.id,
                    snapshot_id=evidence_snapshot.id,
                    evidence_side=side,
                    evidence_type="snapshot_field",
                    source_class=(
                        evidence_source.source_class
                        if evidence_source is not None
                        else SourceClass.UNKNOWN.value
                    ),
                    source_url=evidence_snapshot.final_url,
                    evidence_reference=f"snapshot:{evidence_snapshot.id}",
                    excerpt=(
                        record.old_raw_value if side == "old" else record.new_raw_value
                    ),
                    content_hash=evidence_snapshot.content_hash,
                    extraction_method="deterministic_diff",
                    evidence_metadata={"field_key": record.field_key},
                )
            )
    historical_changes = list(
        db.scalars(
            select(DetectedChange).where(
                DetectedChange.organization_id == actor.organization_id,
                DetectedChange.project_id == project.id,
                DetectedChange.new_snapshot_id != snapshot.id,
            )
        )
    )
    domain_signals = generate_signals(
        domain_changes,
        historical_changes=historical_changes,
    )
    signal_count = 0
    for generated in domain_signals:
        if db.scalar(
            select(Signal.id).where(
                Signal.organization_id == actor.organization_id,
                Signal.deduplication_key == generated.deduplication_key,
            )
        ):
            continue
        primary_change = next(
            (
                persisted_changes[item.deduplication_key]
                for item in domain_changes
                if (item.change_id or item.deduplication_key)
                == generated.primary_detected_change_id
            ),
            None,
        )
        supporting = [
            persisted_changes[item.deduplication_key].id
            for item in domain_changes
            if (item.change_id or item.deduplication_key) in generated.supporting_change_ids
        ]
        signal = Signal(
            organization_id=actor.organization_id,
            project_id=project.id,
            primary_detected_change_id=primary_change.id if primary_change else None,
            rule_code=generated.rule_code.value,
            rule_version=generated.rule_version,
            supporting_change_ids=supporting,
            observed_facts=_json_value(dict(generated.observed_facts)),
            severity=generated.severity.value,
            confidence=generated.confidence,
            confidence_dimensions=dict(generated.confidence_dimensions),
            limitations=list(generated.limitations),
            required_review_level=generated.required_review_level.value,
            state="active",
            detected_at=generated.detected_at,
            deduplication_key=generated.deduplication_key,
        )
        db.add(signal)
        db.flush()
        title, description = _signal_title(generated.rule_code.value)
        requires_review = generated.required_review_level.value != "none" or project.is_provisional
        role_actions = suggested_review_actions(
            generated.rule_code,
            ("property_adviser", "supplier", "lender", "marketing_team"),
        )
        db.add(
            Alert(
                organization_id=actor.organization_id,
                project_id=project.id,
                signal_id=signal.id,
                detected_change_id=primary_change.id if primary_change else None,
                source_id=source.id,
                title=title,
                neutral_description=description,
                old_value=primary_change.old_canonical_value if primary_change else None,
                new_value=primary_change.new_canonical_value if primary_change else None,
                magnitude=(
                    f"{primary_change.magnitude_days:+d} days"
                    if primary_change and primary_change.magnitude_days is not None
                    else None
                ),
                state="open",
                severity=generated.severity.value,
                required_review_level="reviewer" if requires_review else "none",
                requires_review=requires_review,
                source_class=source.source_class,
                freshness=fetch.fetched_at.isoformat(),
                limitation=("; ".join(generated.limitations) or None),
                suggested_review_action=" ".join(
                    f"{item.role.replace('_', ' ').title()}: {item.action}"
                    for item in role_actions
                ),
                deduplication_key=f"alert:{generated.deduplication_key}",
            )
        )
        signal_count += 1
    if (
        parsed.validation_status.value == "valid"
        and source.source_class == SourceClass.OFFICIAL_REGULATOR.value
        and source.id == project.registration_source_id
    ):
        _update_project_cache(project, parsed.structured_facts, fetch.fetched_at)
    source.last_successful_fetch_at = fetch.fetched_at
    source.last_content_hash = fetch.content_hash
    _sync_source_circuit(source, breaker)
    source.health_status = "healthy"
    run.status = "succeeded"
    run.completed_at = datetime.now(UTC)
    run.fetched_count = task_result.attempts
    run.snapshot_count = 1
    run.access_completeness = fetch.completeness.value
    add_audit_event(
        db,
        actor=actor,
        action="monitoring.completed",
        resource_type="monitoring_run",
        resource_id=run.id,
        request=request,
        metadata={
            "snapshot_id": snapshot.id,
            "changes": len(persisted_changes),
            "signals": signal_count,
            "source_conflicts": len(persisted_conflicts),
        },
    )
    _log_monitoring_success(
        run,
        source,
        outcome="snapshot_created",
        snapshot_id=snapshot.id,
    )
    commit_or_rollback(db)
    db.refresh(run)
    return {
        "monitoring_run": MonitoringRunResponse.model_validate(run),
        "duplicate": False,
        "snapshot_id": snapshot.id,
        "changes_created": len(persisted_changes),
        "signals_created": signal_count,
        "source_conflicts": len(persisted_conflicts),
    }


@router.post("/watchlist-items/{item_id}/refresh")
async def refresh_watchlist_item(
    item_id: str,
    payload: RefreshRequest,
    request: Request,
    db: DBSession,
    actor: AnalystActor,
    csrf: CSRFProtected,
) -> dict[str, object]:
    return await execute_watchlist_refresh(
        item_id=item_id,
        payload=payload,
        request=request,
        db=db,
        actor=actor,
        csrf=csrf,
        source_id=payload.source_id,
    )


@router.get("/monitoring-runs/{run_id}", response_model=MonitoringRunResponse)
def get_monitoring_run(
    run_id: str, db: DBSession, actor: CurrentActor
) -> MonitoringRun:
    run = db.scalar(
        select(MonitoringRun).where(
            MonitoringRun.id == run_id,
            MonitoringRun.organization_id == actor.organization_id,
        )
    )
    if run is None:
        raise HTTPException(status_code=404, detail={"code": "monitoring_run_not_found"})
    return run
