from __future__ import annotations

import csv
import io
from datetime import UTC, datetime, timedelta

from pypdf import PdfReader
from reportlab.pdfgen import canvas
from sqlalchemy import func, select

from app.core.config import get_settings
from app.models import (
    Alert,
    DocumentExtraction,
    DocumentVersion,
    MonitoringRun,
    SnapshotField,
    Source,
    SourceConflict,
    SourceSnapshot,
)


def _pdf_bytes(
    *, towers: int = 6, units: int = 1_200, completion: str = "31 December 2027"
) -> bytes:
    output = io.BytesIO()
    document = canvas.Canvas(output)
    document.drawString(72, 760, "Project Name: Lakeview Residences")
    document.drawString(72, 740, "Registration Number: HRERA-DEMO-202")
    document.drawString(72, 720, f"Tower Count: {towers}")
    document.drawString(72, 700, f"Unit Count: {units}")
    document.drawString(72, 680, f"Proposed Completion Date: {completion}")
    document.save()
    return output.getvalue()


def _blank_pdf_bytes() -> bytes:
    output = io.BytesIO()
    document = canvas.Canvas(output)
    document.showPage()
    document.save()
    return output.getvalue()


def test_upload_signature_validation_and_private_document_flow(client, auth) -> None:
    headers = auth("analyst")
    project = client.post(
        "/api/v1/projects",
        json={"registration_number": "HRERA-DOC-1", "name": "Document Project"},
        headers=headers,
    ).json()
    invalid = client.post(
        f"/api/v1/projects/{project['id']}/documents",
        data={"authorization_attested": "true", "rights_attestation_text": "Authorized"},
        files={"file": ("fake.pdf", b"not a pdf", "application/pdf")},
        headers=headers,
    )
    assert invalid.status_code == 400
    uploaded = client.post(
        f"/api/v1/projects/{project['id']}/documents",
        data={"authorization_attested": "true", "rights_attestation_text": "Authorized"},
        files={"file": ("brochure.pdf", _pdf_bytes(), "application/pdf")},
        headers=headers,
    )
    assert uploaded.status_code == 201, uploaded.text
    document_id = uploaded.json()["id"]
    extracted = client.post(
        f"/api/v1/documents/{document_id}/extract",
        json={"use_ocr_if_needed": True},
        headers=headers,
    )
    assert extracted.status_code == 200, extracted.text
    assert all(item["document_version_id"] for item in extracted.json())
    downloaded = client.get(f"/api/v1/documents/{document_id}/download", headers=headers)
    assert downloaded.status_code == 200
    deleted = client.delete(f"/api/v1/documents/{document_id}", headers=headers)
    assert deleted.status_code == 200 and deleted.json()["deleted"] is True
    missing_download = client.get(
        f"/api/v1/documents/{document_id}/download", headers=headers
    )
    assert missing_download.status_code == 404


