from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.api.v1 import imports as imports_api
from app.api.v1 import monitoring as monitoring_api
from app.collectors.base import CollectorPolicyError
from app.collectors.fixtures import FixtureCollector
from app.collectors.hrera_gurugram import HRERAGurugramCollector
from app.core.config import get_settings
from app.domain import (
    AccessCompleteness,
    AccessStatus,
    ErrorCategory,
    FetchRequestContext,
    FetchResult,
    SourceAccessPolicy,
)
from app.jobs.scheduler import LocalMonitoringScheduler
from app.jobs.tasks import run_monitoring_task
from app.main import _dispatch_due_monitoring_jobs, app
from app.models import (
    Alert,
    DetectedChange,
    MonitoringJob,
    MonitoringRun,
    Project,
    ProjectSourceLink,
    Signal,
    Source,
    SourceConflict,
    SourceSnapshot,
)
from app.parsers.fixture_html import FixtureHTMLParser
from app.schemas import watchlists as watchlist_schemas
from app.services.reliability import CircuitBreaker, CircuitState, RetryPolicy
from app.services.scheduler import ActiveJobGate


def _configured_settings(**updates: object):
    return get_settings().model_copy(update=updates)


def test_live_hrera_gate_leaves_fixture_collection_deterministic(monkeypatch) -> None:
    monkeypatch.setattr(
        monitoring_api,
        "get_settings",
        lambda: _configured_settings(enable_live_hrera=False),
    )
    source = SimpleNamespace(source_type="fixture", id="fixture-source")
    collector = monitoring_api._collector_for(source)

    assert isinstance(collector, FixtureCollector)
    policy = SourceAccessPolicy(
        source_name="Frozen fixture",
        source_type="fixture",
        base_url="fixture://case_a_completion_old",
        allowed_domains=(),
        enabled=True,
        permitted_access_modes=frozenset({"fixture"}),
        robots_review_status="not_applicable",
        terms_review_status="not_applicable",
        allowed_schemes=frozenset({"fixture"}),
    )
    context = FetchRequestContext(
        source_id=source.id,
        requested_url="fixture://case_a_completion_old",
        fixture_key="case_a_completion_old",
        policy=policy,
    )

    first = asyncio.run(collector.fetch(context))
    second = asyncio.run(collector.fetch(context))

    assert first.is_successful and second.is_successful
    assert first.content == second.content
    assert first.content_hash == second.content_hash


def test_live_hrera_collector_requires_configuration_and_unknown_types_fail_closed(
    monkeypatch,
) -> None:
    source = SimpleNamespace(source_type="hrera_gurugram", id="live-source")
    monkeypatch.setattr(
        monitoring_api,
        "get_settings",
        lambda: _configured_settings(enable_live_hrera=False),
    )

    with pytest.raises(CollectorPolicyError, match="disabled") as disabled:
        monitoring_api._collector_for(source)
    assert disabled.value.code == "live_hrera_disabled"

    monkeypatch.setattr(
        monitoring_api,
        "get_settings",
        lambda: _configured_settings(enable_live_hrera=True),
    )
    assert isinstance(monitoring_api._collector_for(source), HRERAGurugramCollector)

    source.source_type = "unrecognized_live_source"
    with pytest.raises(CollectorPolicyError) as unsupported:
        monitoring_api._collector_for(source)
    assert unsupported.value.code == "unsupported_source_type"


