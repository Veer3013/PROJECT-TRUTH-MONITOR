from __future__ import annotations

import io
from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter
from sqlalchemy import select

from app.models import Document, Project, SourceConflict
from app.seed_demo import seed_demo

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures"


@pytest.fixture
def seeded_browser_session(client, db):
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


def test_case_c_phase_relationship_and_timeline_render_end_to_end(
    seeded_browser_session,
) -> None:
    client, db = seeded_browser_session
    phase_three = db.scalar(
        select(Project).where(Project.registration_number == "HRERA-DEMO-303")
    )
    assert phase_three is not None

    phase_map = client.get(f"/projects/{phase_three.id}/phase-map")
    assert phase_map.status_code == 200
    assert "Orion Greens" in phase_map.text
    assert "HRERA-DEMO-303" in phase_map.text

    timeline = client.get(f"/projects/{phase_three.id}/timeline")
    assert timeline.status_code == 200
    assert "New phase" in timeline.text
    assert "Phase III" in timeline.text


def test_case_d_preserves_three_source_values_and_low_confidence_ocr(
    seeded_browser_session,
) -> None:
    client, db = seeded_browser_session
    project = db.scalar(
        select(Project).where(Project.registration_number == "HRERA-DEMO-404")
    )
    assert project is not None
    document = db.scalar(
        select(Document).where(
            Document.project_id == project.id,
            Document.organization_id == project.organization_id,
        )
    )
    assert document is not None
    conflict = db.scalar(
        select(SourceConflict).where(
            SourceConflict.project_id == project.id,
            SourceConflict.source_a_value.is_not(None),
            SourceConflict.source_b_value.is_not(None),
        )
    )
    assert conflict is not None

    conflicts = client.get(
        f"/conflicts?project_id={project.id}&selected={conflict.id}"
    )
    assert conflicts.status_code == 200
    assert "2029-06-30" in conflicts.text
    assert "2029-12-31" in conflicts.text
    assert "Official Developer" in conflicts.text
    assert "Marketing Brochure" in conflicts.text

    documents = client.get(
        f"/projects/{project.id}/documents?document_id={document.id}"
    )
    assert documents.status_code == 200
    assert "Human review required" in documents.text
    assert "43%" in documents.text
    assert "2029-12-31" in documents.text


def test_text_pdf_fixture_upload_and_extraction_uses_real_file(
    client, auth
) -> None:
    headers = auth("analyst")
    project = client.post(
        "/api/v1/projects",
        json={
            "registration_number": "HRERA-PDF-001",
            "name": "Fixture Brochure Project",
        },
        headers=headers,
    ).json()
    pdf_bytes = (FIXTURE_ROOT / "pdf" / "brochure_text_fixture.pdf").read_bytes()
    uploaded = client.post(
        f"/api/v1/projects/{project['id']}/documents",
        data={"authorization_attested": "true"},
        files={"file": ("brochure_text_fixture.pdf", pdf_bytes, "application/pdf")},
        headers=headers,
    )
    assert uploaded.status_code == 201, uploaded.text
    extracted = client.post(
        f"/api/v1/documents/{uploaded.json()['id']}/extract",
        json={"use_ocr_if_needed": False},
        headers=headers,
    )
    assert extracted.status_code == 200, extracted.text
    claims = {item["field_key"]: item for item in extracted.json()}
    assert claims["registration_number"]["normalized_value"] == "HRERA-PDF-001"
    assert claims["proposed_completion_date"]["page_number"] == 1


def test_encrypted_real_pdf_is_rejected_and_not_persisted(client, auth) -> None:
    headers = auth("analyst")
    project = client.post(
        "/api/v1/projects",
        json={"registration_number": "HRERA-PDF-LOCKED", "name": "Locked PDF"},
        headers=headers,
    ).json()
    reader = PdfReader(FIXTURE_ROOT / "pdf" / "brochure_text_fixture.pdf")
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    writer.encrypt("fixture-password")
    encrypted = io.BytesIO()
    writer.write(encrypted)

    rejected = client.post(
        f"/api/v1/projects/{project['id']}/documents",
        data={"authorization_attested": "true"},
        files={"file": ("locked.pdf", encrypted.getvalue(), "application/pdf")},
        headers=headers,
    )
    assert rejected.status_code == 400
    assert rejected.json()["error"]["code"] == "encrypted_pdf"
