from __future__ import annotations

from calendar import month_name
from datetime import UTC, date, datetime, timedelta
from math import ceil
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    Alert,
    AuditEvent,
    DetectedChange,
    Developer,
    Location,
    MarketSnapshot,
    MonitoringJob,
    MonitoringRun,
    Project,
    Report,
    ReportItem,
    Signal,
    Source,
    SourceAccessPolicy,
    SourceConflict,
    SourceSnapshot,
    User,
    Watchlist,
    WatchlistItem,
)
from app.services.reports import weekly_digest_metrics

PAGE_SIZE = 20
_OPEN_STATES = {"open", "unresolved", "pending"}
_APPROVAL_STATES = {"confirmed", "confirm", "expected", "mark_expected"}


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _timestamp(value: datetime | None) -> str:
    aware = _aware(value)
    if aware is None:
        return "Not recorded"
    utc_value = aware.astimezone(UTC)
    return f"{utc_value.day} {month_name[utc_value.month]} {utc_value.year}, {utc_value:%H:%M} UTC"


def _date_label(value: date | None) -> str:
    if value is None:
        return "Not recorded"
    return f"{value.day} {month_name[value.month]} {value.year}"


def _period_label(start: date | None, end: date | None) -> str:
    if start and end:
        return f"{_date_label(start)} - {_date_label(end)}"
    if start:
        return f"From {_date_label(start)}"
    if end:
        return f"Through {_date_label(end)}"
    return "Period not recorded"


def _title(value: str | None, fallback: str = "Unverified") -> str:
    return value.replace("_", " ").strip().title() if value else fallback


def _percent(successes: int, total: int) -> int:
    return round((successes / total) * 100) if total else 0


def _pagination(total: int, page: int) -> tuple[int, int, int]:
    total_pages = max(1, ceil(total / PAGE_SIZE))
    safe_page = max(1, min(page, total_pages))
    return safe_page, total_pages, (safe_page - 1) * PAGE_SIZE


def _confidence(value: float | None) -> str:
    if value is None:
        return "Needs review"
    score = value * 100 if value <= 1 else value
    if score >= 85:
        return "High confidence"
    if score >= 65:
        return "Probable"
    return "Needs review"


def _latest_run_by_source(runs: list[MonitoringRun]) -> dict[str, MonitoringRun]:
    latest: dict[str, MonitoringRun] = {}
    for run in sorted(runs, key=lambda item: _aware(item.created_at) or datetime.min.replace(tzinfo=UTC)):
        latest[run.source_id] = run
    return latest