def test_revised_document_versions_are_immutable_tenant_scoped_and_drive_ui(
    client, auth, db
) -> None:
    headers = auth("analyst")
    project = client.post(
        "/api/v1/projects",
        json={"registration_number": "HRERA-DOC-VERSIONS", "name": "Versioned Brochure"},
        headers=headers,
    ).json()
    version_one_pdf = _pdf_bytes()
    uploaded = client.post(
        f"/api/v1/projects/{project['id']}/documents",
        data={"authorization_attested": "true", "rights_attestation_text": "Authorized"},
        files={"file": ("brochure-v1.pdf", version_one_pdf, "application/pdf")},
        headers=headers,
    )
    assert uploaded.status_code == 201, uploaded.text
    document_id = uploaded.json()["id"]

    duplicate = client.post(
        f"/api/v1/documents/{document_id}/versions",
        data={"authorization_attested": "true", "rights_attestation_text": "Authorized"},
        files={"file": ("brochure-copy.pdf", version_one_pdf, "application/pdf")},
        headers=headers,
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "duplicate_document_version"

    revised_pdf = _pdf_bytes(towers=5, units=1_080, completion="30 June 2028")
    revised = client.post(
        f"/api/v1/documents/{document_id}/versions",
        data={
            "authorization_attested": "true",
            "rights_attestation_text": "Authorized revised brochure",
        },
        files={"file": ("brochure-v2.pdf", revised_pdf, "application/pdf")},
        headers=headers,
    )
    assert revised.status_code == 201, revised.text
    assert revised.json()["version_number"] == 2
    versions = client.get(
        f"/api/v1/documents/{document_id}/versions", headers=headers
    ).json()
    assert [item["version_number"] for item in versions] == [2, 1]
    assert versions[0]["content_hash"] != versions[1]["content_hash"]
    for version in versions:
        download = client.get(
            f"/api/v1/documents/{document_id}/versions/{version['id']}/download",
            headers=headers,
        )
        assert download.status_code == 200

    other_login = client.post(
        "/api/v1/auth/login",
        json={"email": "other@example.com", "password": "TestPassword!2026"},
    )
    other_headers = {"Authorization": f"Bearer {other_login.json()['token']}"}
    denied = client.post(
        f"/api/v1/documents/{document_id}/versions",
        data={"authorization_attested": "true"},
        files={"file": ("cross-tenant.pdf", _pdf_bytes(towers=7), "application/pdf")},
        headers=other_headers,
    )
    assert denied.status_code == 404

    headers = auth("analyst")
    extracted = client.post(
        f"/api/v1/documents/{document_id}/extract",
        json={"use_ocr_if_needed": True},
        headers=headers,
    )
    assert extracted.status_code == 200, extracted.text
    assert extracted.json()
    assert {item["document_version_id"] for item in extracted.json()} == {
        revised.json()["id"]
    }
    assert any(item["raw_value"] == "1080" for item in extracted.json())

    page = client.get(
        f"/projects/{project['id']}/documents?document_id={document_id}"
    )
    assert page.status_code == 200
    assert "Create immutable revision" in page.text
    assert "Version history" in page.text
    assert "v2" in page.text
    assert "1080" in page.text

    storage_keys = list(
        db.scalars(
            select(DocumentVersion.storage_key).where(
                DocumentVersion.document_id == document_id
            )
        )
    )
    deleted = client.delete(f"/api/v1/documents/{document_id}", headers=headers)
    assert deleted.status_code == 200 and deleted.json()["deleted"] is True
    upload_root = get_settings().upload_dir / str(project["organization_id"])
    assert all(not (upload_root / f"{key}.pdf").exists() for key in storage_keys)


def test_unreadable_text_routes_to_manual_review_when_ocr_is_disabled(
    client, auth, db
) -> None:
    headers = auth("analyst")
    project = client.post(
        "/api/v1/projects",
        json={"registration_number": "HRERA-OCR-MANUAL", "name": "Manual OCR Review"},
        headers=headers,
    ).json()
    uploaded = client.post(
        f"/api/v1/projects/{project['id']}/documents",
        data={"authorization_attested": "true"},
        files={"file": ("scanned.pdf", _blank_pdf_bytes(), "application/pdf")},
        headers=headers,
    )
    assert uploaded.status_code == 201, uploaded.text
    document_id = uploaded.json()["id"]
    extracted = client.post(
        f"/api/v1/documents/{document_id}/extract",
        json={"use_ocr_if_needed": True},
        headers=headers,
    )
    assert extracted.status_code == 200, extracted.text
    assert extracted.json() == []
    assert client.get(f"/api/v1/documents/{document_id}", headers=headers).json()[
        "status"
    ] == "needs_review"
    extraction = db.scalar(
        select(DocumentExtraction).where(
            DocumentExtraction.organization_id == project["organization_id"]
        )
    )
    assert extraction is not None
    assert extraction.status == "needs_review"
    assert extraction.requires_review is True
    assert any("OCR" in warning for warning in extraction.warnings)
    repeated = client.post(
        f"/api/v1/documents/{document_id}/extract",
        json={"use_ocr_if_needed": True},
        headers=headers,
    )
    assert repeated.status_code == 200 and repeated.json() == []
    assert (
        db.scalar(select(func.count()).select_from(DocumentExtraction)) == 1
    )
    comparison = client.post(
        f"/api/v1/documents/{document_id}/compare", json={}, headers=headers
    )
    assert comparison.status_code == 409
    page = client.get(
        f"/projects/{project['id']}/documents?document_id={document_id}"
    )
    assert page.status_code == 200
    assert "Human review required" in page.text
    assert "OCR is not configured" in page.text


def test_brochure_conflicts_preserve_official_facts_and_page_evidence(
    client, auth, db
) -> None:
    headers = auth("analyst")
    watchlist = client.post(
        "/api/v1/watchlists",
        json={"name": "Brochure comparison", "monitoring_cadence": "weekly"},
        headers=headers,
    ).json()
    project = client.post(
        "/api/v1/projects",
        json={"registration_number": "HRERA-DEMO-202", "name": "Lakeview Residences"},
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
            "name": "Lakeview official fixture",
            "fixture_key": "case_b_official_record",
        },
        headers=headers,
    )
    assert source.status_code == 201, source.text
    baseline = client.post(
        f"/api/v1/watchlist-items/{item['id']}/refresh",
        json={
            "idempotency_key": "case-b-official-baseline",
            "fixture_key": "case_b_official_record",
        },
        headers=headers,
    )
    assert baseline.status_code == 200, baseline.text
    official_before = baseline.json()["snapshot_id"]

    # A newer marketing snapshot must never replace the official comparison baseline.
    developer_source = Source(
        organization_id=project["organization_id"],
        name="Lakeview developer page",
        source_type="developer_page",
        source_class="official_developer",
        base_url="https://developer.example/lakeview",
        domain="developer.example",
        is_enabled=True,
    )
    db.add(developer_source)
    db.flush()
    developer_run = MonitoringRun(
        organization_id=project["organization_id"],
        source_id=developer_source.id,
        project_id=project["id"],
        trigger_type="manual",
        status="succeeded",
        deduplication_key="newer-developer-comparison-run",
        started_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
        fetched_count=1,
        snapshot_count=1,
        access_completeness="complete",
    )
    db.add(developer_run)
    db.flush()
    developer_snapshot = SourceSnapshot(
        organization_id=project["organization_id"],
        source_id=developer_source.id,
        project_id=project["id"],
        monitoring_run_id=developer_run.id,
        entity_id=project["id"],
        fetch_status="success",
        requested_url=developer_source.base_url,
        final_url=developer_source.base_url,
        http_status=200,
        fetched_at=datetime.now(UTC) + timedelta(minutes=1),
        content_type="text/html",
        content_hash="developer-newer-hash",
        parser_name="developer_fixture",
        parser_version="1.0.0",
        access_completeness="complete",
        structured_facts={"unit_count": "9999"},
        validation_status="valid",
        deduplication_key="newer-developer-comparison-snapshot",
    )
    db.add(developer_snapshot)
    db.flush()
    db.add_all(
        [
            SnapshotField(
                organization_id=project["organization_id"],
                snapshot_id=developer_snapshot.id,
                field_key="unit_count",
                raw_value="9999",
                canonical_value="9999",
                extraction_method="html",
                parser_name="developer_fixture",
                parser_version="1.0.0",
            ),
            SnapshotField(
                organization_id=project["organization_id"],
                snapshot_id=developer_snapshot.id,
                field_key="building_count",
                raw_value="99",
                canonical_value="99",
                extraction_method="html",
                parser_name="developer_fixture",
                parser_version="1.0.0",
            ),
            SnapshotField(
                organization_id=project["organization_id"],
                snapshot_id=developer_snapshot.id,
                field_key="proposed_completion_date",
                raw_value="31 December 2099",
                canonical_value="2099-12-31",
                extraction_method="html",
                parser_name="developer_fixture",
                parser_version="1.0.0",
            ),
        ]
    )
    db.commit()

    uploaded = client.post(
        f"/api/v1/projects/{project['id']}/documents",
        data={"authorization_attested": "true"},
        files={"file": ("lakeview-brochure.pdf", _pdf_bytes(), "application/pdf")},
        headers=headers,
    )
    assert uploaded.status_code == 201, uploaded.text
    document_id = uploaded.json()["id"]
    extracted = client.post(
        f"/api/v1/documents/{document_id}/extract",
        json={"use_ocr_if_needed": True},
        headers=headers,
    )
    assert extracted.status_code == 200, extracted.text
    compared = client.post(
        f"/api/v1/documents/{document_id}/compare", json={}, headers=headers
    )
    assert compared.status_code == 200, compared.text
    conflicts = {
        item["field_key"]: item
        for item in compared.json()["results"]
        if item["state"] == "CONFLICTING"
    }
    assert {"building_count", "unit_count", "proposed_completion_date"}.issubset(
        conflicts
    )
    assert all(item["page"] == 1 and item["evidence_text"] for item in conflicts.values())
    preserved = list(
        db.scalars(
            select(SourceConflict).where(
                SourceConflict.organization_id == project["organization_id"],
                SourceConflict.project_id == project["id"],
            )
        )
    )
    assert len(preserved) == 3
    assert all(item.source_a_value and item.source_b_value for item in preserved)
    assert all(item.source_a_reference["snapshot_id"] == official_before for item in preserved)
    rendered = client.get(
        f"/projects/{project['id']}/documents?document_id={document_id}"
    )
    assert rendered.status_code == 200
    assert "9999" not in rendered.text


