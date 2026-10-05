from __future__ import annotations

import hashlib
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import FileResponse
from sqlalchemy import distinct, func, select

from app.api.deps import AnalystActor, CSRFProtected, CurrentActor, DBSession, ReviewerActor
from app.api.utils import add_audit_event, commit_or_rollback
from app.core.config import get_settings
from app.core.permissions import Role, role_allows
from app.models import (
    Alert,
    AlertReview,
    AuditEvent,
    ChangeEvidence,
    DeletionTombstone,
    DetectedChange,
    DocumentExtraction,
    DocumentVersion,
    Organization,
    Project,
    Report,
    ReportItem,
    Signal,
    Source,
    SourceConflict,
    SourceSnapshot,
    User,
)
from app.schemas.common import AuditResponse, DeleteResponse
from app.schemas.reports import (
    MarketSummary,
    ReportApprove,
    ReportCreate,
    ReportResponse,
)
from app.services.reports import (
    PRODUCT_DISCLAIMER,
    ReportApprovalError,
    ReportEvidenceError,
    build_evidence_manifest,
    export_csv,
    export_json,
    export_pdf_report,
    validate_client_report,
    validate_evidence_manifest,
    weekly_digest_metrics,
)

router = APIRouter(tags=["Reports, market and audit"])
ALLOWED_FORMATS = {"pdf", "csv", "json", "evidence"}


def get_tenant_report(db: DBSession, organization_id: str, report_id: str) -> Report:
    report = db.scalar(
        select(Report).where(
            Report.id == report_id,
            Report.organization_id == organization_id,
            Report.deleted_at.is_(None),
        )
    )
    if report is None:
        raise HTTPException(status_code=404, detail={"code": "report_not_found"})
    return report


def _report_root(organization_id: str) -> Path:
    root = get_settings().report_dir.resolve()
    org_root = (root / organization_id).resolve()
    if root not in org_root.parents:
        raise RuntimeError("Report storage is not configured safely")
    org_root.mkdir(parents=True, exist_ok=True)
    return org_root


def _artifact_path(organization_id: str, report_id: str, format_name: str) -> Path:
    if format_name not in ALLOWED_FORMATS:
        raise HTTPException(status_code=422, detail={"code": "unsupported_report_format"})
    root = _report_root(organization_id)
    suffix = "csv" if format_name == "evidence" else format_name
    name = f"{report_id}-evidence.csv" if format_name == "evidence" else f"{report_id}.{suffix}"
    path = (root / name).resolve()
    if root not in path.parents:
        raise HTTPException(status_code=404, detail={"code": "artifact_not_found"})
    return path