def dashboard_view(db: Session, *, organization_id: str) -> dict[str, Any]:
    now = datetime.now(UTC)
    monitored_project_ids = list(
        db.scalars(
            select(Project.id)
            .join(WatchlistItem, WatchlistItem.project_id == Project.id)
            .join(Watchlist, Watchlist.id == WatchlistItem.watchlist_id)
            .where(
                Project.organization_id == organization_id,
                Project.deleted_at.is_(None),
                WatchlistItem.organization_id == organization_id,
                WatchlistItem.status == "active",
                Watchlist.organization_id == organization_id,
                Watchlist.is_active.is_(True),
                Watchlist.deleted_at.is_(None),
            )
            .distinct()
        )
    )
    jobs = list(
        db.scalars(
            select(MonitoringJob).where(MonitoringJob.organization_id == organization_id)
        )
    )
    runs = list(
        db.scalars(
            select(MonitoringRun)
            .where(MonitoringRun.organization_id == organization_id)
            .order_by(MonitoringRun.created_at.desc())
        )
    )
    sources = list(
        db.scalars(select(Source).where(Source.organization_id == organization_id))
    )
    latest_report = db.scalar(
        select(Report)
        .where(
            Report.organization_id == organization_id,
            Report.report_type == "weekly_digest",
            Report.deleted_at.is_(None),
        )
        .order_by(Report.created_at.desc())
    )
    stats = {
        "projects_monitored": len(monitored_project_ids),
        "project_hint": "Across active watchlists",
        "checks_due": sum(
            job.state in {"retry", "queued"}
            or (
                job.state == "scheduled"
                and _aware(job.next_run_at) is not None
                and _aware(job.next_run_at) <= now
            )
            for job in jobs
        ),
        "checks_complete": sum(run.status == "succeeded" for run in runs),
        "changes_detected": int(
            db.scalar(
                select(func.count())
                .select_from(DetectedChange)
                .where(DetectedChange.organization_id == organization_id)
            )
            or 0
        ),
        "alerts_awaiting_review": int(
            db.scalar(
                select(func.count())
                .select_from(Alert)
                .where(
                    Alert.organization_id == organization_id,
                    Alert.requires_review.is_(True),
                    Alert.state.in_(sorted(_OPEN_STATES)),
                )
            )
            or 0
        ),
        "source_degradation": sum(
            source.health_status in {"degraded", "unavailable", "disabled"}
            for source in sources
        ),
        "unresolved_conflicts": int(
            db.scalar(
                select(func.count())
                .select_from(SourceConflict)
                .where(
                    SourceConflict.organization_id == organization_id,
                    SourceConflict.state == "open",
                )
            )
            or 0
        ),
        "latest_digest_label": _title(latest_report.state) if latest_report else "Not generated",
        "latest_digest_hint": latest_report.title if latest_report else "Weekly evidence summary",
    }

    alert_rows = list(
        db.execute(
            select(Alert, Project, Signal)
            .join(
                Project,
                (Project.id == Alert.project_id)
                & (Project.organization_id == organization_id)
                & Project.deleted_at.is_(None),
            )
            .outerjoin(
                Signal,
                (Signal.id == Alert.signal_id)
                & (Signal.organization_id == organization_id),
            )
            .where(
                Alert.organization_id == organization_id,
                Alert.requires_review.is_(True),
                Alert.state.in_(sorted(_OPEN_STATES)),
            )
            .order_by(Alert.first_seen_at.desc())
            .limit(6)
        )
    )
    recent_alerts = [
        {
            "id": alert.id,
            "project_name": project.name,
            "title": alert.title,
            "observed_at": _timestamp(alert.first_seen_at),
            "confidence": _confidence(signal.confidence if signal else None),
            "review_state": _title(alert.state, "Open"),
        }
        for alert, project, signal in alert_rows
    ]

    successful_runs = sum(run.status == "succeeded" for run in runs)
    last_completed = max(
        (_aware(run.completed_at) for run in runs if run.completed_at),
        default=None,
    )
    unhealthy = any(
        source.health_status in {"degraded", "unavailable", "disabled"}
        for source in sources
    )
    source_summary = {
        "source_name": f"{len(sources)} configured source{'s' if len(sources) != 1 else ''}",
        "freshness": _timestamp(last_completed) if last_completed else "No completed check",
        "state": "degraded" if unhealthy else "healthy" if sources else "unverified",
        "source_date": _timestamp(last_completed) if last_completed else "",
        "success_rate": _percent(successful_runs, len(runs)),
    }

    audit_rows = list(
        db.scalars(
            select(AuditEvent)
            .where(AuditEvent.organization_id == organization_id)
            .order_by(AuditEvent.created_at.desc())
            .limit(4)
        )
    )
    recent_activity = [
        {
            "timestamp": _timestamp(event.created_at),
            "title": _title(event.action, "Recorded activity"),
            "description": (
                f"{_title(event.resource_type, 'Organization record')} {event.resource_id or ''}"
            ).strip(),
            "tone": "verified",
        }
        for event in audit_rows
    ]
    return {
        "stats": stats,
        "recent_alerts": recent_alerts,
        "source_summary": source_summary,
        "recent_activity": recent_activity,
    }