def test_report_approval_blocks_every_unresolved_required_review_alert(
    client, auth, db
) -> None:
    analyst = auth("analyst")
    watchlist = client.post(
        "/api/v1/watchlists",
        json={"name": "Report monitoring", "monitoring_cadence": "weekly"},
        headers=analyst,
    ).json()
    project = client.post(
        "/api/v1/projects",
        json={"registration_number": "HRERA-DEMO-101", "name": "Report Project"},
        headers=analyst,
    ).json()
    item = client.post(
        f"/api/v1/watchlists/{watchlist['id']}/items",
        json={"project_id": project["id"], "monitoring_cadence": "weekly"},
        headers=analyst,
    ).json()
    source = client.post(
        f"/api/v1/projects/{project['id']}/sources",
        json={
            "source_type": "fixture",
            "name": "Report official fixture",
            "fixture_key": "case_a_completion_old",
        },
        headers=analyst,
    )
    assert source.status_code == 201, source.text
    baseline = client.post(
        f"/api/v1/watchlist-items/{item['id']}/refresh",
        json={
            "idempotency_key": "report-baseline-run",
            "fixture_key": "case_a_completion_old",
        },
        headers=analyst,
    )
    changed = client.post(
        f"/api/v1/watchlist-items/{item['id']}/refresh",
        json={
            "idempotency_key": "report-change-run",
            "fixture_key": "case_a_completion_extended",
        },
        headers=analyst,
    )
    assert baseline.status_code == 200, baseline.text
    assert changed.status_code == 200, changed.text
    alerts = client.get("/api/v1/alerts", headers=analyst).json()
    alert = next(
        item
        for item in alerts
        if item["project_id"] == project["id"]
        and item["requires_review"]
        and item["detected_change_id"]
        and item["severity"].lower() in {"high", "critical"}
    )
    medium_review_alert = Alert(
        organization_id=project["organization_id"],
        project_id=project["id"],
        source_id=source.json()["id"],
        detected_change_id=alert["detected_change_id"],
        title="Medium review policy fixture",
        neutral_description="This medium-severity item is explicitly routed to review.",
        state="open",
        severity="medium",
        required_review_level="reviewer",
        requires_review=True,
        source_class="official_regulator",
        limitation="Review is required by policy.",
        deduplication_key="medium-required-review-report-fixture",
    )
    db.add(medium_review_alert)
    db.commit()

    created = client.post(
        "/api/v1/reports",
        json={
            "name": "Weekly evidence report",
            "project_ids": [project["id"]],
            "formats": ["pdf", "csv", "json", "evidence"],
            "idempotency_key": "report-create-key-101",
        },
        headers=analyst,
    )
    assert created.status_code == 200, created.text
    report_id = created.json()["id"]
    viewer = auth("viewer")
    viewer_draft = client.get(
        f"/api/v1/reports/{report_id}/download?format=pdf", headers=viewer
    )
    assert viewer_draft.status_code == 403
    assert viewer_draft.json()["error"]["code"] == "draft_report_restricted"
    for format_name in ("pdf", "csv", "json", "evidence"):
        response = client.get(
            f"/api/v1/reports/{report_id}/download?format={format_name}", headers=analyst
        )
        assert response.status_code == 200, (format_name, response.text)
        if format_name == "pdf":
            draft_pdf_text = "\n".join(
                page.extract_text() or ""
                for page in PdfReader(io.BytesIO(response.content)).pages
            )
            assert "DRAFT" in draft_pdf_text
            assert "Evidence references" in draft_pdf_text
            assert "fixture://" in draft_pdf_text
            assert "Content hash" in draft_pdf_text
    draft_manifest = client.get(
        f"/api/v1/reports/{report_id}/download?format=evidence", headers=analyst
    )
    draft_rows = list(
        csv.DictReader(io.StringIO(draft_manifest.content.decode("utf-8-sig")))
    )
    draft_row = next(row for row in draft_rows if row["alert_id"] == alert["id"])
    assert {
        "source_reference",
        "capture_timestamp",
        "content_hash",
        "parser_version",
        "reviewer",
        "review_decision",
    }.issubset(draft_row)
    assert draft_row["source_reference"].startswith("fixture://")
    assert draft_row["content_hash"]
    assert draft_row["parser_version"]

    reviewer = auth("reviewer")
    blocked = client.post(
        f"/api/v1/reports/{report_id}/approve", json={}, headers=reviewer
    )
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["error"]["code"] == "unresolved_required_review"
    blocking_alert_ids = blocked.json()["error"]["alert_ids"]
    assert alert["id"] in blocking_alert_ids
    assert medium_review_alert.id in blocking_alert_ids

    for alert_id in blocking_alert_ids:
        reviewed = client.post(
            f"/api/v1/alerts/{alert_id}/review",
            json={
                "decision": "confirm",
                "reason_code": "evidence_supported",
                "note": "Reviewed both captured fixture snapshots.",
            },
            headers=reviewer,
        )
        assert reviewed.status_code == 200, reviewed.text
    approved = client.post(
        f"/api/v1/reports/{report_id}/approve", json={}, headers=reviewer
    )
    assert approved.status_code == 200
    assert approved.json()["state"] == "approved"

    viewer_approved = client.get(
        f"/api/v1/reports/{report_id}/download?format=pdf", headers=viewer
    )
    assert viewer_approved.status_code == 200
    approved_pdf_text = "\n".join(
        page.extract_text() or ""
        for page in PdfReader(io.BytesIO(viewer_approved.content)).pages
    )
    assert "APPROVED" in approved_pdf_text
    assert "DRAFT" not in approved_pdf_text

    final_manifest = client.get(
        f"/api/v1/reports/{report_id}/download?format=evidence", headers=reviewer
    )
    final_rows = list(
        csv.DictReader(io.StringIO(final_manifest.content.decode("utf-8-sig")))
    )
    final_row = next(row for row in final_rows if row["alert_id"] == alert["id"])
    assert final_row["reviewer"] == "reviewer@example.com"
    assert final_row["review_decision"] == "confirm"


