from __future__ import annotations

import io
from datetime import datetime, timedelta

from bs4 import BeautifulSoup
from reportlab.pdfgen import canvas
from sqlalchemy import func, select

from app.core.config import get_settings
from app.models import (
    AuditEvent,
    DeletionTombstone,
    Document,
    MonitoringJob,
    MonitoringRun,
    Organization,
    Project,
    Report,
    Source,
    SourceAccessPolicy,
    Watchlist,
)
from app.services.deletion import organization_deletion_idempotency_key
from app.services.normalization import comparison_text


def _pdf_bytes() -> bytes:
    output = io.BytesIO()
    document = canvas.Canvas(output)
    document.drawString(72, 760, "Project Name: Organization Deletion Fixture")
    document.drawString(72, 740, "Registration Number: HRERA-DELETE-101")
    document.save()
    return output.getvalue()


def test_retention_settings_ui_and_cookie_csrf_are_wired(client) -> None:
    login = client.post(
        "/login",
        data={
            "email": "admin@example.com",
            "password": "TestPassword!2026",
            "next": "/settings/retention",
        },
        follow_redirects=False,
    )
    assert login.status_code == 303

    page = client.get("/settings/retention")
    assert page.status_code == 200
    soup = BeautifulSoup(page.text, "html.parser")
    retention_form = soup.select_one(
        'form[action="/api/v1/organizations/current/retention"][data-api-form]'
    )
    deletion_form = soup.select_one(
        'form[action="/api/v1/organizations/current/deletion"][data-api-form]'
    )
    assert retention_form is not None and deletion_form is not None
    assert retention_form.select_one('[name="retention_upload_days"]')["value"] == "90"
    assert retention_form.select_one('[name="retention_report_days"]')["value"] == "90"
    assert retention_form.select_one('[name="tombstone_mode"]').has_attr("disabled")
    assert not retention_form.select_one('button[type="submit"]').has_attr("disabled")
    csrf = str(deletion_form.select_one('[name="csrf_token"]')["value"])
    assert csrf

    missing_csrf = client.post(
        "/api/v1/organizations/current/deletion",
        json={"confirmation": "Test Advisory", "reason": "CSRF test"},
    )
    assert missing_csrf.status_code == 403
    passed_csrf = client.post(
        "/api/v1/organizations/current/deletion",
        json={"confirmation": "Wrong organization", "reason": "CSRF test"},
        headers={"X-CSRF-Token": csrf},
    )
    assert passed_csrf.status_code == 422

    updated = client.patch(
        "/api/v1/organizations/current/retention",
        json={"retention_upload_days": 45, "retention_report_days": 60},
        headers={"X-CSRF-Token": csrf},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["retention_upload_days"] == 45
    assert updated.json()["retention_report_days"] == 60
    report = client.post(
        "/api/v1/reports",
        json={
            "name": "Organization retention report",
            "formats": ["json", "evidence"],
            "idempotency_key": "organization-retention-report",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert report.status_code == 200, report.text
    created_at = datetime.fromisoformat(report.json()["created_at"])
    expires_at = datetime.fromisoformat(report.json()["expires_at"])
    retention_window = expires_at - created_at
    assert timedelta(days=59, hours=23) < retention_window < timedelta(days=60, hours=1)
    refreshed = BeautifulSoup(client.get("/settings/retention").text, "html.parser")
    assert refreshed.select_one('[name="retention_upload_days"]')["value"] == "45"
    assert refreshed.select_one('[name="retention_report_days"]')["value"] == "60"

    client.post("/logout", follow_redirects=False)
    viewer_login = client.post(
        "/login",
        data={
            "email": "viewer@example.com",
            "password": "TestPassword!2026",
            "next": "/settings/retention",
        },
        follow_redirects=False,
    )
    assert viewer_login.status_code == 303
    viewer_page = BeautifulSoup(client.get("/settings/retention").text, "html.parser")
    assert viewer_page.select_one('[name="retention_upload_days"]').has_attr("disabled")
    assert viewer_page.select_one('[name="confirmation"]').has_attr("disabled")


def test_organization_deletion_is_tenant_safe_persistent_and_idempotent(
    client, auth, db, identity
) -> None:
    analyst = auth("analyst")
    watchlist = client.post(
        "/api/v1/watchlists",
        json={"name": "Deletion fixture", "monitoring_cadence": "weekly"},
        headers=analyst,
    ).json()
    project = client.post(
        "/api/v1/projects",
        json={"registration_number": "HRERA-DELETE-101", "name": "Deletion Project"},
        headers=analyst,
    ).json()
    item = client.post(
        f"/api/v1/watchlists/{watchlist['id']}/items",
        json={"project_id": project["id"], "monitoring_cadence": "weekly"},
        headers=analyst,
    ).json()
    source_response = client.post(
        f"/api/v1/projects/{project['id']}/sources",
        json={
            "source_type": "fixture",
            "name": "Deletion fixture source",
            "fixture_key": "clean_project_page",
        },
        headers=analyst,
    )
    assert source_response.status_code == 201, source_response.text
    source = source_response.json()

    uploaded = client.post(
        f"/api/v1/projects/{project['id']}/documents",
        data={"authorization_attested": "true", "rights_attestation_text": "Authorized"},
        files={"file": ("deletion.pdf", _pdf_bytes(), "application/pdf")},
        headers=analyst,
    )
    assert uploaded.status_code == 201, uploaded.text
    document_id = uploaded.json()["id"]
    report_response = client.post(
        "/api/v1/reports",
        json={
            "name": "Deletion report",
            "project_ids": [project["id"]],
            "formats": ["pdf", "csv", "json", "evidence"],
            "idempotency_key": "organization-deletion-report-101",
        },
        headers=analyst,
    )
    assert report_response.status_code == 200, report_response.text
    report_id = report_response.json()["id"]

    running = MonitoringRun(
        organization_id=identity["organization"].id,
        source_id=source["id"],
        project_id=project["id"],
        triggered_by_user_id=identity["users"]["analyst"].id,
        trigger_type="manual",
        status="running",
        deduplication_key="organization-deletion-running-check",
    )
    other_project = Project(
        organization_id=identity["other_organization"].id,
        name="Other Tenant Survives",
        normalized_name=comparison_text("Other Tenant Survives"),
        registration_number="HRERA-OTHER-DELETE-1",
        identity_status="confirmed",
        is_provisional=False,
    )
    db.add_all([running, other_project])
    db.commit()

    document_row = db.get(Document, document_id)
    upload_path = (
        get_settings().upload_dir
        / identity["organization"].id
        / f"{document_row.storage_key}.pdf"
    )
    report_path = get_settings().report_dir / identity["organization"].id / f"{report_id}.pdf"
    assert upload_path.is_file() and report_path.is_file()
    assert (
        client.get(f"/api/v1/documents/{document_id}/download", headers=analyst).status_code
        == 200
    )
    assert (
        client.get(f"/api/v1/reports/{report_id}/download?format=pdf", headers=analyst).status_code
        == 200
    )

    admin = auth("admin")
    denied_role = client.post(
        "/api/v1/organizations/current/deletion",
        json={"confirmation": "Test Advisory", "reason": "Role check"},
        headers=analyst,
    )
    assert denied_role.status_code == 403
    wrong_confirmation = client.post(
        "/api/v1/organizations/current/deletion",
        json={"confirmation": "test advisory", "reason": "Mismatch check"},
        headers=admin,
    )
    assert wrong_confirmation.status_code == 422

    payload = {"confirmation": "Test Advisory", "reason": "End of local retention test"}
    deleted = client.post(
        "/api/v1/organizations/current/deletion", json=payload, headers=admin
    )
    assert deleted.status_code == 200, deleted.text
    result = deleted.json()
    assert result["deleted"] is True and result["already_deleted"] is False
    assert result["projects_deleted"] == 1
    assert result["watchlists_deleted"] == 1
    assert result["documents_deleted"] == 1
    assert result["reports_deleted"] == 1
    assert result["sources_disabled"] == 1
    assert result["monitoring_jobs_cancelled"] == 1
    assert result["monitoring_runs_cancelled"] == 1
    assert result["artifacts_removed"] == 5

    db.expire_all()
    assert db.get(Organization, identity["organization"].id).is_active is False
    assert db.get(Organization, identity["other_organization"].id).is_active is True
    assert db.get(Project, project["id"]).deleted_at is not None
    assert db.get(Project, other_project.id).deleted_at is None
    assert db.get(Watchlist, watchlist["id"]).deleted_at is not None
    assert db.get(Document, document_id).deleted_at is not None
    assert db.get(Report, report_id).deleted_at is not None
    assert db.get(Source, source["id"]).is_enabled is False
    assert db.scalar(
        select(SourceAccessPolicy).where(SourceAccessPolicy.source_id == source["id"])
    ).is_enabled is False
    assert db.scalar(
        select(MonitoringJob).where(
            MonitoringJob.organization_id == identity["organization"].id,
            MonitoringJob.payload["watchlist_item_id"].as_string() == item["id"],
        )
    ).state == "cancelled"
    assert db.get(MonitoringRun, running.id).status == "cancelled"
    assert not upload_path.exists() and not report_path.exists()
    assert client.get("/api/v1/auth/me", headers=admin).status_code == 403
    assert (
        client.post(
            "/api/v1/auth/login",
            json={"email": "admin@example.com", "password": "TestPassword!2026"},
        ).status_code
        == 403
    )
    assert (
        client.get(f"/api/v1/documents/{document_id}/download", headers=analyst).status_code
        == 403
    )
    assert (
        client.get(f"/api/v1/reports/{report_id}/download?format=pdf", headers=analyst).status_code
        == 403
    )

    repeated = client.post(
        "/api/v1/organizations/current/deletion", json=payload, headers=admin
    )
    assert repeated.status_code == 200, repeated.text
    repeated_result = repeated.json()
    assert repeated_result["already_deleted"] is True
    assert repeated_result["tombstone_id"] == result["tombstone_id"]
    assert repeated_result["artifacts_removed"] == result["artifacts_removed"]
    assert (
        db.scalar(
            select(func.count())
            .select_from(DeletionTombstone)
            .where(
                DeletionTombstone.organization_id == identity["organization"].id,
                DeletionTombstone.resource_type == "organization",
            )
        )
        == 1
    )
    tombstone = db.get(DeletionTombstone, result["tombstone_id"])
    assert tombstone.idempotency_key == organization_deletion_idempotency_key(
        identity["organization"].id
    )
    assert (
        db.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(
                AuditEvent.organization_id == identity["organization"].id,
                AuditEvent.action == "organization.data_deleted",
            )
        )
        == 1
    )
