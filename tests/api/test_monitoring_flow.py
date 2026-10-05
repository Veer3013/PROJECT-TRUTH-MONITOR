from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from app.api.v1 import monitoring as monitoring_api
from app.models import DetectedChange


def test_fixture_map_prefers_runtime_copy_and_keeps_test_fallback(
    tmp_path: Path, monkeypatch
) -> None:
    runtime_root = tmp_path / "app" / "fixtures" / "html"
    test_root = tmp_path / "tests" / "fixtures" / "html"
    test_root.mkdir(parents=True)
    (test_root / "fallback.html").write_text("fallback", encoding="utf-8")
    monkeypatch.setattr(monitoring_api, "ROOT_DIR", tmp_path)

    fixtures, selected_root = monitoring_api._fixture_map()
    assert selected_root == test_root.resolve()
    assert fixtures["fallback"] == (test_root / "fallback.html")

    runtime_root.mkdir(parents=True)
    (runtime_root / "runtime.html").write_text("runtime", encoding="utf-8")
    fixtures, selected_root = monitoring_api._fixture_map()
    assert selected_root == runtime_root.resolve()
    assert fixtures["runtime.html"] == (runtime_root / "runtime.html")


def _setup_aster(client, headers):
    watchlist = client.post(
        "/api/v1/watchlists",
        json={"name": "Aster monitoring", "monitoring_cadence": "weekly"},
        headers=headers,
    ).json()
    project = client.post(
        "/api/v1/projects",
        json={
            "registration_number": "HRERA-DEMO-101",
            "name": "Aster Heights Phase II",
            "developer_name": "Aster Demo Developers Private Limited",
            "city": "Gurugram",
            "locality_or_sector": "Sector 79",
        },
        headers=headers,
    ).json()
    item = client.post(
        f"/api/v1/watchlists/{watchlist['id']}/items",
        json={"project_id": project["id"], "monitoring_cadence": "weekly"},
        headers=headers,
    ).json()
    source = client.post(
        f"/api/v1/projects/{project['id']}/sources",
        json={
            "source_type": "fixture",
            "name": "Aster official fixture",
            "fixture_key": "case_a_completion_old",
        },
        headers=headers,
    )
    assert source.status_code == 201, source.text
    return project, item


def test_two_snapshots_create_plus_275_change_evidence_and_signal(
    client, auth, db
) -> None:
    headers = auth("analyst")
    project, item = _setup_aster(client, headers)
    first = client.post(
        f"/api/v1/watchlist-items/{item['id']}/refresh",
        json={"idempotency_key": "aster-baseline-run", "fixture_key": "case_a_completion_old"},
        headers=headers,
    )
    assert first.status_code == 200, first.text
    assert first.json()["changes_created"] == 0
    second = client.post(
        f"/api/v1/watchlist-items/{item['id']}/refresh",
        json={"idempotency_key": "aster-extension-run", "fixture_key": "case_a_completion_extended"},
        headers=headers,
    )
    assert second.status_code == 200, second.text
    changes = client.get(f"/api/v1/projects/{project['id']}/changes", headers=headers)
    assert changes.status_code == 200
    extension = next(item for item in changes.json() if item["change_type"] == "date_extended")
    assert int(extension["magnitude"]) == 275
    assert extension["old_snapshot_id"] and extension["new_snapshot_id"]
    evidence = client.get(
        f"/api/v1/changes/{extension['id']}/evidence", headers=headers
    )
    assert evidence.status_code == 200
    assert {item["evidence_side"] for item in evidence.json()} == {"old", "new"}
    signals = client.get(f"/api/v1/projects/{project['id']}/signals", headers=headers)
    completion_signal = next(
        item for item in signals.json() if item["rule_code"] == "COMPLETION_DATE_EXTENDED"
    )
    impact = client.get(
        f"/api/v1/projects/{project['id']}/signals/{completion_signal['id']}/impact-actions",
        headers=headers,
    )
    assert impact.status_code == 200
    assert {item["role"] for item in impact.json()} >= {"property_adviser", "supplier"}
    assert all("Suggested review action only" in item["limitation"] for item in impact.json())

    stored_change = db.scalar(
        select(DetectedChange).where(DetectedChange.id == extension["id"])
    )
    assert stored_change is not None
    assert stored_change.source_reliability == 0.9
    assert stored_change.source_access_completeness == "complete"
    assert stored_change.identity_confidence == 1.0
    assert stored_change.extraction_confidence == 1.0
    assert stored_change.change_confidence == 1.0

    filtered = client.get(
        f"/api/v1/projects/{project['id']}/changes?field_key=revised_completion_date",
        headers=headers,
    )
    assert len(filtered.json()) == 1
    assert (
        client.get(
            f"/api/v1/projects/{project['id']}/changes?field_key=unit_count",
            headers=headers,
        ).json()
        == []
    )

    timeline = client.get(f"/api/v1/projects/{project['id']}/timeline", headers=headers)
    assert timeline.status_code == 200
    event_types = {item["event_type"] for item in timeline.json()}
    assert {"project_identity", "baseline_capture", "date_extended"} <= event_types