def watchlists_view(
    db: Session,
    *,
    organization_id: str,
    page: int,
) -> dict[str, Any]:
    base = (
        Watchlist.organization_id == organization_id,
        Watchlist.deleted_at.is_(None),
    )
    total = int(
        db.scalar(select(func.count()).select_from(Watchlist).where(*base)) or 0
    )
    safe_page, total_pages, offset = _pagination(total, page)
    watchlists = list(
        db.scalars(
            select(Watchlist)
            .where(*base)
            .order_by(Watchlist.name)
            .limit(PAGE_SIZE)
            .offset(offset)
        )
    )
    watchlist_ids = [item.id for item in watchlists]
    item_rows = (
        list(
            db.execute(
                select(WatchlistItem, Project)
                .join(
                    Project,
                    (Project.id == WatchlistItem.project_id)
                    & (Project.organization_id == organization_id)
                    & Project.deleted_at.is_(None),
                )
                .where(
                    WatchlistItem.organization_id == organization_id,
                    WatchlistItem.watchlist_id.in_(watchlist_ids),
                    WatchlistItem.status == "active",
                )
            )
        )
        if watchlist_ids
        else []
    )
    jobs = (
        list(
            db.scalars(
                select(MonitoringJob).where(
                    MonitoringJob.organization_id == organization_id,
                    MonitoringJob.watchlist_id.in_(watchlist_ids),
                    MonitoringJob.state.in_(["scheduled", "retry", "queued", "running"]),
                )
            )
        )
        if watchlist_ids
        else []
    )
    rows: list[dict[str, Any]] = []
    for watchlist in watchlists:
        projects = [
            project
            for item, project in item_rows
            if item.watchlist_id == watchlist.id
        ]
        verified = sum(project.last_verified_at is not None for project in projects)
        coverage = (
            "Current"
            if projects and verified == len(projects)
            else "Partial"
            if verified
            else "Unverified"
        )
        next_run = min(
            (
                _aware(job.next_run_at)
                for job in jobs
                if job.watchlist_id == watchlist.id and job.next_run_at is not None
            ),
            default=None,
        )
        rows.append(
            {
                "id": watchlist.id,
                "name": watchlist.name,
                "description": watchlist.description,
                "project_count": len(projects),
                "monitoring_cadence": _title(watchlist.monitoring_cadence, "Manual"),
                "next_check_at": _timestamp(next_run) if next_run else "Manual",
                "coverage_state": coverage,
            }
        )
    return {"watchlists": rows, "page": safe_page, "total_pages": total_pages}


def watchlist_detail_view(
    db: Session,
    *,
    organization_id: str,
    watchlist: Watchlist,
) -> dict[str, Any]:
    rows = list(
        db.execute(
            select(WatchlistItem, Project, Location)
            .join(
                Project,
                (Project.id == WatchlistItem.project_id)
                & (Project.organization_id == organization_id)
                & Project.deleted_at.is_(None),
            )
            .outerjoin(
                Location,
                (Location.id == Project.location_id)
                & (Location.organization_id == organization_id),
            )
            .where(
                WatchlistItem.organization_id == organization_id,
                WatchlistItem.watchlist_id == watchlist.id,
                WatchlistItem.status == "active",
            )
            .order_by(Project.name)
        )
    )
    project_ids = [project.id for _, project, _ in rows]
    jobs = list(
        db.scalars(
            select(MonitoringJob).where(
                MonitoringJob.organization_id == organization_id,
                MonitoringJob.watchlist_id == watchlist.id,
            )
        )
    )
    runs = (
        list(
            db.scalars(
                select(MonitoringRun).where(
                    MonitoringRun.organization_id == organization_id,
                    MonitoringRun.project_id.in_(project_ids),
                )
            )
        )
        if project_ids
        else []
    )
    latest_runs: dict[str, MonitoringRun] = {}
    for run in sorted(
        runs,
        key=lambda item: _aware(item.created_at) or datetime.min.replace(tzinfo=UTC),
    ):
        if run.project_id:
            latest_runs[run.project_id] = run
    alert_counts = (
        {
            project_id: int(count)
            for project_id, count in db.execute(
                select(Alert.project_id, func.count())
                .where(
                    Alert.organization_id == organization_id,
                    Alert.project_id.in_(project_ids),
                    Alert.state.in_(sorted(_OPEN_STATES)),
                )
                .group_by(Alert.project_id)
            )
        }
        if project_ids
        else {}
    )
    now = datetime.now(UTC)
    items: list[dict[str, Any]] = []
    for item, project, location in rows:
        latest_run = latest_runs.get(project.id)
        monitoring_state = "Pending"
        if latest_run:
            monitoring_state = (
                "Current"
                if latest_run.status == "succeeded"
                and latest_run.access_completeness == "complete"
                else "Degraded"
            )
        elif project.last_verified_at:
            monitoring_state = "Current"
        items.append(
            {
                "id": item.id,
                "project": {
                    "id": project.id,
                    "name": project.name,
                    "registration_number": project.registration_number,
                    "locality_or_sector": (
                        location.locality_or_sector if location else "Location not recorded"
                    ),
                    "current_status": _title(project.current_status),
                },
                "monitoring_state": monitoring_state,
                "last_verified_at": _timestamp(project.last_verified_at),
                "open_alerts": int(alert_counts.get(project.id, 0)),
                "fixture_key": (project.official_url or "").removeprefix("fixture://"),
                "refresh_idempotency_key": f"manual-{item.id}-{int(now.timestamp())}",
            }
        )
    verified_count = sum(
        item["last_verified_at"] != "Not recorded" for item in items
    )
    coverage = (
        "Current"
        if items and verified_count == len(items)
        else "Partial"
        if verified_count
        else "Unverified"
    )
    last_verified = max(
        (_aware(project.last_verified_at) for _, project, _ in rows if project.last_verified_at),
        default=None,
    )
    due_cutoff = now + timedelta(days=7)
    view = {
        "id": watchlist.id,
        "name": watchlist.name,
        "description": watchlist.description,
        "monitoring_cadence": _title(watchlist.monitoring_cadence, "Manual"),
        "source_name": "Configured watchlist monitoring runs",
        "freshness": _timestamp(last_verified) if last_verified else "No completed run",
        "coverage_state": coverage.lower(),
        "project_count": len(items),
        "checks_due": sum(
            job.state in {"retry", "queued"}
            or (
                job.state == "scheduled"
                and _aware(job.next_run_at) is not None
                and _aware(job.next_run_at) <= due_cutoff
            )
            for job in jobs
        ),
        "open_alerts": sum(int(item["open_alerts"]) for item in items),
    }
    return {"watchlist": view, "items": items}