def test_disabled_and_unknown_live_refreshes_preserve_verified_facts(
    client, auth, db, monkeypatch
) -> None:
    headers = auth("analyst")
    watchlist = client.post(
        "/api/v1/watchlists",
        json={"name": "Live source safety", "monitoring_cadence": "weekly"},
        headers=headers,
    ).json()
    project_payload = client.post(
        "/api/v1/projects",
        json={
            "registration_number": "HRERA-LIVE-SAFETY-1",
            "name": "Live Safety Project",
            "city": "Gurugram",
        },
        headers=headers,
    ).json()
    item = client.post(
        f"/api/v1/watchlists/{watchlist['id']}/items",
        json={"project_id": project_payload["id"], "monitoring_cadence": "weekly"},
        headers=headers,
    ).json()
    source_payload = client.post(
        f"/api/v1/projects/{project_payload['id']}/sources",
        json={
            "source_type": "hrera_gurugram",
            "name": "Policy-gated live HRERA",
            "url": "https://haryanarera.gov.in/project/live-safety-1",
        },
        headers=headers,
    ).json()

    verified_at = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    project = db.get(Project, project_payload["id"])
    assert project is not None
    project.current_status = "Registered"
    project.proposed_completion_date = date(2028, 12, 31)
    project.last_verified_at = verified_at
    db.commit()

    monkeypatch.setattr(
        monitoring_api,
        "get_settings",
        lambda: _configured_settings(enable_live_hrera=False),
    )
    disabled = client.post(
        f"/api/v1/watchlist-items/{item['id']}/refresh",
        json={"idempotency_key": "disabled-live-refresh"},
        headers=headers,
    )

    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["monitoring_run"]["status"] == "failed"
    assert disabled.json()["monitoring_run"]["error_category"] == "live_hrera_disabled"
    assert disabled.json()["changes_created"] == 0

    source = db.get(Source, source_payload["id"])
    assert source is not None
    source.source_type = "unrecognized_live_source"
    db.commit()
    unsupported = client.post(
        f"/api/v1/watchlist-items/{item['id']}/refresh",
        json={"idempotency_key": "unknown-live-refresh"},
        headers=headers,
    )

    assert unsupported.status_code == 200, unsupported.text
    assert unsupported.json()["monitoring_run"]["status"] == "failed"
    assert unsupported.json()["monitoring_run"]["error_category"] == "unsupported_source_type"
    assert unsupported.json()["changes_created"] == 0

    db.expire_all()
    preserved = db.get(Project, project_payload["id"])
    assert preserved is not None
    assert preserved.current_status == "Registered"
    assert preserved.proposed_completion_date == date(2028, 12, 31)
    assert preserved.last_verified_at == verified_at
    assert (
        db.scalar(
            select(func.count())
            .select_from(SourceSnapshot)
            .where(SourceSnapshot.project_id == preserved.id)
        )
        == 0
    )
    assert (
        db.scalar(
            select(func.count())
            .select_from(DetectedChange)
            .where(DetectedChange.project_id == preserved.id)
        )
        == 0
    )