def test_report_approval_fails_closed_when_evidence_provenance_is_missing(
    client, auth, db
) -> None:
    analyst = auth("analyst")
    project = client.post(
        "/api/v1/projects",
        json={"registration_number": "HRERA-REPORT-NO-EVIDENCE", "name": "No Evidence"},
        headers=analyst,
    ).json()
    alert = Alert(
        organization_id=project["organization_id"],
        project_id=project["id"],
        title="Evidence provenance unavailable",
        neutral_description="No captured source snapshot exists for this draft item.",
        state="open",
        severity="low",
        required_review_level="none",
        requires_review=False,
        limitation="No source evidence has been captured.",
        deduplication_key="missing-evidence-report-fixture",
    )
    db.add(alert)
    db.commit()
    created = client.post(
        "/api/v1/reports",
        json={
            "name": "Incomplete evidence draft",
            "project_ids": [project["id"]],
            "formats": ["pdf", "evidence"],
            "idempotency_key": "missing-evidence-report-draft",
        },
        headers=analyst,
    )
    assert created.status_code == 200, created.text
    blocked = client.post(
        f"/api/v1/reports/{created.json()['id']}/approve",
        json={},
        headers=auth("reviewer"),
    )
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "incomplete_evidence_manifest"
    assert alert.id in blocked.json()["error"]["missing_fields"]