def source_health_view(
    db: Session,
    *,
    organization_id: str,
    page: int,
) -> dict[str, Any]:
    all_sources = list(
        db.scalars(
            select(Source)
            .where(Source.organization_id == organization_id)
            .order_by(Source.name)
        )
    )
    safe_page, total_pages, offset = _pagination(len(all_sources), page)
    sources = all_sources[offset : offset + PAGE_SIZE]
    source_ids = [source.id for source in all_sources]
    policies = list(
        db.scalars(
            select(SourceAccessPolicy).where(
                SourceAccessPolicy.organization_id == organization_id,
                SourceAccessPolicy.source_id.in_(source_ids),
                SourceAccessPolicy.is_current.is_(True),
            )
        )
    ) if source_ids else []
    runs = list(
        db.scalars(
            select(MonitoringRun)
            .where(
                MonitoringRun.organization_id == organization_id,
                MonitoringRun.source_id.in_(source_ids),
            )
            .order_by(MonitoringRun.created_at)
        )
    ) if source_ids else []
    snapshots = list(
        db.scalars(
            select(SourceSnapshot)
            .where(
                SourceSnapshot.organization_id == organization_id,
                SourceSnapshot.source_id.in_(source_ids),
            )
            .order_by(SourceSnapshot.fetched_at)
        )
    ) if source_ids else []
    policy_by_source = {policy.source_id: policy for policy in policies}
    latest_run = _latest_run_by_source(runs)
    rows: list[dict[str, Any]] = []
    for source in sources:
        source_runs = [run for run in runs if run.source_id == source.id]
        source_snapshots = [item for item in snapshots if item.source_id == source.id]
        policy = policy_by_source.get(source.id)
        latest_snapshot = source_snapshots[-1] if source_snapshots else None
        latencies = [item.latency_ms for item in source_snapshots if item.latency_ms is not None]
        fetch_successes = sum(run.status == "succeeded" for run in source_runs)
        parser_successes = sum(
            item.validation_status in {"valid", "partial"} for item in source_snapshots
        )
        parser_failures = sum(
            run.error_category
            in {"layout_changed", "parser_failed", "pdf_unreadable", "invalid_layout"}
            for run in source_runs
        )
        parser_attempts = parser_successes + parser_failures
        run = latest_run.get(source.id)
        rows.append(
            {
                "id": source.id,
                "name": source.name,
                "domain": source.domain,
                "policy_version": policy.policy_version if policy else "Not configured",
                "state": _title(source.health_status),
                "access_completeness": _title(
                    run.access_completeness if run else None,
                    "No completed check",
                ),
                "fetch_success_rate": _percent(fetch_successes, len(source_runs)),
                "parser_success_rate": _percent(parser_successes, parser_attempts),
                "last_successful_fetch": _timestamp(source.last_successful_fetch_at),
                "last_content_hash": source.last_content_hash,
                "consecutive_failures": source.consecutive_failures,
                "circuit_breaker_state": _title(source.circuit_breaker_state, "Closed"),
                "average_latency_ms": round(sum(latencies) / len(latencies)) if latencies else 0,
                "parser_name": latest_snapshot.parser_name if latest_snapshot else "Not assigned",
                "parser_version": latest_snapshot.parser_version if latest_snapshot else "-",
            }
        )
    retry_count = int(
        db.scalar(
            select(func.count())
            .select_from(MonitoringJob)
            .where(
                MonitoringJob.organization_id == organization_id,
                MonitoringJob.state == "retry",
            )
        )
        or 0
    )
    latest_success = max(
        (_aware(source.last_successful_fetch_at) for source in all_sources if source.last_successful_fetch_at),
        default=None,
    )
    healthy = sum(source.health_status == "healthy" for source in all_sources)
    degraded = sum(
        source.health_status in {"degraded", "unavailable", "disabled"}
        for source in all_sources
    )
    enabled = sum(
        source.is_enabled
        and bool(policy_by_source.get(source.id) and policy_by_source[source.id].is_enabled)
        for source in all_sources
    )
    summary = {
        "source_name": "Configured source adapters",
        "freshness": _timestamp(latest_success) if latest_success else "No health check recorded",
        "overall_state": "degraded" if degraded else "healthy" if all_sources else "unverified",
        "as_of": _timestamp(latest_success) if latest_success else "",
        "enabled_sources": enabled,
        "healthy_sources": healthy,
        "degraded_sources": degraded,
        "jobs_awaiting_retry": retry_count,
    }
    incidents = []
    for run in sorted(
        (
            item
            for item in runs
            if item.status != "succeeded" or item.access_completeness != "complete"
        ),
        key=lambda item: _aware(item.created_at) or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )[:10]:
        source = next((item for item in all_sources if item.id == run.source_id), None)
        category = run.error_category or "incomplete_check"
        safe_message = {
            "captcha_or_blocked": "Automatic access was blocked; no factual alert was generated.",
            "rate_limited": "The check is awaiting a permitted retry; prior verified facts remain.",
            "parser_error": "The source format needs maintenance; no factual alert was generated.",
            "layout_changed": "The source format changed; no factual alert was generated.",
        }.get(category, "The check was incomplete; prior verified facts remain visible.")
        incidents.append(
            {
                "source_name": source.name if source else "Configured source",
                "error_category": category,
                "observed_at": _timestamp(run.completed_at or run.created_at),
                "safe_message": safe_message,
            }
        )
    return {
        "summary": summary,
        "sources": rows,
        "incidents": incidents,
        "page": safe_page,
        "total_pages": total_pages,
    }