def _alert_row(
    db: DBSession,
    organization_id: str,
    alert: Alert,
    change: DetectedChange | None,
) -> dict[str, object]:
    snapshot = None
    old_snapshot = None
    if change:
        old_snapshot = db.scalar(
            select(SourceSnapshot).where(
                SourceSnapshot.id == change.old_snapshot_id,
                SourceSnapshot.organization_id == organization_id,
            )
        )
        snapshot = db.scalar(
            select(SourceSnapshot).where(
                SourceSnapshot.id == change.new_snapshot_id,
                SourceSnapshot.organization_id == organization_id,
            )
        )
    if snapshot is None:
        snapshot_query = (
            select(SourceSnapshot)
            .join(Source, Source.id == SourceSnapshot.source_id)
            .where(
                SourceSnapshot.organization_id == organization_id,
                SourceSnapshot.project_id == alert.project_id,
                Source.organization_id == organization_id,
                SourceSnapshot.validation_status.in_(["valid", "partial"]),
            )
        )
        if alert.source_id:
            snapshot_query = snapshot_query.where(SourceSnapshot.source_id == alert.source_id)
        else:
            snapshot_query = snapshot_query.where(
                Source.source_class == "official_regulator"
            )
        snapshot = db.scalar(snapshot_query.order_by(SourceSnapshot.fetched_at.desc()))
    conflict = None
    if alert.deduplication_key.startswith("conflict:"):
        conflict = db.scalar(
            select(SourceConflict).where(
                SourceConflict.organization_id == organization_id,
                SourceConflict.deduplication_key
                == alert.deduplication_key.removeprefix("conflict:"),
            )
        )
    old_evidence_id = change.old_snapshot_id if change else None
    new_evidence_id = change.new_snapshot_id if change else None
    old_content_hash = old_snapshot.content_hash if old_snapshot else None
    new_content_hash = snapshot.content_hash if snapshot else None
    page_reference = None
    conflict_reference = None
    document_extraction = None
    if conflict is not None:
        source_a_reference = dict(conflict.source_a_reference or {})
        source_b_reference = dict(conflict.source_b_reference or {})
        old_evidence_id = source_a_reference.get("snapshot_id") or source_a_reference.get(
            "document_version_id"
        )
        new_evidence_id = source_b_reference.get("snapshot_id") or source_b_reference.get(
            "document_version_id"
        )
        if source_a_reference.get("snapshot_id"):
            conflict_old_snapshot = db.get(
                SourceSnapshot, str(source_a_reference["snapshot_id"])
            )
            old_content_hash = (
                conflict_old_snapshot.content_hash if conflict_old_snapshot else old_content_hash
            )
        document_version_id = source_b_reference.get("document_version_id")
        if document_version_id:
            document_version = db.scalar(
                select(DocumentVersion).where(
                    DocumentVersion.id == str(document_version_id),
                    DocumentVersion.organization_id == organization_id,
                )
            )
            if document_version:
                new_content_hash = document_version.content_hash
                document_extraction = db.scalar(
                    select(DocumentExtraction)
                    .where(
                        DocumentExtraction.organization_id == organization_id,
                        DocumentExtraction.document_version_id == document_version.id,
                    )
                    .order_by(DocumentExtraction.created_at.desc())
                )
        page = source_b_reference.get("page") or source_b_reference.get("page_number")
        page_reference = f"Page {page}" if page else None
        conflict_reference = " | ".join(
            part
            for part in (
                f"official-snapshot:{old_evidence_id}" if old_evidence_id else None,
                f"document-version:{new_evidence_id}" if document_version_id else None,
                page_reference,
            )
            if part
        ) or None
    evidence = (
        db.scalar(
            select(ChangeEvidence)
            .where(
                ChangeEvidence.organization_id == organization_id,
                ChangeEvidence.detected_change_id == change.id,
                ChangeEvidence.evidence_side == "new",
            )
            .order_by(ChangeEvidence.created_at.desc())
        )
        if change
        else None
    )
    source_id = alert.source_id or (snapshot.source_id if snapshot else None)
    source = (
        db.scalar(
            select(Source).where(
                Source.id == source_id,
                Source.organization_id == organization_id,
            )
        )
        if source_id
        else None
    )
    review = db.scalar(
        select(AlertReview)
        .where(
            AlertReview.organization_id == organization_id,
            AlertReview.alert_id == alert.id,
        )
        .order_by(AlertReview.reviewed_at.desc())
    )
    reviewer = db.get(User, review.reviewer_user_id) if review else None
    source_reference = conflict_reference or (
        (evidence.source_url or evidence.evidence_reference)
        if evidence
        else None
    ) or (snapshot.final_url or snapshot.requested_url if snapshot else None) or (
        source.base_url if source else None
    )
    return {
        "alert_id": alert.id,
        "project_id": alert.project_id,
        "title": alert.title,
        "description": alert.neutral_description,
        "severity": alert.severity,
        "requires_review": alert.requires_review,
        "review_state": alert.state,
        "old_value": alert.old_value,
        "new_value": alert.new_value,
        "old_snapshot_id": old_evidence_id,
        "new_snapshot_id": new_evidence_id,
        "old_content_hash": old_content_hash,
        "new_content_hash": new_content_hash,
        "page_reference": page_reference,
        "detected_at": change.detected_at if change else alert.first_seen_at,
        "capture_timestamp": snapshot.fetched_at if snapshot else alert.first_seen_at,
        "source_reference": source_reference,
        "content_hash": (
            new_content_hash
            or evidence.content_hash
            if evidence
            else snapshot.content_hash
            if snapshot
            else None
        ),
        "parser_version": (
            document_extraction.provider_version
            if document_extraction
            else snapshot.parser_version
            if snapshot
            else None
        ),
        "reviewer": (
            reviewer.email
            if reviewer
            else review.reviewer_user_id
            if review
            else None
        ),
        "review_decision": review.decision if review else None,
        "limitations": alert.limitation,
    }


