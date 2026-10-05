from __future__ import annotations

from sqlalchemy import select

from app.models import Alert, Project, SourceConflict, UserCorrection
from app.services.normalization import comparison_text


def _review_records(db, identity):
    organization_id = identity["organization"].id
    project = Project(
        organization_id=organization_id,
        name="UI Evidence Project",
        normalized_name=comparison_text("UI Evidence Project"),
        registration_number="HRERA-UI-101",
        identity_status="confirmed",
        is_provisional=False,
    )
    db.add(project)
    db.flush()
    alert = Alert(
        organization_id=organization_id,
        project_id=project.id,
        title="Completion date changed",
        neutral_description="The captured completion date changed.",
        severity="high",
        required_review_level="reviewer",
        requires_review=True,
        old_value="2027-06-30",
        new_value="2028-03-31",
        magnitude="+275 days",
    )
    conflict = SourceConflict(
        organization_id=organization_id,
        project_id=project.id,
        field_key="building_count",
        source_a_class="official_regulator",
        source_b_class="marketing_document",
        source_a_value="5",
        source_b_value="6",
        is_material=True,
    )
    db.add_all([alert, conflict])
    db.commit()
    return alert, conflict


def test_review_selection_pages_render_real_records(client, db, identity) -> None:
    alert, conflict = _review_records(db, identity)
    login = client.post(
        "/login",
        data={"email": "admin@example.com", "password": "TestPassword!2026"},
        follow_redirects=False,
    )
    assert login.status_code == 303

    alert_page = client.get(f"/alerts?selected={alert.id}")
    assert alert_page.status_code == 200
    assert "UI Evidence Project" in alert_page.text
    assert "Completion date changed" in alert_page.text

    conflict_page = client.get(f"/conflicts?selected={conflict.id}")
    assert conflict_page.status_code == 200
    assert "Building Count" in conflict_page.text
    assert ">5<" in conflict_page.text
    assert ">6<" in conflict_page.text
    assert f'/api/v1/conflicts/{conflict.id}/review' in conflict_page.text


def test_conflict_form_legacy_payload_is_accepted(client, auth, db, identity) -> None:
    _, conflict = _review_records(db, identity)
    response = client.post(
        f"/api/v1/conflicts/{conflict.id}/review",
        json={
            "decision": "prefer_official_display",
            "note": "Official record retained for display precedence.",
        },
        headers=auth("reviewer"),
    )

    assert response.status_code == 200, response.text
    assert response.json()["state"] == "resolved"
    assert response.json()["selected_value"] == "5"


def test_correction_review_and_reversal_preserve_history(client, auth, db, identity) -> None:
    _review_records(db, identity)
    project = db.scalar(select(Project).where(Project.registration_number == "HRERA-UI-101"))
    created = client.post(
        "/api/v1/corrections",
        json={
            "project_id": project.id,
            "field_key": "building_count",
            "corrected_value": "5",
            "reason": "Reviewed supporting official record.",
        },
        headers=auth("analyst"),
    )
    assert created.status_code == 200, created.text

    reviewed = client.post(
        f"/api/v1/corrections/{created.json()['id']}/review",
        json={"decision": "confirm", "note": "Confirmed after evidence review."},
        headers=auth("reviewer"),
    )
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["status"] == "confirmed"

    reversed_response = client.post(
        f"/api/v1/corrections/{created.json()['id']}/reverse",
        json={"note": "Later evidence superseded this correction."},
        headers=auth("reviewer"),
    )
    assert reversed_response.status_code == 200, reversed_response.text
    original = db.get(UserCorrection, created.json()["id"])
    assert original is not None
    db.refresh(original)
    assert original.status == "reversed"
    reversal = db.get(UserCorrection, reversed_response.json()["reversal_id"])
    assert reversal is not None
    assert reversal.reverses_correction_id == original.id