def weekly_digest_view(db: Session, *, organization_id: str) -> dict[str, Any]:
    latest_run_at = db.scalar(
        select(func.max(MonitoringRun.completed_at)).where(
            MonitoringRun.organization_id == organization_id
        )
    )
    latest_signal_at = db.scalar(
        select(func.max(Signal.detected_at)).where(Signal.organization_id == organization_id)
    )
    latest_alert_at = db.scalar(
        select(func.max(Alert.first_seen_at)).where(Alert.organization_id == organization_id)
    )
    candidates = [
        aware
        for value in (latest_run_at, latest_signal_at, latest_alert_at)
        if (aware := _aware(value)) is not None
    ]
    period_end = max(candidates, default=datetime.now(UTC))
    period_start = period_end - timedelta(days=6)
    runs = list(
        db.scalars(
            select(MonitoringRun).where(
                MonitoringRun.organization_id == organization_id,
                MonitoringRun.completed_at >= period_start,
                MonitoringRun.completed_at <= period_end,
            )
        )
    )
    signals = list(
        db.scalars(
            select(Signal).where(
                Signal.organization_id == organization_id,
                Signal.detected_at >= period_start,
                Signal.detected_at <= period_end,
            )
        )
    )
    alerts = list(
        db.scalars(
            select(Alert).where(
                Alert.organization_id == organization_id,
                Alert.first_seen_at >= period_start,
                Alert.first_seen_at <= period_end,
            )
        )
    )
    metrics = weekly_digest_metrics(
        monitoring_runs=runs,
        signals=signals,
        alerts=alerts,
    )
    incomplete = sum(
        run.status != "succeeded" or run.access_completeness != "complete" for run in runs
    )
    open_conflicts = int(
        db.scalar(
            select(func.count())
            .select_from(SourceConflict)
            .where(
                SourceConflict.organization_id == organization_id,
                SourceConflict.state == "open",
                SourceConflict.detected_at >= period_start,
                SourceConflict.detected_at <= period_end,
            )
        )
        or 0
    )
    blockers = [
        item
        for item in alerts
        if item.requires_review
        and item.severity in {"high", "critical"}
        and item.state not in _APPROVAL_STATES
    ]
    digest = {
        "period_label": _period_label(period_start.date(), period_end.date()),
        "source_name": "Monitoring runs and review queue",
        "freshness": "Latest completed seven-day monitoring period",
        "source_state": "degraded" if incomplete else "verified" if runs else "unverified",
        "generated_at": _timestamp(datetime.now(UTC)),
        "projects_checked": metrics["total_projects_checked"],
        "successful_checks": metrics["successful_checks"],
        "degraded_sources": metrics["degraded_or_unavailable_sources"],
        "confirmed_changes": metrics["confirmed_changes"],
        "review_required_alerts": metrics["review_required_alerts"],
        "completion_date_changes": metrics["completion_date_changes"],
        "status_signals": metrics["qpr_lapsed_status_signals"],
        "new_phase_events": metrics["new_phase_or_registration_events"],
        "conflicts": open_conflicts,
        "incomplete_checks": incomplete,
        "last_verified_cutoff": _timestamp(
            max(
                (_aware(run.completed_at) for run in runs if run.status == "succeeded"),
                default=None,
            )
        ),
        "report_readiness": "Ready" if not blockers else "Not ready",
        "readiness_note": (
            "No unresolved high-impact required-review alerts are in this period."
            if not blockers
            else f"Resolve {len(blockers)} high-impact required-review item(s) before approval."
        ),
    }
    signal_by_id = {item.id: item for item in signals}
    project_ids = {item.project_id for item in alerts}
    projects = {
        item.id: item
        for item in db.scalars(
            select(Project).where(
                Project.organization_id == organization_id,
                Project.id.in_(project_ids),
                Project.deleted_at.is_(None),
            )
        )
    } if project_ids else {}
    highlights = []
    for alert in sorted(alerts, key=lambda item: item.first_seen_at, reverse=True)[:10]:
        signal = signal_by_id.get(alert.signal_id or "")
        highlights.append(
            {
                "category": _title(signal.rule_code if signal else "Observed change"),
                "project_name": projects.get(alert.project_id).name
                if alert.project_id in projects
                else "Project identity unavailable",
                "description": alert.neutral_description,
                "review_state": _title(alert.state, "Needs review"),
                "signal_code": signal.rule_code if signal else "EVIDENCE_LINKED_ALERT",
                "observed_at": _timestamp(alert.first_seen_at),
                "detail_href": (
                    f"/evidence/{alert.detected_change_id}"
                    if alert.detected_change_id
                    else f"/alerts/{alert.id}"
                ),
                "detail_label": "Evidence" if alert.detected_change_id else "Review",
                "tone": "danger" if alert.severity in {"high", "critical"} else "warning",
            }
        )
    return {"digest": digest, "highlights": highlights}