def _manifest_dict(row: dict[str, object]) -> dict[str, object]:
    return asdict(build_evidence_manifest((row,))[0])


def _write_report_artifacts(
    *,
    organization_id: str,
    report: Report,
    rows: list[dict[str, object]],
    formats: set[str],
) -> None:
    summary_lines = [
        f"{row['title']}: {row.get('old_value') or 'Unknown'} -> {row.get('new_value') or 'Unknown'}"
        for row in rows
    ] or ["No reviewed changes were selected for this draft."]
    status_lines = (
        ["APPROVED — client-facing evidence report."]
        if report.state == "approved"
        else [
            "DRAFT — INTERNAL REVIEW ONLY.",
            "This artifact is not approved for client distribution.",
        ]
    )
    evidence_lines: list[str] = []
    for row in rows:
        captured = row.get("capture_timestamp")
        captured_text = (
            captured.isoformat() if hasattr(captured, "isoformat") else str(captured or "Unknown")
        )
        review_text = (
            f"{row.get('review_decision')} by {row.get('reviewer')}"
            if row.get("review_decision")
            else "Pending review"
        )
        evidence_lines.append(
            " | ".join(
                (
                    f"Alert: {row.get('alert_id') or 'Unknown'}",
                    f"Source: {row.get('source_reference') or 'Unavailable'}",
                    f"Captured: {captured_text}",
                    f"Content hash: {row.get('content_hash') or 'Unavailable'}",
                    f"Parser: {row.get('parser_version') or 'Unavailable'}",
                    f"Review: {review_text}",
                )
            )
        )
    if not evidence_lines:
        evidence_lines.append("No evidence references are present in this report.")
    payload = {
        "report_id": report.id,
        "title": report.title,
        "state": report.state,
        "generated_at": datetime.now(UTC),
        "items": rows,
        "limitations": report.limitations,
        "disclaimer": PRODUCT_DISCLAIMER,
    }
    if "pdf" in formats:
        _artifact_path(organization_id, report.id, "pdf").write_bytes(
            export_pdf_report(
                title=report.title,
                sections=(
                    ("Report status", status_lines),
                    ("Evidence-backed changes", summary_lines),
                    ("Evidence references", evidence_lines),
                    (
                        "Review state",
                        ["High-impact items require reviewer confirmation before approval."],
                    ),
                ),
            )
        )
    if "csv" in formats:
        _artifact_path(organization_id, report.id, "csv").write_bytes(export_csv(rows))
    if "json" in formats:
        _artifact_path(organization_id, report.id, "json").write_bytes(export_json(payload))
    if "evidence" in formats:
        evidence_rows = [asdict(item) for item in build_evidence_manifest(rows)]
        _artifact_path(organization_id, report.id, "evidence").write_bytes(
            export_csv(evidence_rows)
        )


def _refresh_report_artifacts(
    *,
    organization_id: str,
    report: Report,
    rows: list[dict[str, object]],
    formats: set[str],
) -> None:
    _write_report_artifacts(
        organization_id=organization_id,
        report=report,
        rows=rows,
        formats=formats,
    )
    primary_format = "pdf" if "pdf" in formats else sorted(formats)[0]
    primary = _artifact_path(organization_id, report.id, primary_format)
    report.storage_key = report.id
    report.mime_type = {
        "pdf": "application/pdf",
        "csv": "text/csv",
        "json": "application/json",
        "evidence": "text/csv",
    }[primary_format]
    report.sha256 = hashlib.sha256(primary.read_bytes()).hexdigest()
    report.size_bytes = primary.stat().st_size


