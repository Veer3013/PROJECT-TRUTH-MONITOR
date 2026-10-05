from __future__ import annotations

import csv
import hashlib
import io
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import ValidationError
from sqlalchemy import select

from app.api.deps import AnalystActor, CSRFProtected, DBSession
from app.api.utils import add_audit_event, commit_or_rollback
from app.api.v1.projects import create_or_resolve_project
from app.api.v1.watchlists import get_tenant_watchlist
from app.core.config import get_settings
from app.models import Project, WatchlistItem
from app.schemas.projects import ProjectCreate
from app.schemas.watchlists import (
    CSVCommitRequest,
    CSVPreviewRequest,
    CSVPreviewResponse,
    CSVPreviewRow,
    WatchlistItemCreate,
)
from app.services.normalization import normalize_registration_number

router = APIRouter(prefix="/imports/projects", tags=["Imports"])
EXPECTED_FIELDS = {
    "registration_number",
    "project_name",
    "developer_name",
    "city",
    "locality_or_sector",
    "official_url",
    "monitoring_cadence",
}


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().secret_key, salt="project-csv-preview-v1")


def _read_rows(csv_text: str) -> list[dict[str, str | None]]:
    text = csv_text.lstrip("\ufeff")
    try:
        reader = csv.DictReader(io.StringIO(text, newline=""))
        headers = {str(item).strip() for item in (reader.fieldnames or []) if item}
        if not headers:
            raise ValueError("CSV header row is missing")
        unsupported = sorted(headers - EXPECTED_FIELDS)
        if unsupported:
            raise ValueError(f"Unsupported columns: {', '.join(unsupported)}")
        rows: list[dict[str, str | None]] = []
        for raw in reader:
            row = {
                field: (str(raw.get(field, "")).strip() or None) for field in EXPECTED_FIELDS
            }
            if any(value is not None for value in row.values()):
                rows.append(row)
            if len(rows) > 5_000:
                raise ValueError("CSV imports are limited to 5,000 rows")
        return rows
    except csv.Error as exc:
        raise ValueError(f"CSV could not be parsed: {exc}") from exc


def _validate_row(row: dict[str, str | None], row_number: int) -> tuple[CSVPreviewRow, dict[str, Any]]:
    normalized = dict(row)
    normalized["registration_number"] = normalize_registration_number(
        row.get("registration_number")
    )
    normalized["city"] = row.get("city") or "Gurugram"
    normalized["monitoring_cadence"] = (
        row.get("monitoring_cadence") or get_settings().default_monitoring_cadence
    )
    errors: list[str] = []
    if not normalized["registration_number"] and not row.get("project_name"):
        errors.append("Provide registration_number or project_name")
    if normalized["monitoring_cadence"] not in {"manual", "daily", "weekly"}:
        errors.append("monitoring_cadence must be manual, daily or weekly")
    try:
        validated = WatchlistItemCreate(**normalized)
        serializable = validated.model_dump(mode="json")
    except ValidationError as exc:
        serializable = normalized
        errors.extend(
            f"{'.'.join(str(part) for part in issue['loc'])}: {issue['msg']}"
            for issue in exc.errors()
        )
    return (
        CSVPreviewRow(
            row_number=row_number,
            normalized=normalized,
            errors=errors,
            duplicate=False,
        ),
        serializable,
    )


