from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.models import (
    Alert,
    AuditEvent,
    DetectedChange,
    Location,
    MonitoringRun,
    Organization,
    Project,
    Source,
    SourceConflict,
    SourceSnapshot,
    User,
)
from app.seed_demo import seed_demo


@pytest.fixture
def seeded_governance_ui(client, db):
    seed_demo(db)
    db.commit()
    login = client.post(
        "/login",
        data={
            "email": "analyst@projecttruth.local",
            "password": "DemoAnalyst!2026",
        },
        follow_redirects=False,
    )
    assert login.status_code == 303
    return client, db


def test_alert_conflict_and_audit_filters_preserve_selection_and_tenant_scope(
    seeded_governance_ui,
) -> None:
    client, db = seeded_governance_ui
    aster = db.scalar(
        select(Project).where(Project.registration_number == "HRERA-DEMO-101")
    )
    metro = db.scalar(
        select(Project).where(Project.registration_number == "HRERA-DEMO-404")
    )
    assert aster is not None and metro is not None
    change = db.scalar(
        select(DetectedChange).where(DetectedChange.project_id == aster.id)
    )
    changed_alert = db.scalar(
        select(Alert).where(
            Alert.project_id == aster.id,
            Alert.detected_change_id == change.id,
        )
    )
    non_change_alert = db.scalar(
        select(Alert).where(
            Alert.project_id == metro.id,
            Alert.detected_change_id.is_(None),
        )
    )
    assert change is not None and changed_alert is not None and non_change_alert is not None

    filtered_alert = client.get(
        "/alerts",
        params={
            "state": "all",
            "severity": "high",
            "project_id": aster.id,
            "evidence_id": change.id,
            "selected": changed_alert.id,
        },
    )
    assert filtered_alert.status_code == 200
    assert "Aster Heights Phase II" in filtered_alert.text
    assert f'href="/evidence/{change.id}"' in filtered_alert.text
    assert f'value="{aster.id}"' in filtered_alert.text
    assert f'value="{change.id}"' in filtered_alert.text
    assert '<option value="high" selected>' in filtered_alert.text

    safe_non_change = client.get(
        "/alerts",
        params={
            "state": "all",
            "project_id": metro.id,
            "selected": non_change_alert.id,
        },
    )
    assert safe_non_change.status_code == 200
    assert "This review item is not tied to a detected change" in safe_non_change.text
    assert "/evidence/None" not in safe_non_change.text
    assert "/evidence/none" not in safe_non_change.text.lower()
    assert f'href="/projects/{metro.id}/timeline"' in safe_non_change.text

    conflict = db.scalar(
        select(SourceConflict)
        .where(SourceConflict.project_id == metro.id)
        .order_by(SourceConflict.detected_at)
    )
    assert conflict is not None
    conflict_page = client.get(
        "/conflicts",
        params={
            "q": "Metro",
            "state": "open",
            "field": conflict.field_key,
            "project_id": metro.id,
            "selected": conflict.id,
        },
    )
    assert conflict_page.status_code == 200
    assert "Metro Square" in conflict_page.text
    assert conflict.source_a_value in conflict_page.text
    assert conflict.source_b_value in conflict_page.text
    assert f'value="{metro.id}"' in conflict_page.text
    assert f'<option value="{conflict.field_key}" selected>' in conflict_page.text

    login_event = db.scalar(
        select(AuditEvent)
        .where(
            AuditEvent.organization_id == aster.organization_id,
            AuditEvent.action == "authentication.login",
        )
        .order_by(AuditEvent.created_at.desc())
    )
    assert login_event is not None
    today = datetime.now(UTC).date().isoformat()
    audit_page = client.get(
        "/audit",
        params={
            "q": "Demo Analyst",
            "category": "authentication",
            "from": today,
            "to": today,
        },
    )
    assert audit_page.status_code == 200
    assert "authentication.login" in audit_page.text
    assert "Demo Analyst" in audit_page.text
    assert f'value="{today}"' in audit_page.text
    assert '<option value="authentication" selected>' in audit_page.text

    selected_audit = client.get("/audit", params={"event_id": login_event.id})
    assert selected_audit.status_code == 200
    assert "selected event" in selected_audit.text

    other_org = db.scalar(select(Organization).where(Organization.slug == "other-advisory"))
    other_user = db.scalar(select(User).where(User.email == "other@example.com"))
    assert other_org is not None and other_user is not None
    foreign_project = Project(
        organization_id=other_org.id,
        name="FOREIGN GOVERNANCE PROJECT",
        normalized_name="foreign governance project",
        identity_status="confirmed",
        is_provisional=False,
    )
    db.add(foreign_project)
    db.flush()
    foreign_alert = Alert(
        organization_id=other_org.id,
        project_id=foreign_project.id,
        title="FOREIGN ALERT",
        neutral_description="Must never cross tenant boundaries.",
        severity="high",
        requires_review=True,
    )
    foreign_audit = AuditEvent(
        organization_id=other_org.id,
        actor_user_id=other_user.id,
        action="foreign.secret",
        resource_type="project",
        resource_id=foreign_project.id,
        metadata_json={},
    )
    db.add_all([foreign_alert, foreign_audit])
    db.commit()
    assert client.get(f"/alerts?selected={foreign_alert.id}").status_code == 404
    assert client.get(f"/audit?event_id={foreign_audit.id}").status_code == 404