@router.post("/reports", response_model=ReportResponse)
def create_report(
    payload: ReportCreate,
    request: Request,
    db: DBSession,
    actor: AnalystActor,
    csrf: CSRFProtected,
) -> Report:
    del csrf
    existing = db.scalar(
        select(Report).where(
            Report.organization_id == actor.organization_id,
            Report.deduplication_key == payload.idempotency_key,
            Report.deleted_at.is_(None),
        )
    )
    if existing:
        return existing
    formats = set(payload.formats)
    if invalid := formats - ALLOWED_FORMATS:
        raise HTTPException(
            status_code=422,
            detail={"code": "unsupported_report_format", "formats": sorted(invalid)},
        )
    if not formats:
        formats = {"pdf", "csv", "json", "evidence"}
    query = select(Alert).where(Alert.organization_id == actor.organization_id)
    if payload.project_ids:
        owned = set(
            db.scalars(
                select(Project.id).where(
                    Project.organization_id == actor.organization_id,
                    Project.id.in_(payload.project_ids),
                    Project.deleted_at.is_(None),
                )
            )
        )
        if owned != set(payload.project_ids):
            raise HTTPException(status_code=404, detail={"code": "project_not_found"})
        query = query.where(Alert.project_id.in_(payload.project_ids))
    alerts = list(db.scalars(query.order_by(Alert.first_seen_at.desc())))
    organization = db.scalar(
        select(Organization).where(
            Organization.id == actor.organization_id,
            Organization.is_active.is_(True),
        )
    )
    if organization is None:
        raise HTTPException(status_code=404, detail={"code": "organization_not_found"})
    report = Report(
        organization_id=actor.organization_id,
        title=payload.name,
        report_type=payload.report_type,
        state="draft",
        generated_by_user_id=actor.user_id,
        product_disclaimer=PRODUCT_DISCLAIMER,
        summary_metrics={"alert_count": len(alerts)},
        limitations=[
            "Source coverage may be incomplete or delayed.",
            "This report is decision support, not legal or investment advice.",
        ],
        deduplication_key=payload.idempotency_key,
        expires_at=datetime.now(UTC)
        + timedelta(days=organization.retention_report_days),
    )
    db.add(report)
    db.flush()
    rows: list[dict[str, object]] = []
    for ordinal, alert in enumerate(alerts):
        change = db.get(DetectedChange, alert.detected_change_id) if alert.detected_change_id else None
        row = _alert_row(db, actor.organization_id, alert, change)
        rows.append(row)
        db.add(
            ReportItem(
                organization_id=actor.organization_id,
                report_id=report.id,
                project_id=alert.project_id,
                alert_id=alert.id,
                detected_change_id=alert.detected_change_id,
                signal_id=alert.signal_id,
                item_type="alert",
                ordinal=ordinal,
                title=alert.title,
                narrative=alert.neutral_description,
                evidence_manifest=_manifest_dict(row),
                limitations=[alert.limitation] if alert.limitation else [],
            )
        )
    _refresh_report_artifacts(
        organization_id=actor.organization_id,
        report=report,
        rows=rows,
        formats=formats,
    )
    report.summary_metrics = {**report.summary_metrics, "formats": sorted(formats)}
    add_audit_event(
        db,
        actor=actor,
        action="report.created",
        resource_type="report",
        resource_id=report.id,
        request=request,
        metadata={"formats": sorted(formats), "items": len(rows)},
    )
    commit_or_rollback(db)
    db.refresh(report)
    return report


@router.get("/reports", response_model=list[ReportResponse])
def list_reports(db: DBSession, actor: CurrentActor) -> list[Report]:
    return list(
        db.scalars(
            select(Report)
            .where(
                Report.organization_id == actor.organization_id,
                Report.deleted_at.is_(None),
            )
            .order_by(Report.created_at.desc())
        )
    )


@router.get("/reports/{report_id}", response_model=ReportResponse)
def get_report(report_id: str, db: DBSession, actor: CurrentActor) -> Report:
    return get_tenant_report(db, actor.organization_id, report_id)