@router.post("/preview", response_model=CSVPreviewResponse)
def preview_projects(
    payload: CSVPreviewRequest,
    db: DBSession,
    actor: AnalystActor,
    csrf: CSRFProtected,
) -> CSVPreviewResponse:
    del csrf
    try:
        raw_rows = _read_rows(payload.csv_text)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_csv", "message": str(exc)},
        ) from exc
    previews: list[CSVPreviewRow] = []
    token_rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row_number, raw in enumerate(raw_rows, start=2):
        preview, serializable = _validate_row(raw, row_number)
        registration = preview.normalized.get("registration_number")
        key = (
            f"r:{registration}"
            if registration
            else "n:"
            + "|".join(
                str(preview.normalized.get(part) or "").casefold()
                for part in ("project_name", "developer_name", "city", "locality_or_sector")
            )
        )
        duplicate = key in seen
        seen.add(key)
        if registration and not duplicate:
            duplicate = (
                db.scalar(
                    select(Project.id).where(
                        Project.organization_id == actor.organization_id,
                        Project.registration_number == registration,
                        Project.deleted_at.is_(None),
                    )
                )
                is not None
            )
        preview.duplicate = duplicate
        previews.append(preview)
        token_rows.append(
            {
                "row_number": row_number,
                "data": serializable,
                "errors": preview.errors,
                "duplicate": duplicate,
            }
        )
    token = _serializer().dumps(
        {"organization_id": actor.organization_id, "user_id": actor.user_id, "rows": token_rows}
    )
    return CSVPreviewResponse(
        preview_token=token,
        rows=previews,
        valid_count=sum(not row.errors for row in previews),
        error_count=sum(bool(row.errors) for row in previews),
    )


@router.post("/commit", status_code=status.HTTP_201_CREATED)
def commit_projects(
    payload: CSVCommitRequest,
    request: Request,
    db: DBSession,
    actor: AnalystActor,
    csrf: CSRFProtected,
) -> dict[str, object]:
    del csrf
    watchlist = get_tenant_watchlist(db, actor.organization_id, payload.watchlist_id)
    try:
        preview = _serializer().loads(payload.preview_token, max_age=1_800)
    except SignatureExpired as exc:
        raise HTTPException(
            status_code=410,
            detail={"code": "preview_expired", "message": "Run the CSV dry-run again."},
        ) from exc
    except BadSignature as exc:
        raise HTTPException(status_code=400, detail={"code": "invalid_preview_token"}) from exc
    if preview.get("organization_id") != actor.organization_id:
        raise HTTPException(status_code=403, detail={"code": "tenant_mismatch"})
    invalid = [row for row in preview.get("rows", []) if row.get("errors")]
    if invalid:
        raise HTTPException(
            status_code=409,
            detail={"code": "preview_has_errors", "rows": [row["row_number"] for row in invalid]},
        )
    created_count = 0
    duplicate_count = 0
    item_ids: list[str] = []
    for row in preview.get("rows", []):
        values = WatchlistItemCreate(**row["data"])
        project, _ = create_or_resolve_project(
            db,
            organization_id=actor.organization_id,
            proposed_by_user_id=actor.user_id,
            payload=ProjectCreate(
                registration_number=values.registration_number,
                name=values.project_name or values.registration_number or "Unverified project",
                developer_name=values.developer_name,
                city=values.city or "Gurugram",
                locality_or_sector=values.locality_or_sector,
                official_url=values.official_url,
            ),
        )
        row_key = hashlib.sha256(
            f"{actor.organization_id}:{payload.idempotency_key}:{row['row_number']}".encode()
        ).hexdigest()
        existing = db.scalar(
            select(WatchlistItem).where(
                WatchlistItem.organization_id == actor.organization_id,
                WatchlistItem.watchlist_id == watchlist.id,
                (
                    (WatchlistItem.project_id == project.id)
                    | (WatchlistItem.idempotency_key == row_key)
                ),
            )
        )
        if existing:
            duplicate_count += 1
            item_ids.append(existing.id)
            continue
        item = WatchlistItem(
            organization_id=actor.organization_id,
            watchlist_id=watchlist.id,
            project_id=project.id,
            official_url=str(values.official_url) if values.official_url else None,
            monitoring_cadence=values.monitoring_cadence or watchlist.monitoring_cadence,
            input_identity=values.model_dump(mode="json"),
            status="active",
            idempotency_key=row_key,
            added_by_user_id=actor.user_id,
        )
        db.add(item)
        db.flush()
        created_count += 1
        item_ids.append(item.id)
    add_audit_event(
        db,
        actor=actor,
        action="project_import.committed",
        resource_type="watchlist",
        resource_id=watchlist.id,
        request=request,
        metadata={"created": created_count, "duplicates": duplicate_count},
    )
    commit_or_rollback(db)
    return {
        "watchlist_id": watchlist.id,
        "created_count": created_count,
        "duplicate_count": duplicate_count,
        "item_ids": item_ids,
    }