def test_configured_default_cadence_applies_to_watchlists_and_csv(
    client, auth, monkeypatch
) -> None:
    configured = _configured_settings(default_monitoring_cadence="daily")
    monkeypatch.setattr(watchlist_schemas, "get_settings", lambda: configured)
    monkeypatch.setattr(imports_api, "get_settings", lambda: configured)
    headers = auth("analyst")

    watchlist = client.post(
        "/api/v1/watchlists",
        json={"name": "Configured cadence"},
        headers=headers,
    )
    assert watchlist.status_code == 201, watchlist.text
    assert watchlist.json()["monitoring_cadence"] == "daily"

    preview = client.post(
        "/api/v1/imports/projects/preview",
        json={
            "csv_text": (
                "registration_number,project_name,developer_name,city,"
                "locality_or_sector,official_url,monitoring_cadence\n"
                "HRERA-CADENCE-1,Configured CSV,Demo Developer,Gurugram,"
                "Sector 1,,\n"
            )
        },
        headers=headers,
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["rows"][0]["normalized"]["monitoring_cadence"] == "daily"


def test_disabled_scheduler_flag_keeps_lifespan_and_lock_inactive(client, tmp_path) -> None:
    assert app.state.scheduler_started is False
    lock_path = tmp_path / "scheduler.lock"
    scheduler = LocalMonitoringScheduler(
        lambda: None,
        enabled=False,
        lock_path=lock_path,
    )

    assert scheduler.start() is False
    assert scheduler.running is False
    assert lock_path.exists() is False


class _SequenceCollector:
    def __init__(self, results: list[FetchResult]) -> None:
        self.results = results
        self.calls = 0

    async def fetch(self, context: FetchRequestContext) -> FetchResult:
        del context
        result = self.results[min(self.calls, len(self.results) - 1)]
        self.calls += 1
        return result


def _task_context() -> FetchRequestContext:
    return FetchRequestContext(
        source_id="runner-source",
        requested_url="fixture://runner",
        fixture_key="runner",
        policy=SourceAccessPolicy(
            source_name="Runner fixture",
            source_type="fixture",
            base_url="fixture://runner",
            allowed_domains=(),
            enabled=True,
            permitted_access_modes=frozenset({"fixture"}),
            robots_review_status="not_applicable",
            terms_review_status="not_applicable",
            allowed_schemes=frozenset({"fixture"}),
        ),
    )


def _network_failure() -> FetchResult:
    return FetchResult(
        requested_url="fixture://runner",
        final_url="fixture://runner",
        source_id="runner-source",
        http_status=503,
        fetched_at=datetime.now(UTC),
        content_type=None,
        access_status=AccessStatus.UNAVAILABLE,
        parser_eligible=False,
        completeness=AccessCompleteness.UNAVAILABLE,
        error_category=ErrorCategory.NETWORK,
    )


def _successful_fixture() -> FetchResult:
    return FetchResult(
        requested_url="fixture://runner",
        final_url="fixture://runner",
        source_id="runner-source",
        http_status=200,
        fetched_at=datetime.now(UTC),
        content_type="text/html",
        content=(
            b'<html><body><p data-field="registration_number">HRERA-RUNNER-1</p></body></html>'
        ),
        access_status=AccessStatus.SUCCESS,
        parser_eligible=True,
        completeness=AccessCompleteness.COMPLETE,
    )


def test_monitoring_task_retries_transient_results_and_opens_circuit() -> None:
    retry_policy = RetryPolicy(
        max_attempts=3,
        base_delay_seconds=0,
        maximum_delay_seconds=0,
        jitter_ratio=0,
    )
    recovering = _SequenceCollector(
        [_network_failure(), _network_failure(), _successful_fixture()]
    )
    breaker = CircuitBreaker(failure_threshold=1)
    recovered = asyncio.run(
        run_monitoring_task(
            job_key="retry-then-success",
            context=_task_context(),
            collector=recovering,
            parser=FixtureHTMLParser(),
            active_jobs=ActiveJobGate(),
            circuit_breaker=breaker,
            retry_policy=retry_policy,
        )
    )
    assert recovered.status == "succeeded"
    assert recovered.attempts == 3
    assert recovering.calls == 3
    assert breaker.state == CircuitState.CLOSED

    failing = _SequenceCollector([_network_failure()])
    failed = asyncio.run(
        run_monitoring_task(
            job_key="open-circuit",
            context=_task_context(),
            collector=failing,
            parser=FixtureHTMLParser(),
            active_jobs=ActiveJobGate(),
            circuit_breaker=breaker,
            retry_policy=retry_policy,
        )
    )
    assert failed.status == "failed"
    assert failed.attempts == 3
    assert breaker.state == CircuitState.OPEN
    blocked = asyncio.run(
        run_monitoring_task(
            job_key="circuit-blocks-next-run",
            context=_task_context(),
            collector=failing,
            parser=FixtureHTMLParser(),
            active_jobs=ActiveJobGate(),
            circuit_breaker=breaker,
            retry_policy=retry_policy,
        )
    )
    assert blocked.status == "circuit_open"
    assert blocked.attempts == 0
    assert failing.calls == 3


def _scheduled_project(client, headers, *, registration_number: str):
    watchlist = client.post(
        "/api/v1/watchlists",
        json={"name": f"Scheduled {registration_number}", "monitoring_cadence": "weekly"},
        headers=headers,
    ).json()
    project = client.post(
        "/api/v1/projects",
        json={"registration_number": registration_number, "name": "Scheduled project"},
        headers=headers,
    ).json()
    item = client.post(
        f"/api/v1/watchlists/{watchlist['id']}/items",
        json={"project_id": project["id"], "monitoring_cadence": "weekly"},
        headers=headers,
    ).json()
    return project, item


def test_scheduler_honors_job_source_and_persists_cross_source_conflict(
    client, auth, db
) -> None:
    headers = auth("analyst")
    project, item = _scheduled_project(
        client, headers, registration_number="HRERA-DEMO-101"
    )
    official = client.post(
        f"/api/v1/projects/{project['id']}/sources",
        json={
            "source_type": "fixture",
            "name": "Aster regulator endpoint",
            "fixture_key": "case_a_completion_old",
        },
        headers=headers,
    ).json()
    baseline = client.post(
        f"/api/v1/watchlist-items/{item['id']}/refresh",
        json={
            "idempotency_key": "scheduled-source-official-baseline",
            "fixture_key": "case_a_completion_old",
        },
        headers=headers,
    )
    assert baseline.status_code == 200, baseline.text
    developer = client.post(
        f"/api/v1/projects/{project['id']}/sources",
        json={
            "source_type": "fixture",
            "name": "Aster developer endpoint",
            "fixture_key": "case_a_developer_claim",
        },
        headers=headers,
    ).json()

    developer_source = db.get(Source, developer["id"])
    project_row = db.get(Project, project["id"])
    assert developer_source is not None and project_row is not None
    developer_source.source_class = "official_developer"
    project_row.registration_source_id = official["id"]
    jobs = list(
        db.scalars(
            select(MonitoringJob).where(MonitoringJob.project_id == project["id"])
        )
    )
    due_at = datetime.now(UTC) - timedelta(minutes=1)
    developer_job = next(job for job in jobs if job.source_id == developer["id"])
    for job in jobs:
        job.next_run_at = (
            due_at if job.id == developer_job.id else datetime.now(UTC) + timedelta(days=1)
        )
    db.commit()

    asyncio.run(_dispatch_due_monitoring_jobs())
    db.expire_all()

    scheduled_run = db.scalar(
        select(MonitoringRun)
        .where(MonitoringRun.monitoring_job_id == developer_job.id)
        .order_by(MonitoringRun.created_at.desc())
    )
    assert scheduled_run is not None
    assert scheduled_run.status == "succeeded"
    assert scheduled_run.source_id == developer["id"]
    assert scheduled_run.trigger_type == "scheduled"
    developer_snapshot = db.scalar(
        select(SourceSnapshot).where(
            SourceSnapshot.monitoring_run_id == scheduled_run.id
        )
    )
    assert developer_snapshot is not None
    assert developer_snapshot.source_id == developer["id"]
    preserved_project = db.get(Project, project["id"])
    assert preserved_project is not None
    assert preserved_project.proposed_completion_date == date(2027, 6, 30)
    conflict = db.scalar(
        select(SourceConflict).where(
            SourceConflict.project_id == project["id"],
            SourceConflict.field_key == "proposed_completion_date",
        )
    )
    assert conflict is not None
    assert {conflict.source_a_class, conflict.source_b_class} == {
        "official_regulator",
        "official_developer",
    }
    assert conflict.source_a_value != conflict.source_b_value
    conflict_change = db.scalar(
        select(DetectedChange).where(
            DetectedChange.project_id == project["id"],
            DetectedChange.change_type == "source_conflict",
            DetectedChange.field_key == "proposed_completion_date",
        )
    )
    assert conflict_change is not None
    assert conflict_change.change_confidence == 1.0
    conflict_signal = db.scalar(
        select(Signal).where(
            Signal.project_id == project["id"],
            Signal.rule_code == "OFFICIAL_VS_DEVELOPER_CONFLICT",
        )
    )
    assert conflict_signal is not None
    conflict_alert = db.scalar(
        select(Alert).where(
            Alert.project_id == project["id"],
            Alert.signal_id == conflict_signal.id,
        )
    )
    assert conflict_alert is not None
    assert conflict_alert.requires_review is True
    assert conflict_alert.old_value and conflict_alert.new_value


def test_dispatcher_schedules_short_backoff_when_refresh_returns_failure(
    client, auth, db
) -> None:
    headers = auth("analyst")
    project, _item = _scheduled_project(
        client, headers, registration_number="HRERA-SCHEDULER-FAIL-1"
    )
    source = client.post(
        f"/api/v1/projects/{project['id']}/sources",
        json={
            "source_type": "fixture",
            "name": "Unavailable scheduled endpoint",
            "fixture_key": "fixture-does-not-exist",
        },
        headers=headers,
    ).json()
    job = db.scalar(
        select(MonitoringJob).where(
            MonitoringJob.project_id == project["id"],
            MonitoringJob.source_id == source["id"],
        )
    )
    assert job is not None
    dispatch_started = datetime.now(UTC)
    job.next_run_at = dispatch_started - timedelta(minutes=1)
    db.commit()

    asyncio.run(_dispatch_due_monitoring_jobs())
    db.expire_all()

    refreshed_job = db.get(MonitoringJob, job.id)
    assert refreshed_job is not None
    assert refreshed_job.state == "retry"
    assert refreshed_job.retry_count == 1
    assert refreshed_job.next_run_at is not None
    assert dispatch_started + timedelta(minutes=4) <= refreshed_job.next_run_at
    assert refreshed_job.next_run_at <= dispatch_started + timedelta(minutes=6)
    failed_run = db.scalar(
        select(MonitoringRun).where(MonitoringRun.monitoring_job_id == job.id)
    )
    assert failed_run is not None
    assert failed_run.status == "failed"


def test_same_named_sources_on_one_domain_keep_project_specific_urls(
    client, auth
) -> None:
    headers = auth("analyst")
    first = client.post(
        "/api/v1/projects",
        json={"registration_number": "HRERA-ENDPOINT-1", "name": "Endpoint one"},
        headers=headers,
    ).json()
    second = client.post(
        "/api/v1/projects",
        json={"registration_number": "HRERA-ENDPOINT-2", "name": "Endpoint two"},
        headers=headers,
    ).json()
    first_source = client.post(
        f"/api/v1/projects/{first['id']}/sources",
        json={
            "source_type": "developer_page",
            "name": "Developer project page",
            "url": "https://developer.example/projects/endpoint-one/",
        },
        headers=headers,
    )
    second_source = client.post(
        f"/api/v1/projects/{second['id']}/sources",
        json={
            "source_type": "developer_page",
            "name": "Developer project page",
            "url": "https://developer.example/projects/endpoint-two",
        },
        headers=headers,
    )

    assert first_source.status_code == 201, first_source.text
    assert second_source.status_code == 201, second_source.text
    assert first_source.json()["id"] != second_source.json()["id"]
    assert first_source.json()["base_url"] != second_source.json()["base_url"]


def test_source_policy_round_trips_every_submitted_control(client, auth) -> None:
    headers = auth("admin")
    created = client.post(
        "/api/v1/source-policies",
        json={
            "source_name": "Reviewed developer policy",
            "source_type": "developer_page",
            "base_url": "https://policy.example/project/101",
            "enabled": True,
            "permitted_access_modes": ["httpx", "playwright"],
            "rate_limit_seconds": 7.5,
            "robots_review_status": "approved",
            "terms_review_status": "approved",
            "evidence_retention_days": 365,
            "raw_content_allowed": True,
            "playwright_allowed": True,
            "ocr_allowed": True,
            "llm_allowed": True,
            "policy_version": "2.0",
        },
        headers=headers,
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["is_enabled"] is True
    assert body["permitted_access_modes"] == ["httpx", "playwright"]
    assert body["rate_limit_period_seconds"] == 7.5
    assert body["robots_review_status"] == "approved"
    assert body["terms_review_status"] == "approved"
    assert body["evidence_retention_days"] == 365
    assert body["raw_content_may_be_stored"] is True
    assert body["playwright_allowed"] is True
    assert body["ocr_allowed"] is True
    assert body["llm_allowed"] is True
    assert body["policy_version"] == "2.0"

    updated = client.patch(
        f"/api/v1/source-policies/{body['id']}",
        json={
            "enabled": False,
            "permitted_access_modes": ["httpx"],
            "rate_limit_seconds": 11,
            "robots_review_status": "recheck",
            "terms_review_status": "recheck",
            "evidence_retention_days": 30,
            "raw_content_allowed": False,
            "playwright_allowed": False,
            "ocr_allowed": False,
            "llm_allowed": False,
            "policy_version": "2.1",
        },
        headers=headers,
    )
    assert updated.status_code == 200, updated.text
    revised = updated.json()
    assert revised["is_enabled"] is False
    assert revised["permitted_access_modes"] == ["httpx"]
    assert revised["rate_limit_period_seconds"] == 11
    assert revised["robots_review_status"] == "recheck"
    assert revised["terms_review_status"] == "recheck"
    assert revised["evidence_retention_days"] == 30
    assert revised["raw_content_may_be_stored"] is False
    assert revised["playwright_allowed"] is False
    assert revised["ocr_allowed"] is False
    assert revised["llm_allowed"] is False
    assert revised["policy_version"] == "2.1"


def test_sources_added_before_watchlist_are_scheduled_and_unlinked_source_is_blocked(
    client, auth, db
) -> None:
    headers = auth("analyst")
    project = client.post(
        "/api/v1/projects",
        json={"registration_number": "HRERA-DEMO-101", "name": "Prelinked Aster"},
        headers=headers,
    ).json()
    official = client.post(
        f"/api/v1/projects/{project['id']}/sources",
        json={
            "source_type": "fixture",
            "name": "Prelinked regulator",
            "fixture_key": "case_a_completion_old",
        },
        headers=headers,
    ).json()
    developer = client.post(
        f"/api/v1/projects/{project['id']}/sources",
        json={
            "source_type": "fixture",
            "name": "Prelinked developer",
            "fixture_key": "case_a_developer_claim",
        },
        headers=headers,
    ).json()
    project_row = db.get(Project, project["id"])
    developer_row = db.get(Source, developer["id"])
    assert project_row is not None and developer_row is not None
    project_row.registration_source_id = official["id"]
    developer_row.source_class = "official_developer"
    db.commit()

    watchlist = client.post(
        "/api/v1/watchlists",
        json={"name": "Prelinked monitoring", "monitoring_cadence": "weekly"},
        headers=headers,
    ).json()
    item = client.post(
        f"/api/v1/watchlists/{watchlist['id']}/items",
        json={"project_id": project["id"], "monitoring_cadence": "weekly"},
        headers=headers,
    ).json()
    jobs = list(
        db.scalars(
            select(MonitoringJob).where(
                MonitoringJob.project_id == project["id"],
            )
        )
    )
    assert all(job.payload.get("watchlist_item_id") == item["id"] for job in jobs)
    assert {job.source_id for job in jobs} == {official["id"], developer["id"]}
    assert db.scalar(
        select(ProjectSourceLink.id).where(
            ProjectSourceLink.project_id == project["id"],
            ProjectSourceLink.source_id == developer["id"],
        )
    )

    official_refresh = client.post(
        f"/api/v1/watchlist-items/{item['id']}/refresh",
        json={
            "idempotency_key": "prelinked-official-refresh",
            "source_id": official["id"],
        },
        headers=headers,
    )
    developer_refresh = client.post(
        f"/api/v1/watchlist-items/{item['id']}/refresh",
        json={
            "idempotency_key": "prelinked-developer-refresh",
            "source_id": developer["id"],
        },
        headers=headers,
    )
    assert official_refresh.status_code == 200, official_refresh.text
    assert developer_refresh.status_code == 200, developer_refresh.text
    assert developer_refresh.json()["source_conflicts"] >= 1

    provisional = client.post(
        "/api/v1/projects",
        json={"name": "Unverified provisional project"},
        headers=headers,
    ).json()
    provisional_item = client.post(
        f"/api/v1/watchlists/{watchlist['id']}/items",
        json={"project_id": provisional["id"], "monitoring_cadence": "manual"},
        headers=headers,
    ).json()
    blocked = client.post(
        f"/api/v1/watchlist-items/{provisional_item['id']}/refresh",
        json={
            "idempotency_key": "unlinked-provisional-source",
            "source_id": developer["id"],
        },
        headers=headers,
    )
    assert blocked.status_code == 404
    assert blocked.json()["error"]["code"] == "project_source_not_found"


def test_collector_instances_are_reused_for_cross_run_rate_state() -> None:
    source = SimpleNamespace(
        organization_id="rate-org",
        id="rate-source",
        source_type="developer_page",
    )
    first = monitoring_api._collector_for(source)
    second = monitoring_api._collector_for(source)
    assert first is second


def test_refresh_forwards_persisted_conditional_headers(
    client, auth, monkeypatch
) -> None:
    headers = auth("analyst")
    project, item = _scheduled_project(
        client, headers, registration_number="HRERA-DEMO-101"
    )
    source = client.post(
        f"/api/v1/projects/{project['id']}/sources",
        json={
            "source_type": "fixture",
            "name": "Conditional fixture",
            "fixture_key": "case_a_completion_old",
        },
        headers=headers,
    ).json()
    fixture_content = monitoring_api._fixture_map()[0]["case_a_completion_old"].read_bytes()

    class _ConditionalCollector:
        contexts: list[FetchRequestContext]

        def __init__(self) -> None:
            self.contexts = []

        async def fetch(self, context: FetchRequestContext) -> FetchResult:
            self.contexts.append(context)
            if len(self.contexts) == 1:
                return FetchResult(
                    requested_url=context.requested_url,
                    final_url=context.requested_url,
                    source_id=context.source_id,
                    http_status=200,
                    fetched_at=datetime.now(UTC),
                    content_type="text/html",
                    content=fixture_content,
                    access_status=AccessStatus.SUCCESS,
                    parser_eligible=True,
                    completeness=AccessCompleteness.COMPLETE,
                    response_metadata={
                        "etag": '"project-v1"',
                        "last-modified": "Sun, 02 Aug 2026 10:00:00 GMT",
                    },
                )
            return FetchResult(
                requested_url=context.requested_url,
                final_url=context.requested_url,
                source_id=context.source_id,
                http_status=304,
                fetched_at=datetime.now(UTC),
                content_type="text/html",
                access_status=AccessStatus.SUCCESS,
                parser_eligible=False,
                completeness=AccessCompleteness.COMPLETE,
            )

    collector = _ConditionalCollector()
    monkeypatch.setattr(monitoring_api, "_collector_for", lambda selected: collector)
    baseline = client.post(
        f"/api/v1/watchlist-items/{item['id']}/refresh",
        json={"idempotency_key": "conditional-baseline"},
        headers=headers,
    )
    assert baseline.status_code == 200, baseline.text
    unchanged = client.post(
        f"/api/v1/watchlist-items/{item['id']}/refresh",
        json={
            "idempotency_key": "conditional-not-modified",
            "source_id": source["id"],
        },
        headers=headers,
    )
    assert unchanged.status_code == 200, unchanged.text
    assert unchanged.json()["duplicate"] is True
    assert len(collector.contexts) == 2
    conditional_context = collector.contexts[1]
    assert conditional_context.conditional_etag == '"project-v1"'
    assert (
        conditional_context.conditional_last_modified
        == "Sun, 02 Aug 2026 10:00:00 GMT"
    )