@router.post("/reports/{report_id}/approve", response_model=ReportResponse)
def approve_report(
    report_id: str,
    payload: ReportApprove,
    request: Request,
    db: DBSession,
    actor: ReviewerActor,
    csrf: CSRFProtected,
) -> Report:
    del csrf
    report = get_tenant_report(db, actor.organization_id, report_id)
    items = list(
        db.scalars(
            select(ReportItem)
            .where(
                ReportItem.organization_id == actor.organization_id,
                ReportItem.report_id == report.id,
            )
            .order_by(ReportItem.ordinal)
        )
    )
    alerts: list[Alert] = []
    rows: list[dict[str, object]] = []
    for item in items:
        if not item.alert_id:
            continue
        alert = db.scalar(
            select(Alert).where(
                Alert.id == item.alert_id,
                Alert.organization_id == actor.organization_id,
            )
        )
        if alert is None:
            continue
        change = (
            db.scalar(
                select(DetectedChange).where(
                    DetectedChange.id == item.detected_change_id,
                    DetectedChange.organization_id == actor.organization_id,
                )
            )
            if item.detected_change_id
            else None
        )
        row = _alert_row(db, actor.organization_id, alert, change)
        if alert.state == "dismissed":
            continue
        item.evidence_manifest = _manifest_dict(row)
        alerts.append(alert)
        rows.append(row)
    try:
        validate_client_report(alerts)
    except ReportApprovalError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "unresolved_required_review",
                "message": str(exc),
                "alert_ids": list(exc.unresolved_ids),
            },
        ) from exc
    try:
        validate_evidence_manifest(rows)
    except ReportEvidenceError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "incomplete_evidence_manifest",
                "message": str(exc),
                "missing_fields": exc.missing_fields,
            },
        ) from exc
    report.state = "approved"
    report.approved_by_user_id = actor.user_id
    report.approved_at = datetime.now(UTC)
    formats = set(report.summary_metrics.get("formats", ()))
    if formats:
        _refresh_report_artifacts(
            organization_id=actor.organization_id,
            report=report,
            rows=rows,
            formats=formats,
        )
    add_audit_event(
        db,
        actor=actor,
        action="report.approved",
        resource_type="report",
        resource_id=report.id,
        request=request,
        metadata={"note": payload.note},
    )
    commit_or_rollback(db)
    db.refresh(report)
    return report


@router.get("/reports/{report_id}/download", response_class=FileResponse)
def download_report(
    report_id: str,
    db: DBSession,
    actor: CurrentActor,
    format: str = "pdf",
) -> FileResponse:
    report = get_tenant_report(db, actor.organization_id, report_id)
    if report.state != "approved" and not role_allows(actor.role, Role.ANALYST):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "draft_report_restricted",
                "message": "Draft reports are available only to internal analyst roles.",
            },
        )
    if report.expires_at and report.expires_at < datetime.now(UTC):
        raise HTTPException(status_code=410, detail={"code": "report_expired"})
    path = _artifact_path(actor.organization_id, report.id, format)
    if not path.is_file():
        raise HTTPException(status_code=404, detail={"code": "artifact_not_found"})
    media = {
        "pdf": "application/pdf",
        "csv": "text/csv",
        "json": "application/json",
        "evidence": "text/csv",
    }[format]
    return FileResponse(
        path,
        media_type=media,
        filename=path.name,
        headers={"Cache-Control": "private, no-store"},
    )


@router.delete("/reports/{report_id}", response_model=DeleteResponse)
def delete_report(
    report_id: str,
    request: Request,
    db: DBSession,
    actor: AnalystActor,
    csrf: CSRFProtected,
) -> DeleteResponse:
    del csrf
    report = db.scalar(
        select(Report).where(
            Report.id == report_id,
            Report.organization_id == actor.organization_id,
        )
    )
    if report is None or report.deleted_at is not None:
        return DeleteResponse(deleted=False)
    for format_name in ALLOWED_FORMATS:
        _artifact_path(actor.organization_id, report.id, format_name).unlink(missing_ok=True)
    report.deleted_at = datetime.now(UTC)
    report.state = "deleted"
    db.add(
        DeletionTombstone(
            organization_id=actor.organization_id,
            resource_type="report",
            resource_id=report.id,
            request_id=request.state.request_id,
            idempotency_key=f"report:{actor.organization_id}:{report.id}",
            deleted_by_user_id=actor.user_id,
            reason_code="user_requested",
            minimal_metadata={"sha256": report.sha256},
        )
    )
    add_audit_event(
        db,
        actor=actor,
        action="report.deleted",
        resource_type="report",
        resource_id=report.id,
        request=request,
    )
    commit_or_rollback(db)
    return DeleteResponse(deleted=True)