def test_project_profile_phase_map_timeline_and_coverage_use_persisted_truth(
    seeded_governance_ui,
) -> None:
    client, db = seeded_governance_ui
    aster = db.scalar(
        select(Project).where(Project.registration_number == "HRERA-DEMO-101")
    )
    phase = db.scalar(
        select(Project).where(Project.registration_number == "HRERA-DEMO-303")
    )
    metro = db.scalar(
        select(Project).where(Project.registration_number == "HRERA-DEMO-404")
    )
    assert aster is not None and phase is not None and metro is not None

    profile = client.get(f"/projects/{aster.id}")
    assert profile.status_code == 200
    assert "Aster Demo Developers Private Limited" in profile.text
    assert "Sector 79" in profile.text
    assert "ProjectTruth frozen HRERA demo fixtures" in profile.text
    assert "31 March 2028" in profile.text
    assert "Developer not recorded" not in profile.text
    assert "Location not recorded" not in profile.text
    assert 'value="case_a_completion_extended"' in profile.text

    stale_passport = client.get(f"/projects/{aster.id}/passport")
    assert stale_passport.status_code == 200
    assert "Monitoring stale" in stale_passport.text
    assert "older than 30 days" in stale_passport.text

    partial_passport = client.get(f"/projects/{metro.id}/passport")
    assert partial_passport.status_code == 200
    assert "Monitoring partial" in partial_passport.text

    source = db.get(Source, aster.registration_source_id)
    assert source is not None
    source.circuit_breaker_state = "open"
    source.health_status = "degraded"
    db.commit()
    blocked_profile = client.get(f"/projects/{aster.id}")
    blocked_passport = client.get(f"/projects/{aster.id}/passport")
    assert "Automatic access is blocked or restricted" in blocked_profile.text
    assert "Monitoring blocked" in blocked_passport.text

    phase_map = client.get(f"/projects/{phase.id}/phase-map")
    assert phase_map.status_code == 200
    assert "Orion Greens" in phase_map.text
    assert "Orion Greens Phase III" in phase_map.text
    assert "HRERA-DEMO-303" in phase_map.text
    assert "Explicit parent link; developer and locality match" in phase_map.text
    assert "Requires review" not in phase_map.text

    timeline = client.get(f"/projects/{phase.id}/timeline")
    assert timeline.status_code == 200
    assert "New phase or registration observed" in timeline.text
    assert "Included in report" in timeline.text
    assert "Registration Number changed" in timeline.text

    phase_only = client.get(
        f"/projects/{phase.id}/timeline",
        params={"event_type": "phase", "from": "2026-04-07", "to": "2026-04-07"},
    )
    assert phase_only.status_code == 200
    assert "New phase or registration observed" in phase_only.text
    assert "Baseline captured" not in phase_only.text
    assert '<option value="phase" selected>' in phase_only.text
    assert 'value="2026-04-07"' in phase_only.text


def test_change_ledger_filters_and_source_health_parser_attempts_are_real(
    seeded_governance_ui,
) -> None:
    client, db = seeded_governance_ui
    aster = db.scalar(
        select(Project).where(Project.registration_number == "HRERA-DEMO-101")
    )
    change = db.scalar(
        select(DetectedChange).where(DetectedChange.project_id == aster.id)
    )
    source = db.get(Source, change.source_id) if change else None
    assert aster is not None and change is not None and source is not None

    filtered = client.get(
        f"/projects/{aster.id}/changes",
        params={
            "field": change.field_key,
            "source_id": source.id,
            "severity": change.severity,
            "review_status": change.review_state,
            "change_type": change.change_type,
            "date_from": "2026-02-02",
            "date_to": "2026-02-02",
        },
    )
    assert filtered.status_code == 200
    assert "+275 days" in filtered.text
    assert f'<option value="{source.id}" selected>' in filtered.text
    assert f'<option value="{change.field_key}" selected>' in filtered.text
    assert f'<option value="{change.change_type}" selected>' in filtered.text
    assert 'value="2026-02-02"' in filtered.text

    outside_period = client.get(
        f"/projects/{aster.id}/changes",
        params={"date_from": "2026-03-01", "date_to": "2026-03-31"},
    )
    assert outside_period.status_code == 200
    assert "No compatible changes" in outside_period.text
    assert "+275 days" not in outside_period.text

    db.add(
        MonitoringRun(
            organization_id=aster.organization_id,
            source_id=source.id,
            project_id=aster.id,
            status="failed",
            access_completeness="unavailable",
            error_category="parser_failed",
            error_message="Synthetic parser failure for rendered health test.",
            deduplication_key="ui-parser-failure-attempt",
        )
    )
    db.commit()
    source_health = client.get("/sources/health")
    assert source_health.status_code == 200
    source_row = re.search(
        rf"<tr><td><strong>{re.escape(source.name)}</strong>.*?</tr>",
        source_health.text,
        flags=re.DOTALL,
    )
    assert source_row is not None
    rates = re.findall(r'class="health-cell">(\d+)%', source_row.group(0))
    assert len(rates) == 2
    assert int(rates[1]) < 100

