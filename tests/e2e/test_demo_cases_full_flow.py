from __future__ import annotations

import io

from reportlab.pdfgen import canvas

import app.api.v1.documents as documents_api
from app.parsers.ocr import OCRPage, OCRResult


def _project_with_watchlist(
    client,
    headers: dict[str, str],
    *,
    registration_number: str | None,
    name: str,
    parent_project_id: str | None = None,
    phase_name: str | None = None,
) -> tuple[dict[str, object], dict[str, object]]:
    watchlist = client.post(
        "/api/v1/watchlists",
        json={"name": f"{name} workflow", "monitoring_cadence": "weekly"},
        headers=headers,
    ).json()
    project = client.post(
        "/api/v1/projects",
        json={
            "registration_number": registration_number,
            "name": name,
            "parent_project_id": parent_project_id,
            "phase_name": phase_name,
        },
        headers=headers,
    ).json()
    item = client.post(
        f"/api/v1/watchlists/{watchlist['id']}/items",
        json={"project_id": project["id"], "monitoring_cadence": "weekly"},
        headers=headers,
    ).json()
    return project, item


def _add_fixture_source(
    client,
    headers: dict[str, str],
    *,
    project_id: str,
    name: str,
    fixture_key: str,
) -> dict[str, object]:
    response = client.post(
        f"/api/v1/projects/{project_id}/sources",
        json={"source_type": "fixture", "name": name, "fixture_key": fixture_key},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


def _refresh(client, headers, item_id: str, key: str, fixture: str):
    response = client.post(
        f"/api/v1/watchlist-items/{item_id}/refresh",
        json={"idempotency_key": key, "fixture_key": fixture},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["monitoring_run"]["status"] == "succeeded"
    return response.json()


def _brochure_pdf() -> bytes:
    output = io.BytesIO()
    pdf = canvas.Canvas(output)
    pdf.drawString(72, 760, "Project Name: Lakeview Residences")
    pdf.drawString(72, 740, "Registration Number: HRERA-DEMO-202")
    pdf.drawString(72, 720, "Tower Count: 6")
    pdf.drawString(72, 700, "Unit Count: 1200")
    pdf.drawString(72, 680, "Proposed Completion Date: 31 December 2027")
    pdf.save()
    return output.getvalue()


def _blank_pdf() -> bytes:
    output = io.BytesIO()
    pdf = canvas.Canvas(output)
    pdf.showPage()
    pdf.save()
    return output.getvalue()


class LowConfidenceCaseDOCR:
    provider_name = "synthetic_case_d_ocr"
    provider_version = "1.0"

    async def extract(self, pdf_content: bytes) -> OCRResult:
        return OCRResult(
            provider_name=self.provider_name,
            provider_version=self.provider_version,
            pages=(
                OCRPage(
                    page=3,
                    text="Proposed Completion Date: 31 December 2029",
                    confidence=0.43,
                ),
            ),
            available=True,
            warnings=("low_image_quality", "low_confidence_core_field"),
        )


def test_case_a_capture_diff_review_flow(client, auth) -> None:
    analyst = auth("analyst")
    project, item = _project_with_watchlist(
        client,
        analyst,
        registration_number="HRERA-DEMO-101",
        name="Aster Heights Phase II",
    )
    _add_fixture_source(
        client,
        analyst,
        project_id=str(project["id"]),
        name="Case A official source",
        fixture_key="case_a_completion_old",
    )
    first = _refresh(
        client, analyst, str(item["id"]), "case-a-e2e-baseline", "case_a_completion_old"
    )
    second = _refresh(
        client,
        analyst,
        str(item["id"]),
        "case-a-e2e-extension",
        "case_a_completion_extended",
    )
    assert first["changes_created"] == 0
    assert second["changes_created"] >= 1
    changes = client.get(
        f"/api/v1/projects/{project['id']}/changes", headers=analyst
    ).json()
    completion = next(item for item in changes if item["change_type"] == "date_extended")
    assert completion["magnitude"] == 275.0
    alerts = client.get("/api/v1/alerts", headers=analyst).json()
    alert = next(item for item in alerts if item["detected_change_id"] == completion["id"])
    reviewed = client.post(
        f"/api/v1/alerts/{alert['id']}/review",
        json={
            "decision": "confirm",
            "reason_code": "evidence_supported",
            "note": "Compared both captured snapshots.",
        },
        headers=auth("reviewer"),
    )
    assert reviewed.status_code == 200
    assert reviewed.json()["state"] == "confirmed"


def test_case_b_capture_document_compare_and_conflict_review_flow(client, auth) -> None:
    analyst = auth("analyst")
    project, item = _project_with_watchlist(
        client,
        analyst,
        registration_number="HRERA-DEMO-202",
        name="Lakeview Residences",
    )
    _add_fixture_source(
        client,
        analyst,
        project_id=str(project["id"]),
        name="Case B official source",
        fixture_key="case_b_official_record",
    )
    _refresh(
        client, analyst, str(item["id"]), "case-b-e2e-baseline", "case_b_official_record"
    )
    uploaded = client.post(
        f"/api/v1/projects/{project['id']}/documents",
        data={"authorization_attested": "true"},
        files={"file": ("case-b.pdf", _brochure_pdf(), "application/pdf")},
        headers=analyst,
    )
    assert uploaded.status_code == 201, uploaded.text
    document_id = uploaded.json()["id"]
    extracted = client.post(
        f"/api/v1/documents/{document_id}/extract",
        json={"use_ocr_if_needed": False},
        headers=analyst,
    )
    assert extracted.status_code == 200
    compared = client.post(
        f"/api/v1/documents/{document_id}/compare", json={}, headers=analyst
    )
    assert compared.status_code == 200
    conflict_fields = {
        item["field_key"]
        for item in compared.json()["results"]
        if item["state"] == "CONFLICTING"
    }
    assert {"building_count", "unit_count", "proposed_completion_date"}.issubset(
        conflict_fields
    )
    conflict = next(
        item
        for item in client.get("/api/v1/conflicts", headers=analyst).json()
        if item["project_id"] == project["id"]
    )
    resolved = client.post(
        f"/api/v1/conflicts/{conflict['id']}/review",
        json={
            "decision": "prefer_official_display",
            "reason": "Official captured fact controls display; both values remain preserved.",
        },
        headers=auth("reviewer"),
    )
    assert resolved.status_code == 200
    assert resolved.json()["state"] == "resolved"
    assert resolved.json()["historical_values_preserved"] is True


def test_case_c_capture_new_phase_signal_relationship_and_review_flow(client, auth) -> None:
    analyst = auth("analyst")
    parent = client.post(
        "/api/v1/projects",
        json={"name": "Orion Greens", "locality_or_sector": "Sector 79"},
        headers=analyst,
    ).json()
    project, item = _project_with_watchlist(
        client,
        analyst,
        registration_number="HRERA-DEMO-303",
        name="Orion Greens Phase III",
        parent_project_id=str(parent["id"]),
        phase_name="Phase III",
    )
    _add_fixture_source(
        client,
        analyst,
        project_id=str(project["id"]),
        name="Case C official source",
        fixture_key="case_c_phase_baseline",
    )
    _refresh(
        client, analyst, str(item["id"]), "case-c-e2e-baseline", "case_c_phase_baseline"
    )
    changed = _refresh(
        client, analyst, str(item["id"]), "case-c-e2e-new-phase", "case_c_new_phase"
    )
    assert changed["changes_created"] >= 1
    signals = client.get(
        f"/api/v1/projects/{project['id']}/signals", headers=analyst
    ).json()
    assert "NEW_PHASE_OR_REGISTRATION" in {item["rule_code"] for item in signals}
    alert = next(
        item
        for item in client.get("/api/v1/alerts", headers=analyst).json()
        if item["project_id"] == project["id"] and item["requires_review"]
    )
    reviewed = client.post(
        f"/api/v1/alerts/{alert['id']}/review",
        json={
            "decision": "confirm",
            "reason_code": "evidence_supported",
            "note": "New phase registration and parent relationship reviewed.",
        },
        headers=auth("reviewer"),
    )
    assert reviewed.status_code == 200
    phase_map = client.get(f"/projects/{project['id']}/phase-map")
    timeline = client.get(f"/projects/{project['id']}/timeline?event_type=signal")
    assert phase_map.status_code == 200 and "Orion Greens" in phase_map.text
    assert timeline.status_code == 200 and "New phase" in timeline.text


def test_case_d_capture_low_confidence_ocr_needs_review_without_official_overwrite(
    client, auth, monkeypatch
) -> None:
    analyst = auth("analyst")
    project, item = _project_with_watchlist(
        client,
        analyst,
        registration_number="HRERA-DEMO-404",
        name="Metro Square",
    )
    _add_fixture_source(
        client,
        analyst,
        project_id=str(project["id"]),
        name="Case D official source",
        fixture_key="case_d_official_missing_completion",
    )
    _refresh(
        client,
        analyst,
        str(item["id"]),
        "case-d-e2e-official",
        "case_d_official_missing_completion",
    )
    monkeypatch.setattr(documents_api, "DisabledOCRProvider", LowConfidenceCaseDOCR)
    uploaded = client.post(
        f"/api/v1/projects/{project['id']}/documents",
        data={"authorization_attested": "true"},
        files={"file": ("case-d-scan.pdf", _blank_pdf(), "application/pdf")},
        headers=analyst,
    )
    assert uploaded.status_code == 201, uploaded.text
    document_id = uploaded.json()["id"]
    extracted = client.post(
        f"/api/v1/documents/{document_id}/extract",
        json={"use_ocr_if_needed": True},
        headers=analyst,
    )
    assert extracted.status_code == 200, extracted.text
    claim = next(
        item
        for item in extracted.json()
        if item["field_key"] == "proposed_completion_date"
    )
    assert claim["extraction_confidence"] == 0.43
    document_state = client.get(
        f"/api/v1/documents/{document_id}", headers=analyst
    ).json()
    assert document_state["status"] == "needs_review"
    compared = client.post(
        f"/api/v1/documents/{document_id}/compare", json={}, headers=analyst
    )
    assert compared.status_code == 200
    completion = next(
        item
        for item in compared.json()["results"]
        if item["field_key"] == "proposed_completion_date"
    )
    assert completion["state"] == "NEEDS_REVIEW"
    refreshed_project = client.get(
        f"/api/v1/projects/{project['id']}", headers=analyst
    ).json()
    assert refreshed_project["proposed_completion_date"] is None
    project_alerts = [
        item
        for item in client.get("/api/v1/alerts", headers=analyst).json()
        if item["project_id"] == project["id"]
    ]
    assert not any(item["state"] == "confirmed" for item in project_alerts)