@router.get("/markets/{location_id}/summary", response_model=MarketSummary)
def market_summary(location_id: str, db: DBSession, actor: CurrentActor) -> MarketSummary:
    base = (
        Project.organization_id == actor.organization_id,
        Project.location_id == location_id,
        Project.deleted_at.is_(None),
    )
    projects_monitored = int(
        db.scalar(select(func.count()).select_from(Project).where(*base)) or 0
    )
    active = int(
        db.scalar(
            select(func.count()).select_from(Project).where(*base, Project.current_status == "active")
        )
        or 0
    )
    lapsed = int(
        db.scalar(select(func.count()).select_from(Project).where(*base, Project.lapsed_flag.is_(True)))
        or 0
    )
    qpr = int(
        db.scalar(
            select(func.count()).select_from(Project).where(*base, Project.qpr_missing_flag.is_(True))
        )
        or 0
    )
    project_ids = select(Project.id).where(*base)
    since = datetime.now(UTC) - timedelta(days=30)
    completion_changes = int(
        db.scalar(
            select(func.count())
            .select_from(DetectedChange)
            .where(
                DetectedChange.organization_id == actor.organization_id,
                DetectedChange.project_id.in_(project_ids),
                DetectedChange.change_type.in_(["date_extended", "date_moved_earlier"]),
                DetectedChange.review_state.in_(["confirmed", "expected"]),
                DetectedChange.detected_at >= since,
            )
        )
        or 0
    )
    new_phases = int(
        db.scalar(
            select(func.count())
            .select_from(Signal)
            .where(
                Signal.organization_id == actor.organization_id,
                Signal.project_id.in_(project_ids),
                Signal.rule_code == "NEW_PHASE_OR_REGISTRATION",
                Signal.detected_at >= since,
            )
        )
        or 0
    )
    developer_count = int(
        db.scalar(select(func.count(distinct(Project.developer_id))).where(*base)) or 0
    )
    source_health = dict(
        db.execute(
            select(Source.health_status, func.count())
            .where(Source.organization_id == actor.organization_id)
            .group_by(Source.health_status)
        )
    )
    return MarketSummary(
        location_id=location_id,
        projects_monitored=projects_monitored,
        active_projects=active,
        lapsed_projects=lapsed,
        qpr_flagged_projects=qpr,
        recent_reviewed_completion_changes=completion_changes,
        new_registrations_or_phases=new_phases,
        developer_count=developer_count,
        source_health={str(key): int(value) for key, value in source_health.items()},
    )


@router.get("/weekly-digest")
def weekly_digest(db: DBSession, actor: CurrentActor) -> dict[str, object]:
    since = datetime.now(UTC) - timedelta(days=7)
    from app.models import MonitoringRun

    runs = list(
        db.scalars(
            select(MonitoringRun).where(
                MonitoringRun.organization_id == actor.organization_id,
                MonitoringRun.created_at >= since,
            )
        )
    )
    signals = list(
        db.scalars(
            select(Signal).where(
                Signal.organization_id == actor.organization_id,
                Signal.detected_at >= since,
            )
        )
    )
    alerts = list(
        db.scalars(
            select(Alert).where(
                Alert.organization_id == actor.organization_id,
                Alert.first_seen_at >= since,
            )
        )
    )
    return {
        "period_start": since,
        "period_end": datetime.now(UTC),
        "metrics": weekly_digest_metrics(
            monitoring_runs=runs, signals=signals, alerts=alerts
        ),
        "disclaimer": PRODUCT_DISCLAIMER,
    }


@router.get("/audit", response_model=list[AuditResponse])
def audit_history(
    db: DBSession,
    actor: CurrentActor,
    limit: int = 100,
    offset: int = 0,
) -> list[AuditEvent]:
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    return list(
        db.scalars(
            select(AuditEvent)
            .where(AuditEvent.organization_id == actor.organization_id)
            .order_by(AuditEvent.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    )