def market_view(
    db: Session,
    *,
    organization_id: str,
    location_id: str,
) -> dict[str, Any] | None:
    location = db.scalar(
        select(Location).where(
            Location.id == location_id,
            Location.organization_id == organization_id,
        )
    )
    if location is None:
        return None
    locations = list(
        db.scalars(
            select(Location)
            .where(Location.organization_id == organization_id)
            .order_by(Location.city, Location.locality_or_sector)
        )
    )
    projects = list(
        db.scalars(
            select(Project).where(
                Project.organization_id == organization_id,
                Project.location_id == location.id,
                Project.deleted_at.is_(None),
            )
        )
    )
    project_ids = [project.id for project in projects]
    snapshot = db.scalar(
        select(MarketSnapshot)
        .where(
            MarketSnapshot.organization_id == organization_id,
            MarketSnapshot.location_id == location.id,
        )
        .order_by(MarketSnapshot.period_end.desc(), MarketSnapshot.generated_at.desc())
    )
    sources = list(
        db.scalars(
            select(Source).where(
                Source.organization_id == organization_id,
                Source.is_enabled.is_(True),
            )
        )
    )
    healthy_percentage = _percent(
        sum(source.health_status == "healthy" for source in sources), len(sources)
    )
    if snapshot:
        summary = {
            "projects_monitored": snapshot.projects_monitored,
            "active_projects": snapshot.active_projects,
            "lapsed_projects": snapshot.lapsed_projects,
            "qpr_flagged_projects": snapshot.qpr_flagged_projects,
            "recent_reviewed_completion_changes": snapshot.recent_reviewed_completion_changes,
            "new_phases": snapshot.new_registrations_or_phases,
            "developer_count": snapshot.developer_count,
            "source_name": "Generated monitored-dataset snapshot",
            "freshness": _period_label(snapshot.period_start, snapshot.period_end),
            "source_state": "verified",
            "as_of": _timestamp(snapshot.generated_at),
        }
    else:
        active_statuses = {"active", "registered", "ongoing", "under construction"}
        summary = {
            "projects_monitored": len(projects),
            "active_projects": sum(
                (project.current_status or "").lower() in active_statuses for project in projects
            ),
            "lapsed_projects": sum(project.lapsed_flag is True for project in projects),
            "qpr_flagged_projects": sum(project.qpr_missing_flag is True for project in projects),
            "recent_reviewed_completion_changes": int(
                db.scalar(
                    select(func.count())
                    .select_from(DetectedChange)
                    .where(
                        DetectedChange.organization_id == organization_id,
                        DetectedChange.project_id.in_(project_ids),
                        DetectedChange.change_type.in_(["date_extended", "date_moved_earlier"]),
                        DetectedChange.review_state.in_(["confirmed", "expected"]),
                    )
                )
                or 0
            ) if project_ids else 0,
            "new_phases": int(
                db.scalar(
                    select(func.count())
                    .select_from(Signal)
                    .where(
                        Signal.organization_id == organization_id,
                        Signal.project_id.in_(project_ids),
                        Signal.rule_code == "NEW_PHASE_OR_REGISTRATION",
                    )
                )
                or 0
            ) if project_ids else 0,
            "developer_count": len({project.developer_id for project in projects if project.developer_id}),
            "source_name": "Current organization monitored dataset",
            "freshness": _timestamp(
                max(
                    (_aware(project.last_verified_at) for project in projects if project.last_verified_at),
                    default=None,
                )
            ),
            "source_state": "current" if projects else "unverified",
            "as_of": "",
        }
    summary["healthy_source_percentage"] = healthy_percentage

    developers_by_id = {
        item.id: item
        for item in db.scalars(
            select(Developer).where(
                Developer.organization_id == organization_id,
                Developer.id.in_({project.developer_id for project in projects if project.developer_id}),
            )
        )
    } if any(project.developer_id for project in projects) else {}
    developers = []
    for developer_id, developer in sorted(
        developers_by_id.items(), key=lambda item: item[1].canonical_name
    ):
        developer_projects = [project for project in projects if project.developer_id == developer_id]
        developers.append(
            {
                "name": developer.canonical_name,
                "project_count": len(developer_projects),
                "coverage_state": (
                    "Current"
                    if developer_projects
                    and all(project.last_verified_at for project in developer_projects)
                    else "Partial"
                ),
            }
        )

    events: list[dict[str, Any]] = []
    if project_ids:
        change_rows = list(
            db.execute(
                select(DetectedChange, Project)
                .join(
                    Project,
                    (Project.id == DetectedChange.project_id)
                    & (Project.organization_id == organization_id),
                )
                .where(
                    DetectedChange.organization_id == organization_id,
                    DetectedChange.project_id.in_(project_ids),
                    DetectedChange.review_state.in_(["confirmed", "expected"]),
                )
            )
        )
        for change, project in change_rows:
            events.append(
                {
                    "timestamp": _timestamp(change.detected_at),
                    "sort_at": _aware(change.detected_at),
                    "event_type": "Reviewed change",
                    "title": project.name,
                    "description": (
                        f"{_title(change.field_key, 'Captured field')}: "
                        f"{change.old_canonical_value or 'Not present'} -> "
                        f"{change.new_canonical_value or 'Not present'}"
                    ),
                    "tone": "verified",
                }
            )
        signal_rows = list(
            db.execute(
                select(Signal, Project)
                .join(
                    Project,
                    (Project.id == Signal.project_id)
                    & (Project.organization_id == organization_id),
                )
                .where(
                    Signal.organization_id == organization_id,
                    Signal.project_id.in_(project_ids),
                    Signal.rule_code == "NEW_PHASE_OR_REGISTRATION",
                )
            )
        )
        for signal, project in signal_rows:
            events.append(
                {
                    "timestamp": _timestamp(signal.detected_at),
                    "sort_at": _aware(signal.detected_at),
                    "event_type": "New phase",
                    "title": project.name,
                    "description": "A new registration or phase was added to the monitored dataset.",
                    "tone": "new",
                }
            )
    recent_events = sorted(
        events,
        key=lambda item: item["sort_at"] or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )[:10]
    for event in recent_events:
        event.pop("sort_at", None)
    return {
        "location": {
            "id": location.id,
            "name": ", ".join(
                item
                for item in (location.locality_or_sector, location.city)
                if item
            ),
        },
        "locations": [
            {
                "id": item.id,
                "name": ", ".join(
                    value for value in (item.locality_or_sector, item.city) if value
                ),
            }
            for item in locations
        ],
        "summary": summary,
        "developers": developers,
        "recent_events": recent_events,
    }


