from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import DetectedChange, Project, SourceSnapshot
from app.seed_demo import seed_demo


@pytest.fixture
def seeded_trust_ui(client, db):
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
    project = db.scalar(select(Project).where(Project.registration_number == "HRERA-DEMO-101"))
    assert project is not None
    change = db.scalar(
        select(DetectedChange).where(
            DetectedChange.project_id == project.id,
            DetectedChange.change_type == "date_extended",
        )
    )
    assert change is not None
    new_snapshot = db.scalar(
        select(SourceSnapshot).where(SourceSnapshot.id == change.new_snapshot_id)
    )
    assert new_snapshot is not None
    return client, project, change, new_snapshot


def test_seeded_change_ledger_and_evidence_card_render_provenance(seeded_trust_ui) -> None:
    client, project, change, new_snapshot = seeded_trust_ui

    ledger = client.get(f"/projects/{project.id}/changes")
    assert ledger.status_code == 200
    assert "30 June 2027" in ledger.text
    assert "31 March 2028" in ledger.text
    assert "+275 days" in ledger.text
    assert change.old_snapshot_id in ledger.text
    assert change.new_snapshot_id in ledger.text
    assert "ProjectTruth frozen HRERA demo fixtures" in ledger.text
    assert "Not present" not in ledger.text
    assert "Source not recorded" not in ledger.text

    evidence = client.get(f"/evidence/{change.id}")
    assert evidence.status_code == 200
    assert "Proposed Completion Date: 30 June 2027" in evidence.text
    assert "Revised Completion Date: 31 March 2028" in evidence.text
    assert "+275 days" in evidence.text
    assert change.old_snapshot_id in evidence.text
    assert change.new_snapshot_id in evidence.text
    assert new_snapshot.content_hash in evidence.text
    assert "fixture_html" in evidence.text
    assert "1.0.0" in evidence.text
    assert "deterministic_diff" in evidence.text
    assert "Not recorded" not in evidence.text


def test_seeded_timeline_and_passport_render_composed_project_truth(seeded_trust_ui) -> None:
    client, project, _, _ = seeded_trust_ui

    timeline = client.get(f"/projects/{project.id}/timeline")
    assert timeline.status_code == 200
    assert "Project tracking started" in timeline.text
    assert "Baseline captured" in timeline.text
    assert "Completion date changed" in timeline.text
    assert "30 June 2027" in timeline.text
    assert "31 March 2028" in timeline.text
    assert "+275 days" in timeline.text
    assert "fixture_html 1.0.0" in timeline.text
    assert "Time not recorded" not in timeline.text
    assert "Monitoring event" not in timeline.text

    passport = client.get(f"/projects/{project.id}/passport")
    assert passport.status_code == 200
    assert "HRERA-DEMO-101" in passport.text
    assert "31 March 2028" in passport.text
    assert "ProjectTruth frozen HRERA demo fixtures" in passport.text
    assert "0 marketing / user sources" in passport.text
    assert "2 February 2026, 09:00 UTC" in passport.text
    assert "Needs review" in passport.text
    assert "Not yet verified" not in passport.text
    assert "Official source not captured" not in passport.text