def test_duplicate_refresh_does_not_duplicate_snapshot_or_alert(client, auth) -> None:
    headers = auth("analyst")
    project, item = _setup_aster(client, headers)
    body = {"idempotency_key": "same-run-key-101", "fixture_key": "case_a_completion_old"}
    first = client.post(
        f"/api/v1/watchlist-items/{item['id']}/refresh", json=body, headers=headers
    )
    second = client.post(
        f"/api/v1/watchlist-items/{item['id']}/refresh", json=body, headers=headers
    )
    assert first.status_code == 200 and second.status_code == 200
    assert second.json()["duplicate"] is True
    assert client.get(f"/api/v1/projects/{project['id']}/changes", headers=headers).json() == []


def test_layout_failure_preserves_last_verified_facts(client, auth) -> None:
    headers = auth("analyst")
    project, item = _setup_aster(client, headers)
    client.post(
        f"/api/v1/watchlist-items/{item['id']}/refresh",
        json={"idempotency_key": "valid-first-run", "fixture_key": "case_a_completion_old"},
        headers=headers,
    )
    failed = client.post(
        f"/api/v1/watchlist-items/{item['id']}/refresh",
        json={"idempotency_key": "layout-failure-run", "fixture_key": "layout_changed"},
        headers=headers,
    )
    assert failed.status_code == 200
    assert failed.json()["changes_created"] == 0
    passport = client.get(f"/api/v1/projects/{project['id']}/passport", headers=headers).json()
    assert passport["last_successful_verification"] is not None
    health = client.get("/api/v1/source-health", headers=headers)
    assert health.status_code == 200
    assert health.json()[0]["parser_success_rate"] == 0.5


def test_repeated_extension_signal_uses_persisted_change_history(client, auth) -> None:
    headers = auth("analyst")
    project, item = _setup_aster(client, headers)
    for key, fixture in (
        ("repeat-baseline", "case_a_completion_old"),
        ("repeat-first-extension", "case_a_completion_extended"),
        ("repeat-second-extension", "case_a_completion_extended_again"),
    ):
        response = client.post(
            f"/api/v1/watchlist-items/{item['id']}/refresh",
            json={"idempotency_key": key, "fixture_key": fixture},
            headers=headers,
        )
        assert response.status_code == 200, response.text

    signals = client.get(f"/api/v1/projects/{project['id']}/signals", headers=headers)
    assert signals.status_code == 200
    assert "REPEATED_EXTENSION" in {item["rule_code"] for item in signals.json()}


def test_registration_mismatch_fails_closed_before_snapshot_or_diff(
    client, auth, db
) -> None:
    headers = auth("analyst")
    project, item = _setup_aster(client, headers)
    mismatch = client.post(
        f"/api/v1/watchlist-items/{item['id']}/refresh",
        json={
            "idempotency_key": "mismatched-project-identity",
            "fixture_key": "case_b_official_record",
        },
        headers=headers,
    )

    assert mismatch.status_code == 200, mismatch.text
    assert mismatch.json()["monitoring_run"]["status"] == "failed"
    assert mismatch.json()["monitoring_run"]["error_category"] == "project_identity_mismatch"
    assert mismatch.json()["changes_created"] == 0
    assert db.scalar(
        select(DetectedChange).where(DetectedChange.project_id == project["id"])
    ) is None
    assert client.get(
        f"/api/v1/projects/{project['id']}/changes", headers=headers
    ).json() == []


def test_qpr_false_does_not_fabricate_flag_and_true_clear_lapsed_rules_fire(
    client, auth
) -> None:
    headers = auth("analyst")
    watchlist = client.post(
        "/api/v1/watchlists",
        json={"name": "Cedar rules", "monitoring_cadence": "weekly"},
        headers=headers,
    ).json()
    project = client.post(
        "/api/v1/projects",
        json={"registration_number": "HRERA-DEMO-505", "name": "Cedar Court"},
        headers=headers,
    ).json()
    item = client.post(
        f"/api/v1/watchlists/{watchlist['id']}/items",
        json={"project_id": project["id"], "monitoring_cadence": "weekly"},
        headers=headers,
    ).json()
    source = client.post(
        f"/api/v1/projects/{project['id']}/sources",
        json={
            "source_type": "fixture",
            "name": "Cedar official fixture",
            "fixture_key": "clean_project_page",
        },
        headers=headers,
    )
    assert source.status_code == 201, source.text

    def refresh(key: str, fixture: str) -> None:
        response = client.post(
            f"/api/v1/watchlist-items/{item['id']}/refresh",
            json={"idempotency_key": key, "fixture_key": fixture},
            headers=headers,
        )
        assert response.status_code == 200, response.text

    refresh("cedar-clean-baseline", "clean_project_page")
    refresh("cedar-explicit-false", "qpr_flag_removed")
    signals = client.get(f"/api/v1/projects/{project['id']}/signals", headers=headers).json()
    assert "QPR_FLAG_OBSERVED" not in {signal["rule_code"] for signal in signals}

    refresh("cedar-qpr-observed", "qpr_flag_added")
    refresh("cedar-qpr-cleared", "qpr_flag_removed")
    refresh("cedar-pre-lapsed-status", "cedar_registered_status")
    refresh("cedar-lapsed", "project_marked_lapsed")
    rules = {
        signal["rule_code"]
        for signal in client.get(
            f"/api/v1/projects/{project['id']}/signals", headers=headers
        ).json()
    }
    assert {
        "QPR_FLAG_OBSERVED",
        "QPR_FLAG_CLEARED",
        "PROJECT_MARKED_LAPSED",
        "PROJECT_STATUS_CHANGED",
    } <= rules