def reports_view(
    db: Session,
    *,
    organization_id: str,
    page: int,
) -> dict[str, Any]:
    base = (
        Report.organization_id == organization_id,
        Report.deleted_at.is_(None),
    )
    total = int(db.scalar(select(func.count()).select_from(Report).where(*base)) or 0)
    safe_page, total_pages, offset = _pagination(total, page)
    rows = list(
        db.execute(
            select(Report, User)
            .join(User, User.id == Report.generated_by_user_id)
            .where(*base)
            .order_by(Report.created_at.desc())
            .limit(PAGE_SIZE)
            .offset(offset)
        )
    )
    report_ids = [report.id for report, _ in rows]
    items = list(
        db.scalars(
            select(ReportItem).where(
                ReportItem.organization_id == organization_id,
                ReportItem.report_id.in_(report_ids),
            )
        )
    ) if report_ids else []
    alert_ids = {item.alert_id for item in items if item.alert_id}
    alerts = {
        alert.id: alert
        for alert in db.scalars(
            select(Alert).where(
                Alert.organization_id == organization_id,
                Alert.id.in_(alert_ids),
            )
        )
    } if alert_ids else {}
    now = datetime.now(UTC)
    reports = []
    for report, creator in rows:
        report_items = [item for item in items if item.report_id == report.id]
        unresolved = sum(
            bool(item.alert_id)
            and item.alert_id in alerts
            and alerts[item.alert_id].requires_review
            and alerts[item.alert_id].state not in _APPROVAL_STATES
            for item in report_items
        )
        configured_formats = report.summary_metrics.get("formats", [])
        formats = [
            str(item)
            for item in configured_formats
            if str(item) in {"pdf", "csv", "json", "evidence"}
        ]
        expired = _aware(report.expires_at) is not None and _aware(report.expires_at) < now
        if expired:
            formats = []
        reports.append(
            {
                "id": report.id,
                "title": report.title,
                "created_at": _timestamp(report.created_at),
                "created_by": creator.display_name,
                "period_label": _period_label(report.period_start, report.period_end),
                "state": "Expired" if expired else _title(report.state, "Draft"),
                "blocked_reason": (
                    f"{unresolved} required-review item(s) must be resolved before approval."
                    if unresolved and report.state != "approved"
                    else ""
                ),
                "evidence_item_count": len(report_items),
                "unresolved_required_review_count": unresolved,
                "expires_at": _timestamp(report.expires_at)
                if report.expires_at
                else "Per retention policy",
                "available_formats": formats,
            }
        )
    report_projects = [
        {"id": project.id, "name": project.name}
        for project in db.scalars(
            select(Project)
            .join(WatchlistItem, WatchlistItem.project_id == Project.id)
            .join(Watchlist, Watchlist.id == WatchlistItem.watchlist_id)
            .where(
                Project.organization_id == organization_id,
                Project.deleted_at.is_(None),
                WatchlistItem.organization_id == organization_id,
                WatchlistItem.status == "active",
                Watchlist.organization_id == organization_id,
                Watchlist.is_active.is_(True),
                Watchlist.deleted_at.is_(None),
            )
            .order_by(Project.name)
            .distinct()
        )
    ]
    return {
        "reports": reports,
        "report_projects": report_projects,
        "page": safe_page,
        "total_pages": total_pages,
    }
