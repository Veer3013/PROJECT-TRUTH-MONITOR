from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import hash_password, verify_password
from app.db import session_scope
from app.models import (
    Alert,
    AuditEvent,
    ChangeEvidence,
    DetectedChange,
    Developer,
    Document,
    DocumentClaim,
    DocumentExtraction,
    DocumentVersion,
    Location,
    MarketSnapshot,
    MonitoringJob,
    MonitoringRun,
    Organization,
    OrganizationMembership,
    Project,
    ProjectSourceLink,
    Report,
    ReportItem,
    Signal,
    SnapshotField,
    Source,
    SourceAccessPolicy,
    SourceConflict,
    SourceSnapshot,
    User,
    Watchlist,
    WatchlistItem,
)
from app.services.normalization import comparison_text

DEMO_ORGANIZATION_NAME = "ProjectTruth Demo Advisory"
DEMO_ORGANIZATION_SLUG = "projecttruth-demo"
DEMO_WATCHLIST_NAME = "Gurugram Pilot"
RULE_VERSION = "2026.08.1"

DEMO_USERS = (
    ("admin@projecttruth.local", "DemoAdmin!2026", "Demo Admin", "admin"),  # noqa: S105
    (
        "reviewer@projecttruth.local",
        "DemoReviewer!2026",  # noqa: S105
        "Demo Reviewer",
        "reviewer",
    ),
    (
        "analyst@projecttruth.local",
        "DemoAnalyst!2026",  # noqa: S105
        "Demo Analyst",
        "analyst",
    ),
    ("viewer@projecttruth.local", "DemoViewer!2026", "Demo Viewer", "viewer"),  # noqa: S105
)

PRODUCT_DISCLAIMER = (
    "ProjectTruth Monitor is decision support based on the monitored dataset. "
    "It is not legal, investment, valuation, or regulatory advice."
)
DATASET_LIMITATION = (
    "Based only on projects in this monitored dataset - not the complete Gurugram market."
)


@dataclass(frozen=True, slots=True)
class FieldSeed:
    key: str
    raw_label: str
    raw_value: str | None
    value: Any
    value_type: str = "text"
    unit: str | None = None
    selector: str | None = None
    page_number: int | None = None
    extraction_method: str = "fixture_html"
    extraction_confidence: float = 1.0
    validation_warnings: tuple[str, ...] = ()


def _utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json_value(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _text_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def _upsert[ModelT](
    db: Session,
    model: type[ModelT],
    *,
    lookup: dict[str, Any],
    values: dict[str, Any],
) -> ModelT:
    row = db.scalar(select(model).filter_by(**lookup))
    if row is None:
        row = model(**{**lookup, **values})
        db.add(row)
    else:
        for field_name, value in values.items():
            setattr(row, field_name, value)
    db.flush()
    return row


def _seed_organization_and_users(
    db: Session,
) -> tuple[Organization, dict[str, User]]:
    organization = _upsert(
        db,
        Organization,
        lookup={"slug": DEMO_ORGANIZATION_SLUG},
        values={
            "name": DEMO_ORGANIZATION_NAME,
            "is_active": True,
            "retention_upload_days": 90,
            "retention_report_days": 90,
        },
    )

    users: dict[str, User] = {}
    for email, password, display_name, role in DEMO_USERS:
        user = db.scalar(select(User).where(User.email == email))
        if user is None:
            user = User(
                email=email,
                password_hash=hash_password(password),
                display_name=display_name,
                is_active=True,
            )
            db.add(user)
            db.flush()
        else:
            user.display_name = display_name
            user.is_active = True
            if not verify_password(password, user.password_hash):
                user.password_hash = hash_password(password)
            db.flush()
        users[role] = user
        _upsert(
            db,
            OrganizationMembership,
            lookup={"organization_id": organization.id, "user_id": user.id},
            values={"role": role, "is_active": True},
        )
    return organization, users


def _seed_source_policy(
    db: Session,
    *,
    organization_id: str,
    source: Source,
    reviewer_user_id: str,
    ocr_allowed: bool = False,
) -> SourceAccessPolicy:
    version = "1.0.0"
    other_current = list(
        db.scalars(
            select(SourceAccessPolicy).where(
                SourceAccessPolicy.organization_id == organization_id,
                SourceAccessPolicy.source_id == source.id,
                SourceAccessPolicy.policy_version != version,
                SourceAccessPolicy.is_current.is_(True),
            )
        )
    )
    for policy in other_current:
        policy.is_current = False
    if other_current:
        db.flush()
    return _upsert(
        db,
        SourceAccessPolicy,
        lookup={
            "organization_id": organization_id,
            "source_id": source.id,
            "policy_version": version,
        },
        values={
            "is_enabled": True,
            "is_current": True,
            "permitted_access_modes": ["fixture"],
            "rate_limit_requests": 1,
            "rate_limit_period_seconds": 1.0,
            "robots_review_status": "not_applicable_synthetic_fixture",
            "terms_review_status": "consented_synthetic_content",
            "evidence_retention_days": 90,
            "raw_content_may_be_stored": True,
            "playwright_allowed": False,
            "ocr_allowed": ocr_allowed,
            "llm_allowed": False,
            "reviewer_user_id": reviewer_user_id,
            "reviewed_at": _utc(2026, 7, 1),
            "notes": (
                "Frozen synthetic ProjectTruth fixture content. It does not represent "
                "a real project, registration, developer, or regulator record."
            ),
        },
    )


def _seed_sources(
    db: Session,
    *,
    organization_id: str,
    reviewer_user_id: str,
) -> dict[str, Source]:
    source_definitions = {
        "official": {
            "name": "ProjectTruth frozen HRERA demo fixtures",
            "source_class": "official_regulator",
            "base_url": "fixture://hrera/",
            "ocr_allowed": False,
        },
        "brochure": {
            "name": "ProjectTruth frozen authorized brochure fixtures",
            "source_class": "marketing_brochure",
            "base_url": "fixture://documents/",
            "ocr_allowed": True,
        },
        "developer": {
            "name": "ProjectTruth frozen developer page fixtures",
            "source_class": "official_developer",
            "base_url": "fixture://developer/",
            "ocr_allowed": False,
        },
    }
    sources: dict[str, Source] = {}
    for key, definition in source_definitions.items():
        source = _upsert(
            db,
            Source,
            lookup={
                "organization_id": organization_id,
                "domain": "fixtures.projecttruth.invalid",
                "name": definition["name"],
            },
            values={
                "source_type": "fixture",
                "source_class": definition["source_class"],
                "base_url": definition["base_url"],
                "is_enabled": True,
                "health_status": "healthy",
                "last_successful_fetch_at": _utc(2026, 5, 11, 11),
                "last_content_hash": _sha256(f"projecttruth-demo:{key}:latest"),
                "consecutive_failures": 0,
                "circuit_breaker_state": "closed",
            },
        )
        _seed_source_policy(
            db,
            organization_id=organization_id,
            source=source,
            reviewer_user_id=reviewer_user_id,
            ocr_allowed=bool(definition["ocr_allowed"]),
        )
        sources[key] = source
    return sources


def _seed_location(
    db: Session, organization_id: str, locality: str, *, latitude: float, longitude: float
) -> Location:
    return _upsert(
        db,
        Location,
        lookup={
            "organization_id": organization_id,
            "country": "India",
            "state": "Haryana",
            "city": "Gurugram",
            "locality_or_sector": locality,
        },
        values={
            "postal_code": None,
            "latitude": latitude,
            "longitude": longitude,
        },
    )


def _seed_developer(db: Session, organization_id: str, name: str, website_slug: str) -> Developer:
    normalized_name = comparison_text(name)
    return _upsert(
        db,
        Developer,
        lookup={"organization_id": organization_id, "normalized_name": normalized_name},
        values={
            "canonical_name": name,
            "registration_identifier": f"DEMO-{website_slug.upper()}",
            "website_url": f"https://fixtures.projecttruth.invalid/developers/{website_slug}",
            "identity_status": "confirmed",
            "merged_into_developer_id": None,
        },
    )


def _seed_project(
    db: Session,
    *,
    organization_id: str,
    developer: Developer,
    location: Location,
    source: Source,
    name: str,
    registration_number: str | None,
    official_url: str | None,
    phase_name: str | None = None,
    parent_project_id: str | None = None,
    project_type: str | None = "Residential group housing",
    current_status: str = "Registered",
    proposed_completion_date: datetime | None = None,
    revised_completion_date: datetime | None = None,
    tracked_at: datetime | None = None,
) -> Project:
    normalized_name = comparison_text(name)
    if registration_number:
        lookup = {
            "organization_id": organization_id,
            "registration_number": registration_number,
        }
    else:
        lookup = {
            "organization_id": organization_id,
            "normalized_name": normalized_name,
            "developer_id": developer.id,
        }
    return _upsert(
        db,
        Project,
        lookup=lookup,
        values={
            "developer_id": developer.id,
            "location_id": location.id,
            "parent_project_id": parent_project_id,
            "registration_source_id": source.id,
            "name": name,
            "normalized_name": normalized_name,
            "phase_name": phase_name,
            "project_type": project_type,
            "official_url": official_url,
            "identity_status": "confirmed",
            "is_provisional": False,
            "current_status": current_status,
            "proposed_completion_date": proposed_completion_date,
            "revised_completion_date": revised_completion_date,
            "lapsed_flag": False,
            "qpr_missing_flag": False,
            "last_verified_at": tracked_at,
            "tracking_started_at": tracked_at or _utc(2026, 1, 1),
            "deleted_at": None,
        },
    )


def _seed_projects(
    db: Session,
    *,
    organization_id: str,
    sources: dict[str, Source],
) -> tuple[dict[str, Project], dict[str, Location]]:
    locations = {
        "sector79": _seed_location(
            db, organization_id, "Sector 79", latitude=28.3959, longitude=76.9431
        ),
        "sector88": _seed_location(
            db, organization_id, "Sector 88", latitude=28.4337, longitude=76.9633
        ),
        "sector65": _seed_location(
            db, organization_id, "Sector 65", latitude=28.4031, longitude=77.0680
        ),
    }
    developers = {
        "aster": _seed_developer(
            db, organization_id, "Aster Demo Developers Private Limited", "aster-demo"
        ),
        "lakeview": _seed_developer(
            db, organization_id, "Lakeview Demo Realty Limited", "lakeview-demo"
        ),
        "orion": _seed_developer(db, organization_id, "Orion Demo Estates Limited", "orion-demo"),
        "metro": _seed_developer(db, organization_id, "Metro Demo Developments LLP", "metro-demo"),
    }

    projects: dict[str, Project] = {}
    projects["aster"] = _seed_project(
        db,
        organization_id=organization_id,
        developer=developers["aster"],
        location=locations["sector79"],
        source=sources["official"],
        name="Aster Heights Phase II",
        registration_number="HRERA-DEMO-101",
        official_url="fixture://case_a_completion_extended",
        phase_name="Phase II",
        proposed_completion_date=_utc(2027, 6, 30),
        revised_completion_date=_utc(2028, 3, 31),
        tracked_at=_utc(2026, 1, 5, 9),
    )
    projects["lakeview"] = _seed_project(
        db,
        organization_id=organization_id,
        developer=developers["lakeview"],
        location=locations["sector88"],
        source=sources["official"],
        name="Lakeview Residences",
        registration_number="HRERA-DEMO-202",
        official_url="fixture://case_b_official_record",
        proposed_completion_date=_utc(2028, 6, 30),
        tracked_at=_utc(2026, 3, 3, 10, 15),
    )
    projects["orion_parent"] = _seed_project(
        db,
        organization_id=organization_id,
        developer=developers["orion"],
        location=locations["sector79"],
        source=sources["official"],
        name="Orion Greens",
        registration_number=None,
        official_url="fixture://case_c_multiple_phases",
        project_type="Residential master development",
        tracked_at=_utc(2026, 4, 1),
    )
    projects["orion_phase"] = _seed_project(
        db,
        organization_id=organization_id,
        developer=developers["orion"],
        location=locations["sector79"],
        source=sources["official"],
        name="Orion Greens Phase III",
        registration_number="HRERA-DEMO-303",
        official_url="fixture://case_c_new_phase",
        phase_name="Phase III",
        parent_project_id=projects["orion_parent"].id,
        tracked_at=_utc(2026, 4, 7, 8, 30),
    )
    projects["metro"] = _seed_project(
        db,
        organization_id=organization_id,
        developer=developers["metro"],
        location=locations["sector65"],
        source=sources["official"],
        name="Metro Square",
        registration_number="HRERA-DEMO-404",
        official_url="fixture://case_d_official_missing_completion",
        project_type="Mixed-use commercial",
        proposed_completion_date=None,
        revised_completion_date=None,
        tracked_at=_utc(2026, 5, 11, 11),
    )
    return projects, locations


def _seed_watchlist(
    db: Session,
    *,
    organization_id: str,
    analyst_user_id: str,
    projects: dict[str, Project],
) -> Watchlist:
    watchlist = _upsert(
        db,
        Watchlist,
        lookup={"organization_id": organization_id, "name": DEMO_WATCHLIST_NAME},
        values={
            "description": (
                "Synthetic Gurugram evidence-monitoring pilot covering the four "
                "ProjectTruth demo cases."
            ),
            "monitoring_cadence": "weekly",
            "alert_preferences": {
                "completion_date_changes": True,
                "source_conflicts": True,
                "new_phases": True,
                "review_required": True,
            },
            "is_active": True,
            "created_by_user_id": analyst_user_id,
            "deleted_at": None,
        },
    )
    item_definitions = (
        ("aster", "weekly"),
        ("lakeview", "daily"),
        ("orion_phase", "manual"),
        ("metro", "weekly"),
    )
    for key, cadence in item_definitions:
        project = projects[key]
        _upsert(
            db,
            WatchlistItem,
            lookup={
                "organization_id": organization_id,
                "watchlist_id": watchlist.id,
                "project_id": project.id,
            },
            values={
                "official_url": project.official_url,
                "monitoring_cadence": cadence,
                "input_identity": {
                    "registration_number": project.registration_number,
                    "project_name": project.name,
                    "source": "synthetic_demo_seed",
                },
                "status": "active",
                "idempotency_key": f"demo:watchlist:{project.registration_number}",
                "added_by_user_id": analyst_user_id,
            },
        )
    return watchlist


def _ensure_job(
    db: Session,
    *,
    organization_id: str,
    source: Source,
    project: Project,
    watchlist: Watchlist,
    cadence: str,
) -> MonitoringJob:
    _upsert(
        db,
        ProjectSourceLink,
        lookup={
            "organization_id": organization_id,
            "project_id": project.id,
            "source_id": source.id,
        },
        values={
            "source_role": (
                "primary_registration"
                if project.registration_source_id == source.id
                else "supporting"
            ),
            "is_active": True,
        },
    )
    watchlist_item = db.scalar(
        select(WatchlistItem).where(
            WatchlistItem.organization_id == organization_id,
            WatchlistItem.watchlist_id == watchlist.id,
            WatchlistItem.project_id == project.id,
        )
    )
    if watchlist_item is None:
        raise RuntimeError(f"Demo watchlist item is missing for project {project.id}")
    return _upsert(
        db,
        MonitoringJob,
        lookup={
            "organization_id": organization_id,
            "deduplication_key": f"demo:job:{source.id}:{project.registration_number}",
        },
        values={
            "source_id": source.id,
            "project_id": project.id,
            "watchlist_id": watchlist.id,
            "job_type": "source_refresh",
            "cadence": cadence,
            "state": "scheduled",
            "next_run_at": _utc(2026, 8, 3, 9),
            "last_run_at": project.last_verified_at,
            "retry_count": 0,
            "max_retries": 3,
            "payload": {
                "fixture_key": (project.official_url or "").removeprefix("fixture://"),
                "watchlist_item_id": watchlist_item.id,
                "seeded": True,
            },
        },
    )


def _ensure_run(
    db: Session,
    *,
    organization_id: str,
    job: MonitoringJob | None,
    source: Source,
    project: Project,
    analyst_user_id: str,
    key: str,
    fetched_at: datetime,
    completeness: str = "complete",
) -> MonitoringRun:
    return _upsert(
        db,
        MonitoringRun,
        lookup={
            "organization_id": organization_id,
            "deduplication_key": f"demo:run:{key}",
        },
        values={
            "monitoring_job_id": job.id if job else None,
            "source_id": source.id,
            "project_id": project.id,
            "triggered_by_user_id": analyst_user_id,
            "trigger_type": "seed_fixture",
            "status": "succeeded",
            "attempt_number": 1,
            "started_at": fetched_at,
            "completed_at": fetched_at,
            "fetched_count": 1,
            "snapshot_count": 1,
            "access_completeness": completeness,
            "error_category": None,
            "error_message": None,
            "run_metadata": {"fixture_case": key, "seeded": True},
        },
    )


def _ensure_snapshot(
    db: Session,
    *,
    organization_id: str,
    source: Source,
    project: Project,
    run: MonitoringRun,
    key: str,
    requested_url: str,
    fetched_at: datetime,
    fields: tuple[FieldSeed, ...],
    completeness: str = "complete",
    validation_status: str = "valid",
    validation_warnings: tuple[str, ...] = (),
) -> tuple[SourceSnapshot, dict[str, SnapshotField]]:
    deduplication_key = f"demo:snapshot:{key}"
    snapshot = db.scalar(
        select(SourceSnapshot).where(
            SourceSnapshot.organization_id == organization_id,
            SourceSnapshot.deduplication_key == deduplication_key,
        )
    )
    content_hash = _sha256(f"projecttruth-frozen-fixture:{key}")
    if snapshot is None:
        facts = {field.key: _json_value(field.value) for field in fields}
        snapshot = SourceSnapshot(
            organization_id=organization_id,
            source_id=source.id,
            project_id=project.id,
            monitoring_run_id=run.id,
            entity_type="project",
            entity_id=project.id,
            fetch_status="partial" if completeness == "partial" else "success",
            requested_url=requested_url,
            final_url=requested_url,
            http_status=200,
            fetched_at=fetched_at,
            source_date=fetched_at,
            content_type="text/html",
            content_hash=content_hash,
            parser_name="fixture_html",
            parser_version="1.0.0",
            access_completeness=completeness,
            structured_facts=facts,
            evidence_references=[
                {
                    "safe_reference": requested_url,
                    "content_hash": content_hash,
                    "excerpt": f"Frozen synthetic fixture {key}",
                }
            ],
            validation_status=validation_status,
            validation_warnings=list(validation_warnings),
            raw_payload_storage_key=None,
            raw_payload_hash=content_hash,
            response_metadata={"fixture_case": key, "synthetic": True},
            latency_ms=8,
            deduplication_key=deduplication_key,
        )
        db.add(snapshot)
        db.flush()

    field_rows: dict[str, SnapshotField] = {}
    for field in fields:
        existing = db.scalar(
            select(SnapshotField).where(
                SnapshotField.snapshot_id == snapshot.id,
                SnapshotField.field_key == field.key,
                SnapshotField.occurrence_index == 0,
            )
        )
        if existing is None:
            number_value = (
                float(field.value)
                if isinstance(field.value, (int, float)) and not isinstance(field.value, bool)
                else None
            )
            date_value = (
                field.value
                if isinstance(field.value, date) and not isinstance(field.value, datetime)
                else None
            )
            existing = SnapshotField(
                organization_id=organization_id,
                snapshot_id=snapshot.id,
                field_key=field.key,
                occurrence_index=0,
                raw_label=field.raw_label,
                raw_value=field.raw_value,
                canonical_value=_text_value(field.value),
                normalized_text=_text_value(field.value),
                normalized_number=number_value,
                normalized_date=date_value,
                value_type=field.value_type,
                unit=field.unit,
                selector=field.selector or f'[data-field="{field.key}"]',
                page_number=field.page_number,
                coordinates=None,
                extraction_method=field.extraction_method,
                extraction_confidence=field.extraction_confidence,
                parser_name="fixture_html",
                parser_version="1.0.0",
                validation_warnings=list(field.validation_warnings),
            )
            db.add(existing)
            db.flush()
        field_rows[field.key] = existing
    return snapshot, field_rows


def _ensure_change(
    db: Session,
    *,
    organization_id: str,
    project: Project,
    source: Source,
    old_snapshot: SourceSnapshot,
    new_snapshot: SourceSnapshot,
    key: str,
    field_key: str,
    old_raw_value: str | None,
    old_value: str | None,
    new_raw_value: str | None,
    new_value: str | None,
    change_type: str,
    detected_at: datetime,
    magnitude_days: int | None = None,
    confidence: float = 1.0,
    reason_codes: list[str] | None = None,
    review_state: str = "pending",
) -> DetectedChange:
    return _upsert(
        db,
        DetectedChange,
        lookup={
            "organization_id": organization_id,
            "deduplication_key": f"demo:change:{key}",
        },
        values={
            "project_id": project.id,
            "source_id": source.id,
            "old_snapshot_id": old_snapshot.id,
            "new_snapshot_id": new_snapshot.id,
            "field_key": field_key,
            "old_raw_value": old_raw_value,
            "old_canonical_value": old_value,
            "new_raw_value": new_raw_value,
            "new_canonical_value": new_value,
            "change_type": change_type,
            "magnitude": magnitude_days,
            "magnitude_unit": "days" if magnitude_days is not None else None,
            "magnitude_days": magnitude_days,
            "source_reliability": 1.0,
            "source_access_completeness": "complete",
            "identity_confidence": 1.0,
            "extraction_confidence": confidence,
            "change_confidence": confidence,
            "confidence_dimensions": {
                "source_reliability": "high",
                "source_access_completeness": "complete",
                "identity_confidence": "confirmed",
                "extraction_confidence": confidence,
                "change_confidence": confidence,
                "conflict_state": "none",
            },
            "severity": "high" if magnitude_days is not None else "medium",
            "reason_codes": reason_codes or [change_type],
            "requires_review": True,
            "review_state": review_state,
            "parser_compatible": True,
            "detected_at": detected_at,
        },
    )


def _ensure_evidence(
    db: Session,
    *,
    organization_id: str,
    change: DetectedChange,
    snapshot: SourceSnapshot,
    snapshot_field: SnapshotField | None,
    side: str,
    source: Source,
    excerpt: str,
) -> ChangeEvidence:
    return _upsert(
        db,
        ChangeEvidence,
        lookup={
            "detected_change_id": change.id,
            "snapshot_id": snapshot.id,
            "evidence_side": side,
            "evidence_type": "snapshot_field",
        },
        values={
            "organization_id": organization_id,
            "snapshot_field_id": snapshot_field.id if snapshot_field else None,
            "source_class": source.source_class,
            "source_url": snapshot.final_url,
            "evidence_reference": f"snapshot:{snapshot.id}",
            "excerpt": excerpt,
            "selector": snapshot_field.selector if snapshot_field else None,
            "page_number": snapshot_field.page_number if snapshot_field else None,
            "coordinates": snapshot_field.coordinates if snapshot_field else None,
            "content_hash": snapshot.content_hash,
            "extraction_method": "deterministic_diff",
            "evidence_metadata": {
                "field_key": change.field_key,
                "synthetic_fixture": True,
            },
        },
    )


def _ensure_signal(
    db: Session,
    *,
    organization_id: str,
    project: Project,
    key: str,
    rule_code: str,
    observed_facts: dict[str, Any],
    severity: str,
    confidence: float,
    required_review_level: str,
    change: DetectedChange | None = None,
    detected_at: datetime | None = None,
    limitations: list[str] | None = None,
) -> Signal:
    supporting = [change.id] if change else []
    return _upsert(
        db,
        Signal,
        lookup={
            "organization_id": organization_id,
            "deduplication_key": f"demo:signal:{key}",
        },
        values={
            "project_id": project.id,
            "primary_detected_change_id": change.id if change else None,
            "rule_code": rule_code,
            "rule_version": RULE_VERSION,
            "supporting_change_ids": supporting,
            "observed_facts": _json_value(observed_facts),
            "severity": severity,
            "confidence": confidence,
            "confidence_dimensions": {
                "identity_confidence": "confirmed",
                "change_confidence": confidence,
                "conflict_state": "none" if confidence >= 0.8 else "needs_review",
            },
            "limitations": limitations or [],
            "required_review_level": required_review_level,
            "state": "active",
            "detected_at": change.detected_at if change else detected_at or _utc(2026, 5, 11, 11),
        },
    )


def _ensure_alert(
    db: Session,
    *,
    organization_id: str,
    project: Project,
    source: Source | None,
    signal: Signal,
    key: str,
    title: str,
    description: str,
    severity: str,
    old_value: str | None = None,
    new_value: str | None = None,
    magnitude: str | None = None,
    state: str = "open",
    change: DetectedChange | None = None,
    limitation: str | None = None,
) -> Alert:
    return _upsert(
        db,
        Alert,
        lookup={
            "organization_id": organization_id,
            "deduplication_key": f"demo:alert:{key}",
        },
        values={
            "project_id": project.id,
            "signal_id": signal.id,
            "detected_change_id": change.id if change else None,
            "source_id": source.id if source else None,
            "title": title,
            "neutral_description": description,
            "old_value": old_value,
            "new_value": new_value,
            "magnitude": magnitude,
            "state": state,
            "severity": severity,
            "required_review_level": "reviewer",
            "requires_review": True,
            "source_class": source.source_class if source else None,
            "freshness": "frozen_fixture_2026",
            "limitation": limitation,
            "suggested_review_action": (
                "Review the linked source evidence and keep the item unresolved until verified."
            ),
            "first_seen_at": signal.detected_at,
            "last_seen_at": signal.detected_at,
            "resolved_at": None,
        },
    )


def _seed_case_a(
    db: Session,
    *,
    organization_id: str,
    analyst_user_id: str,
    watchlist: Watchlist,
    project: Project,
    source: Source,
) -> tuple[DetectedChange, Signal, Alert]:
    job = _ensure_job(
        db,
        organization_id=organization_id,
        source=source,
        project=project,
        watchlist=watchlist,
        cadence="weekly",
    )
    old_at = _utc(2026, 1, 5, 9)
    old_run = _ensure_run(
        db,
        organization_id=organization_id,
        job=job,
        source=source,
        project=project,
        analyst_user_id=analyst_user_id,
        key="case-a-old",
        fetched_at=old_at,
    )
    old_snapshot, old_fields = _ensure_snapshot(
        db,
        organization_id=organization_id,
        source=source,
        project=project,
        run=old_run,
        key="case-a-old",
        requested_url="fixture://case_a_completion_old",
        fetched_at=old_at,
        fields=(
            FieldSeed("project_name", "Project Name", project.name, project.name),
            FieldSeed(
                "registration_number",
                "Registration Number",
                project.registration_number,
                project.registration_number,
            ),
            FieldSeed(
                "proposed_completion_date",
                "Proposed Completion Date",
                "30 June 2027",
                date(2027, 6, 30),
                value_type="date",
            ),
            FieldSeed("current_status", "Current Status", "Registered", "Registered"),
            FieldSeed("phase", "Phase", "Phase II", "Phase II"),
        ),
    )

    new_at = _utc(2026, 2, 2, 9)
    new_run = _ensure_run(
        db,
        organization_id=organization_id,
        job=job,
        source=source,
        project=project,
        analyst_user_id=analyst_user_id,
        key="case-a-new",
        fetched_at=new_at,
    )
    new_snapshot, new_fields = _ensure_snapshot(
        db,
        organization_id=organization_id,
        source=source,
        project=project,
        run=new_run,
        key="case-a-new",
        requested_url="fixture://case_a_completion_extended",
        fetched_at=new_at,
        fields=(
            FieldSeed("project_name", "Project Name", project.name, project.name),
            FieldSeed(
                "registration_number",
                "Registration Number",
                project.registration_number,
                project.registration_number,
            ),
            FieldSeed(
                "proposed_completion_date",
                "Revised Completion Date",
                "31 March 2028",
                date(2028, 3, 31),
                value_type="date",
            ),
            FieldSeed(
                "revised_completion_date",
                "Revised Completion Date",
                "31 March 2028",
                date(2028, 3, 31),
                value_type="date",
            ),
            FieldSeed("current_status", "Current Status", "Registered", "Registered"),
            FieldSeed("phase", "Phase", "Phase II", "Phase II"),
            FieldSeed(
                "official_notices",
                "Official Notice",
                "Completion date revision record",
                "https://fixtures.projecttruth.invalid/notices/HRERA-DEMO-101-extension",
            ),
        ),
    )
    project.last_verified_at = new_at
    job.last_run_at = new_at
    change = _ensure_change(
        db,
        organization_id=organization_id,
        project=project,
        source=source,
        old_snapshot=old_snapshot,
        new_snapshot=new_snapshot,
        key="case-a-completion-extended",
        field_key="proposed_completion_date",
        old_raw_value="30 June 2027",
        old_value="2027-06-30",
        new_raw_value="31 March 2028",
        new_value="2028-03-31",
        change_type="date_extended",
        magnitude_days=275,
        confidence=1.0,
        reason_codes=["date_extended_later", "frozen_official_fixture"],
        detected_at=new_at,
    )
    _ensure_evidence(
        db,
        organization_id=organization_id,
        change=change,
        snapshot=old_snapshot,
        snapshot_field=old_fields["proposed_completion_date"],
        side="old",
        source=source,
        excerpt="Proposed Completion Date: 30 June 2027",
    )
    _ensure_evidence(
        db,
        organization_id=organization_id,
        change=change,
        snapshot=new_snapshot,
        snapshot_field=new_fields["proposed_completion_date"],
        side="new",
        source=source,
        excerpt="Revised Completion Date: 31 March 2028",
    )
    signal = _ensure_signal(
        db,
        organization_id=organization_id,
        project=project,
        key="case-a-completion-extended",
        rule_code="COMPLETION_DATE_EXTENDED",
        observed_facts={
            "field_key": "proposed_completion_date",
            "old_value": "2027-06-30",
            "new_value": "2028-03-31",
            "magnitude": 275,
            "reason_codes": ["date_extended_later"],
        },
        severity="high",
        confidence=1.0,
        required_review_level="reviewer",
        change=change,
    )
    alert = _ensure_alert(
        db,
        organization_id=organization_id,
        project=project,
        source=source,
        signal=signal,
        key="case-a-completion-extended",
        title="Completion date changed",
        description="The captured official completion date moved later by 275 days.",
        severity="high",
        old_value="2027-06-30",
        new_value="2028-03-31",
        magnitude="+275 days",
        change=change,
        limitation="Requires reviewer confirmation before client-facing use.",
    )
    return change, signal, alert


def _seed_case_b_document(
    db: Session,
    *,
    organization_id: str,
    project: Project,
    source: Source,
    analyst_user_id: str,
) -> tuple[Document, DocumentVersion, DocumentExtraction]:
    content = (
        "Six residential towers; 1,200 thoughtfully planned homes; "
        "possession expected by December 2027."
    )
    content_hash = _sha256(content)
    document = _upsert(
        db,
        Document,
        lookup={
            "organization_id": organization_id,
            "project_id": project.id,
            "title": "Lakeview Residences synthetic authorized brochure",
        },
        values={
            "source_id": source.id,
            "document_type": "brochure",
            "original_filename": "lakeview-residences-demo-brochure.pdf",
            "sanitized_filename": "lakeview-residences-demo-brochure.pdf",
            "mime_type": "application/pdf",
            "authorization_attested": True,
            "rights_attestation_text": "Synthetic fixture authorized for local demonstration.",
            "attested_by_user_id": analyst_user_id,
            "created_by_user_id": analyst_user_id,
            "status": "extracted",
            "storage_key": "fixture://text/case_b_brochure_claims.txt",
            "sha256": content_hash,
            "size_bytes": len(content.encode("utf-8")),
            "deleted_at": None,
        },
    )
    version = _upsert(
        db,
        DocumentVersion,
        lookup={"document_id": document.id, "version_number": 1},
        values={
            "organization_id": organization_id,
            "content_hash": content_hash,
            "file_size_bytes": len(content.encode("utf-8")),
            "storage_key": "fixture://text/case_b_brochure_claims.txt",
            "source_url": "fixture://case_b_brochure_claims",
            "uploaded_by_user_id": analyst_user_id,
            "captured_at": _utc(2026, 2, 15),
            "is_encrypted": False,
            "malware_scan_status": "synthetic_fixture",
        },
    )
    extraction = _upsert(
        db,
        DocumentExtraction,
        lookup={
            "organization_id": organization_id,
            "deduplication_key": "demo:extraction:case-b-brochure-v1",
        },
        values={
            "document_version_id": version.id,
            "provider_name": "projecttruth_fixture_text",
            "provider_version": "1.0.0",
            "extraction_method": "fixture_text",
            "status": "completed",
            "text_content": content,
            "page_count": 12,
            "confidence": 1.0,
            "warnings": [],
            "extraction_metadata": {"synthetic_fixture": True},
            "requires_review": True,
            "started_at": _utc(2026, 2, 15, 9),
            "completed_at": _utc(2026, 2, 15, 9, 1),
        },
    )
    return document, version, extraction


def _seed_case_b(
    db: Session,
    *,
    organization_id: str,
    analyst_user_id: str,
    watchlist: Watchlist,
    project: Project,
    official_source: Source,
    brochure_source: Source,
) -> tuple[list[SourceConflict], list[Signal], list[Alert]]:
    job = _ensure_job(
        db,
        organization_id=organization_id,
        source=official_source,
        project=project,
        watchlist=watchlist,
        cadence="daily",
    )
    captured_at = _utc(2026, 3, 3, 10, 15)
    run = _ensure_run(
        db,
        organization_id=organization_id,
        job=job,
        source=official_source,
        project=project,
        analyst_user_id=analyst_user_id,
        key="case-b-official",
        fetched_at=captured_at,
    )
    snapshot, official_fields = _ensure_snapshot(
        db,
        organization_id=organization_id,
        source=official_source,
        project=project,
        run=run,
        key="case-b-official",
        requested_url="fixture://case_b_official_record",
        fetched_at=captured_at,
        fields=(
            FieldSeed("project_name", "Project Name", project.name, project.name),
            FieldSeed(
                "registration_number",
                "Registration Number",
                project.registration_number,
                project.registration_number,
            ),
            FieldSeed(
                "building_count", "Number of Towers", "5", 5, value_type="number", unit="towers"
            ),
            FieldSeed(
                "unit_count", "Number of Units", "1080", 1080, value_type="number", unit="units"
            ),
            FieldSeed(
                "proposed_completion_date",
                "Proposed Completion Date",
                "30 June 2028",
                date(2028, 6, 30),
                value_type="date",
            ),
            FieldSeed("current_status", "Current Status", "Registered", "Registered"),
        ),
    )
    _, version, extraction = _seed_case_b_document(
        db,
        organization_id=organization_id,
        project=project,
        source=brochure_source,
        analyst_user_id=analyst_user_id,
    )
    claim_definitions = (
        (
            "building_count",
            "6",
            "6",
            "number",
            "towers",
            4,
            "Six residential towers arranged around a landscaped central court.",
            "5",
            "one additional tower",
        ),
        (
            "unit_count",
            "1,200",
            "1200",
            "number",
            "units",
            7,
            "1,200 thoughtfully planned homes.",
            "1080",
            "120 additional units",
        ),
        (
            "proposed_completion_date",
            "December 2027",
            "2027-12-31",
            "date",
            None,
            12,
            "Possession expected by December 2027.",
            "2028-06-30",
            "marketing date precedes official completion",
        ),
    )
    conflicts: list[SourceConflict] = []
    signals: list[Signal] = []
    alerts: list[Alert] = []
    for (
        field_key,
        raw_value,
        normalized_value,
        value_type,
        unit,
        page_number,
        evidence_text,
        official_value,
        magnitude,
    ) in claim_definitions:
        claim = db.scalar(
            select(DocumentClaim).where(
                DocumentClaim.organization_id == organization_id,
                DocumentClaim.document_extraction_id == extraction.id,
                DocumentClaim.field_key == field_key,
                DocumentClaim.page_number == page_number,
            )
        )
        if claim is None:
            claim = DocumentClaim(
                organization_id=organization_id,
                document_extraction_id=extraction.id,
                document_version_id=version.id,
                project_id=project.id,
                official_snapshot_field_id=official_fields[field_key].id,
                field_key=field_key,
                raw_value=raw_value,
                normalized_value=normalized_value,
                value_type=value_type,
                unit=unit,
                page_number=page_number,
                evidence_text=evidence_text,
                coordinates={"paragraph": 2 if page_number == 4 else 1 if page_number == 7 else 3},
                extraction_method="fixture_text",
                extraction_confidence=1.0,
                comparison_state="conflicting",
                review_state="pending",
            )
            db.add(claim)
            db.flush()
        else:
            claim.official_snapshot_field_id = official_fields[field_key].id
            claim.comparison_state = "conflicting"
            claim.review_state = "pending"
            db.flush()

        conflict = _upsert(
            db,
            SourceConflict,
            lookup={
                "organization_id": organization_id,
                "deduplication_key": f"demo:conflict:case-b:{field_key}",
            },
            values={
                "project_id": project.id,
                "field_key": field_key,
                "source_a_id": official_source.id,
                "source_b_id": brochure_source.id,
                "source_a_class": "official_regulator",
                "source_b_class": "marketing_brochure",
                "source_a_value": official_value,
                "source_b_value": normalized_value,
                "source_a_reference": {
                    "snapshot_id": snapshot.id,
                    "snapshot_field_id": official_fields[field_key].id,
                    "url": snapshot.final_url,
                },
                "source_b_reference": {
                    "document_version_id": version.id,
                    "claim_id": claim.id,
                    "page_number": page_number,
                },
                "source_a_freshness_at": snapshot.fetched_at,
                "source_b_freshness_at": version.captured_at,
                "comparison_state": "conflicting",
                "is_material": True,
                "state": "open",
                "detected_at": captured_at,
                "resolved_at": None,
                "resolved_by_user_id": None,
                "resolution_reason": None,
                "selected_value": None,
            },
        )
        signal = _ensure_signal(
            db,
            organization_id=organization_id,
            project=project,
            key=f"case-b-{field_key}",
            rule_code="OFFICIAL_VS_MARKETING_CONFLICT",
            observed_facts={
                "field_key": field_key,
                "official_value": official_value,
                "marketing_value": normalized_value,
                "document_claim_id": claim.id,
                "page_number": page_number,
            },
            severity="high" if field_key == "proposed_completion_date" else "medium",
            confidence=1.0,
            required_review_level="reviewer",
            detected_at=captured_at,
            limitations=["A marketing claim cannot overwrite the official captured fact."],
        )
        alert = _ensure_alert(
            db,
            organization_id=organization_id,
            project=project,
            source=brochure_source,
            signal=signal,
            key=f"case-b-{field_key}",
            title=f"Brochure claim conflicts: {field_key.replace('_', ' ')}",
            description="The page-linked brochure claim differs from the official fixture.",
            severity="high" if field_key == "proposed_completion_date" else "medium",
            old_value=official_value,
            new_value=normalized_value,
            magnitude=magnitude,
            limitation="Official facts and marketing claims remain separately preserved.",
        )
        conflicts.append(conflict)
        signals.append(signal)
        alerts.append(alert)
    return conflicts, signals, alerts


def _seed_case_c(
    db: Session,
    *,
    organization_id: str,
    analyst_user_id: str,
    watchlist: Watchlist,
    project: Project,
    source: Source,
) -> tuple[DetectedChange, Signal, Alert]:
    job = _ensure_job(
        db,
        organization_id=organization_id,
        source=source,
        project=project,
        watchlist=watchlist,
        cadence="manual",
    )
    old_at = _utc(2026, 4, 3, 8, 30)
    old_run = _ensure_run(
        db,
        organization_id=organization_id,
        job=job,
        source=source,
        project=project,
        analyst_user_id=analyst_user_id,
        key="case-c-before-phase-iii",
        fetched_at=old_at,
    )
    old_snapshot, old_fields = _ensure_snapshot(
        db,
        organization_id=organization_id,
        source=source,
        project=project,
        run=old_run,
        key="case-c-before-phase-iii",
        requested_url="fixture://case_c_multiple_phases",
        fetched_at=old_at,
        fields=(
            FieldSeed("project_name", "Project Name", project.name, project.name),
            FieldSeed("registration_number", "Registration Number", None, None),
            FieldSeed("phase", "Phase", "Phase III", "Phase III"),
            FieldSeed("locality_or_sector", "Sector", "Sector 79", "Sector 79"),
        ),
    )
    new_at = _utc(2026, 4, 7, 8, 30)
    new_run = _ensure_run(
        db,
        organization_id=organization_id,
        job=job,
        source=source,
        project=project,
        analyst_user_id=analyst_user_id,
        key="case-c-phase-iii",
        fetched_at=new_at,
    )
    new_snapshot, new_fields = _ensure_snapshot(
        db,
        organization_id=organization_id,
        source=source,
        project=project,
        run=new_run,
        key="case-c-phase-iii",
        requested_url="fixture://case_c_new_phase",
        fetched_at=new_at,
        fields=(
            FieldSeed("project_name", "Project Name", project.name, project.name),
            FieldSeed(
                "registration_number",
                "Registration Number",
                project.registration_number,
                project.registration_number,
            ),
            FieldSeed("phase", "Phase", "Phase III", "Phase III"),
            FieldSeed("locality_or_sector", "Sector", "Sector 79", "Sector 79"),
            FieldSeed("parent_project_name", "Parent Project", "Orion Greens", "Orion Greens"),
            FieldSeed(
                "registration_date",
                "Registration Date",
                "01 April 2026",
                date(2026, 4, 1),
                value_type="date",
            ),
        ),
    )
    project.tracking_started_at = old_at
    project.last_verified_at = new_at
    job.last_run_at = new_at
    change = _ensure_change(
        db,
        organization_id=organization_id,
        project=project,
        source=source,
        old_snapshot=old_snapshot,
        new_snapshot=new_snapshot,
        key="case-c-new-registration",
        field_key="registration_number",
        old_raw_value=None,
        old_value=None,
        new_raw_value=project.registration_number,
        new_value=project.registration_number,
        change_type="value_added",
        detected_at=new_at,
        confidence=1.0,
        reason_codes=["new_registration", "parent_phase_linked", "sector_79"],
    )
    _ensure_evidence(
        db,
        organization_id=organization_id,
        change=change,
        snapshot=old_snapshot,
        snapshot_field=old_fields["registration_number"],
        side="old",
        source=source,
        excerpt="Phase III registration not present in the prior captured listing.",
    )
    _ensure_evidence(
        db,
        organization_id=organization_id,
        change=change,
        snapshot=new_snapshot,
        snapshot_field=new_fields["registration_number"],
        side="new",
        source=source,
        excerpt=f"Registration Number: {project.registration_number}",
    )
    signal = _ensure_signal(
        db,
        organization_id=organization_id,
        project=project,
        key="case-c-new-registration",
        rule_code="NEW_PHASE_OR_REGISTRATION",
        observed_facts={
            "registration_number": project.registration_number,
            "phase": "Phase III",
            "parent_project_id": project.parent_project_id,
            "locality_or_sector": "Sector 79",
        },
        severity="medium",
        confidence=1.0,
        required_review_level="reviewer",
        change=change,
        limitations=[DATASET_LIMITATION],
    )
    alert = _ensure_alert(
        db,
        organization_id=organization_id,
        project=project,
        source=source,
        signal=signal,
        key="case-c-new-registration",
        title="New phase or registration observed",
        description="Orion Greens Phase III was linked to its reviewed parent in Sector 79.",
        severity="medium",
        new_value=project.registration_number,
        change=change,
        limitation=DATASET_LIMITATION,
    )
    return change, signal, alert


def _seed_case_d_document(
    db: Session,
    *,
    organization_id: str,
    project: Project,
    source: Source,
    analyst_user_id: str,
    official_field: SnapshotField,
) -> tuple[DocumentVersion, DocumentClaim]:
    content = 'OCR text: "Proposed completion 31 December 2029"; confidence 0.43.'
    content_hash = _sha256(content)
    document = _upsert(
        db,
        Document,
        lookup={
            "organization_id": organization_id,
            "project_id": project.id,
            "title": "Metro Square low-confidence OCR fixture",
        },
        values={
            "source_id": source.id,
            "document_type": "brochure",
            "original_filename": "metro-square-demo-scan.pdf",
            "sanitized_filename": "metro-square-demo-scan.pdf",
            "mime_type": "application/pdf",
            "authorization_attested": True,
            "rights_attestation_text": "Synthetic fixture authorized for local demonstration.",
            "attested_by_user_id": analyst_user_id,
            "created_by_user_id": analyst_user_id,
            "status": "needs_review",
            "storage_key": "fixture://text/case_d_low_confidence_ocr.txt",
            "sha256": content_hash,
            "size_bytes": len(content.encode("utf-8")),
            "deleted_at": None,
        },
    )
    version = _upsert(
        db,
        DocumentVersion,
        lookup={"document_id": document.id, "version_number": 1},
        values={
            "organization_id": organization_id,
            "content_hash": content_hash,
            "file_size_bytes": len(content.encode("utf-8")),
            "storage_key": "fixture://text/case_d_low_confidence_ocr.txt",
            "source_url": "fixture://case_d_low_confidence_ocr",
            "uploaded_by_user_id": analyst_user_id,
            "captured_at": _utc(2026, 5, 11, 11, 5),
            "is_encrypted": False,
            "malware_scan_status": "synthetic_fixture",
        },
    )
    extraction = _upsert(
        db,
        DocumentExtraction,
        lookup={
            "organization_id": organization_id,
            "deduplication_key": "demo:extraction:case-d-ocr-v1",
        },
        values={
            "document_version_id": version.id,
            "provider_name": "projecttruth_fixture_ocr",
            "provider_version": "1.0.0",
            "extraction_method": "ocr_fixture",
            "status": "needs_review",
            "text_content": content,
            "page_count": 3,
            "confidence": 0.43,
            "warnings": ["low_image_quality", "low_confidence_core_field"],
            "extraction_metadata": {
                "synthetic_fixture": True,
                "image_quality": "low",
            },
            "requires_review": True,
            "started_at": _utc(2026, 5, 11, 11, 5),
            "completed_at": _utc(2026, 5, 11, 11, 6),
        },
    )
    claim = db.scalar(
        select(DocumentClaim).where(
            DocumentClaim.organization_id == organization_id,
            DocumentClaim.document_extraction_id == extraction.id,
            DocumentClaim.field_key == "proposed_completion_date",
            DocumentClaim.page_number == 3,
        )
    )
    if claim is None:
        claim = DocumentClaim(
            organization_id=organization_id,
            document_extraction_id=extraction.id,
            document_version_id=version.id,
            project_id=project.id,
            official_snapshot_field_id=official_field.id,
            field_key="proposed_completion_date",
            raw_value="31 December 2029",
            normalized_value="2029-12-31",
            value_type="date",
            page_number=3,
            evidence_text='OCR text: "Proposed completion 31 December 2029"',
            coordinates={"page": 3, "region": "candidate_date"},
            extraction_method="ocr_fixture",
            extraction_confidence=0.43,
            comparison_state="needs_review",
            review_state="unresolved",
        )
        db.add(claim)
        db.flush()
    else:
        claim.official_snapshot_field_id = official_field.id
        claim.comparison_state = "needs_review"
        claim.review_state = "unresolved"
        db.flush()
    return version, claim


def _seed_case_d(
    db: Session,
    *,
    organization_id: str,
    analyst_user_id: str,
    watchlist: Watchlist,
    project: Project,
    official_source: Source,
    brochure_source: Source,
    developer_source: Source,
) -> tuple[list[SourceConflict], Signal, Alert]:
    for linked_source in (developer_source, brochure_source):
        _upsert(
            db,
            ProjectSourceLink,
            lookup={
                "organization_id": organization_id,
                "project_id": project.id,
                "source_id": linked_source.id,
            },
            values={"source_role": "supporting", "is_active": True},
        )
    official_job = _ensure_job(
        db,
        organization_id=organization_id,
        source=official_source,
        project=project,
        watchlist=watchlist,
        cadence="weekly",
    )
    official_at = _utc(2026, 5, 11, 11)
    official_run = _ensure_run(
        db,
        organization_id=organization_id,
        job=official_job,
        source=official_source,
        project=project,
        analyst_user_id=analyst_user_id,
        key="case-d-official-missing",
        fetched_at=official_at,
        completeness="partial",
    )
    official_snapshot, official_fields = _ensure_snapshot(
        db,
        organization_id=organization_id,
        source=official_source,
        project=project,
        run=official_run,
        key="case-d-official-missing",
        requested_url="fixture://case_d_official_missing_completion",
        fetched_at=official_at,
        completeness="partial",
        validation_status="partial",
        validation_warnings=("completion_date_value_missing",),
        fields=(
            FieldSeed("project_name", "Project Name", project.name, project.name),
            FieldSeed(
                "registration_number",
                "Registration Number",
                project.registration_number,
                project.registration_number,
            ),
            FieldSeed(
                "proposed_completion_date",
                "Proposed Completion Date",
                "",
                None,
                value_type="date",
                extraction_confidence=1.0,
                validation_warnings=("missing_value_preserved_as_null",),
            ),
            FieldSeed("current_status", "Current Status", "Registered", "Registered"),
        ),
    )

    developer_at = _utc(2026, 5, 10, 7)
    developer_job = _ensure_job(
        db,
        organization_id=organization_id,
        source=developer_source,
        project=project,
        watchlist=watchlist,
        cadence="weekly",
    )
    developer_run = _ensure_run(
        db,
        organization_id=organization_id,
        job=developer_job,
        source=developer_source,
        project=project,
        analyst_user_id=analyst_user_id,
        key="case-d-developer",
        fetched_at=developer_at,
    )
    developer_snapshot, developer_fields = _ensure_snapshot(
        db,
        organization_id=organization_id,
        source=developer_source,
        project=project,
        run=developer_run,
        key="case-d-developer",
        requested_url="fixture://case_d_developer_claim",
        fetched_at=developer_at,
        fields=(
            FieldSeed("project_name", "Project Name", project.name, project.name),
            FieldSeed(
                "registration_number",
                "Project Reference",
                project.registration_number,
                project.registration_number,
            ),
            FieldSeed(
                "possession_date",
                "Anticipated Possession",
                "June 2029",
                date(2029, 6, 30),
                value_type="date",
                extraction_method="fixture_developer_html",
                extraction_confidence=0.9,
            ),
        ),
    )
    ocr_version, ocr_claim = _seed_case_d_document(
        db,
        organization_id=organization_id,
        project=project,
        source=brochure_source,
        analyst_user_id=analyst_user_id,
        official_field=official_fields["proposed_completion_date"],
    )
    project.tracking_started_at = developer_at
    project.last_verified_at = official_at
    official_job.last_run_at = official_at
    official_source.last_successful_fetch_at = official_at
    official_source.last_content_hash = official_snapshot.content_hash
    developer_source.last_successful_fetch_at = developer_at
    developer_source.last_content_hash = developer_snapshot.content_hash
    brochure_source.last_successful_fetch_at = ocr_version.captured_at
    brochure_source.last_content_hash = ocr_version.content_hash

    conflict_definitions = (
        (
            "official-vs-ocr",
            official_source,
            brochure_source,
            "official_regulator",
            "marketing_brochure",
            None,
            "2029-12-31",
            {
                "snapshot_id": official_snapshot.id,
                "snapshot_field_id": official_fields["proposed_completion_date"].id,
                "value_state": "missing",
            },
            {
                "document_version_id": ocr_version.id,
                "claim_id": ocr_claim.id,
                "page_number": 3,
                "confidence": 0.43,
            },
            "needs_review",
        ),
        (
            "official-vs-developer",
            official_source,
            developer_source,
            "official_regulator",
            "official_developer",
            None,
            "2029-06-30",
            {
                "snapshot_id": official_snapshot.id,
                "snapshot_field_id": official_fields["proposed_completion_date"].id,
                "value_state": "missing",
            },
            {
                "snapshot_id": developer_snapshot.id,
                "snapshot_field_id": developer_fields["possession_date"].id,
            },
            "needs_review",
        ),
        (
            "developer-vs-ocr",
            developer_source,
            brochure_source,
            "official_developer",
            "marketing_brochure",
            "2029-06-30",
            "2029-12-31",
            {
                "snapshot_id": developer_snapshot.id,
                "snapshot_field_id": developer_fields["possession_date"].id,
            },
            {
                "document_version_id": ocr_version.id,
                "claim_id": ocr_claim.id,
                "page_number": 3,
                "confidence": 0.43,
            },
            "conflicting",
        ),
    )
    conflicts: list[SourceConflict] = []
    for (
        key,
        source_a,
        source_b,
        source_a_class,
        source_b_class,
        source_a_value,
        source_b_value,
        source_a_reference,
        source_b_reference,
        comparison_state,
    ) in conflict_definitions:
        conflict = _upsert(
            db,
            SourceConflict,
            lookup={
                "organization_id": organization_id,
                "deduplication_key": f"demo:conflict:case-d:{key}",
            },
            values={
                "project_id": project.id,
                "field_key": "proposed_completion_date",
                "source_a_id": source_a.id,
                "source_b_id": source_b.id,
                "source_a_class": source_a_class,
                "source_b_class": source_b_class,
                "source_a_value": source_a_value,
                "source_b_value": source_b_value,
                "source_a_reference": source_a_reference,
                "source_b_reference": source_b_reference,
                "source_a_freshness_at": official_at,
                "source_b_freshness_at": official_at,
                "comparison_state": comparison_state,
                "is_material": True,
                "state": "open",
                "detected_at": official_at + timedelta(minutes={"developer-vs-ocr": 0, "official-vs-developer": 1, "official-vs-ocr": 2}[key]),
                "resolved_at": None,
                "resolved_by_user_id": None,
                "resolution_reason": None,
                "selected_value": None,
            },
        )
        conflicts.append(conflict)

    signal = _ensure_signal(
        db,
        organization_id=organization_id,
        project=project,
        key="case-d-needs-review",
        rule_code="OFFICIAL_VS_DEVELOPER_CONFLICT",
        observed_facts={
            "official_value": None,
            "ocr_candidate": "2029-12-31",
            "ocr_confidence": 0.43,
            "developer_claim": "2029-06-30",
            "conflict_ids": [conflict.id for conflict in conflicts],
        },
        severity="medium",
        confidence=0.43,
        required_review_level="reviewer",
        detected_at=official_at,
        limitations=[
            "The official completion value is missing.",
            "The OCR candidate is low confidence and is not an official fact.",
            "No confirmed external alert may be emitted without review.",
        ],
    )
    alert = _ensure_alert(
        db,
        organization_id=organization_id,
        project=project,
        source=brochure_source,
        signal=signal,
        key="case-d-needs-review",
        title="Completion claims need review",
        description=(
            "Official completion is missing; a low-confidence OCR candidate and a "
            "developer claim remain unresolved."
        ),
        severity="medium",
        old_value="2029-06-30",
        new_value="2029-12-31",
        state="unresolved",
        limitation="Internal review item only; no confirmed external alert was emitted.",
    )
    return conflicts, signal, alert


def _seed_market_snapshot(
    db: Session,
    *,
    organization_id: str,
    sector_79: Location,
) -> MarketSnapshot:
    return _upsert(
        db,
        MarketSnapshot,
        lookup={
            "organization_id": organization_id,
            "deduplication_key": "demo:market:sector-79:2026-04",
        },
        values={
            "location_id": sector_79.id,
            "period_start": date(2026, 4, 1),
            "period_end": date(2026, 4, 30),
            "projects_monitored": 2,
            "active_projects": 2,
            "lapsed_projects": 0,
            "qpr_flagged_projects": 0,
            "recent_reviewed_completion_changes": 0,
            "new_registrations_or_phases": 1,
            "developer_count": 2,
            "source_health_summary": {
                "official_fixture": "healthy",
                "locality_or_sector": "Sector 79",
                "new_phase_increment": 1,
            },
            "basis_note": DATASET_LIMITATION,
            "generated_at": _utc(2026, 4, 30, 23, 59),
        },
    )


def _seed_report(
    db: Session,
    *,
    organization_id: str,
    analyst_user_id: str,
    case_a: tuple[DetectedChange, Signal, Alert],
    case_b: tuple[list[SourceConflict], list[Signal], list[Alert]],
    case_c: tuple[DetectedChange, Signal, Alert],
    case_d: tuple[list[SourceConflict], Signal, Alert],
) -> Report:
    report = _upsert(
        db,
        Report,
        lookup={
            "organization_id": organization_id,
            "deduplication_key": "demo:report:gurugram-pilot-weekly-draft",
        },
        values={
            "title": "Gurugram Pilot Weekly Evidence Digest",
            "report_type": "weekly_digest",
            "state": "draft",
            "period_start": date(2026, 1, 5),
            "period_end": date(2026, 5, 11),
            "generated_by_user_id": analyst_user_id,
            "approved_by_user_id": None,
            "approved_at": None,
            "storage_key": None,
            "mime_type": None,
            "sha256": None,
            "size_bytes": None,
            "expires_at": _utc(2026, 8, 9),
            "deleted_at": None,
            "product_disclaimer": PRODUCT_DISCLAIMER,
            "summary_metrics": {
                "projects_monitored": 4,
                "completion_date_changes": 1,
                "brochure_conflicts": 3,
                "new_phase_or_registration_events": 1,
                "needs_review_items": 6,
            },
            "limitations": [
                DATASET_LIMITATION,
                "Source coverage may be incomplete or delayed.",
                "Conflicting and low-confidence claims require reviewer action.",
            ],
        },
    )
    case_a_change, case_a_signal, case_a_alert = case_a
    _, case_b_signals, case_b_alerts = case_b
    case_c_change, case_c_signal, case_c_alert = case_c
    _, case_d_signal, case_d_alert = case_d
    rows = [
        (
            case_a_alert,
            case_a_change,
            case_a_signal,
            "Completion date extension requires review",
            {
                "old_snapshot_id": case_a_change.old_snapshot_id,
                "new_snapshot_id": case_a_change.new_snapshot_id,
                "magnitude_days": 275,
            },
        ),
        *[
            (
                alert,
                None,
                signal,
                alert.title,
                {"source_conflict": True, "page_linked_claim": True},
            )
            for alert, signal in zip(case_b_alerts, case_b_signals, strict=True)
        ],
        (
            case_c_alert,
            case_c_change,
            case_c_signal,
            "New Sector 79 phase observed",
            {"parent_project_linked": True, "locality_pulse_increment": 1},
        ),
        (
            case_d_alert,
            None,
            case_d_signal,
            "Low-confidence completion claims remain unresolved",
            {"confirmed_external_alert": False, "ocr_confidence": 0.43},
        ),
    ]
    for ordinal, (alert, change, signal, title, evidence_manifest) in enumerate(rows):
        _upsert(
            db,
            ReportItem,
            lookup={"report_id": report.id, "ordinal": ordinal},
            values={
                "organization_id": organization_id,
                "project_id": alert.project_id,
                "alert_id": alert.id,
                "detected_change_id": change.id if change else None,
                "signal_id": signal.id,
                "item_type": "alert",
                "title": title,
                "narrative": alert.neutral_description,
                "evidence_manifest": evidence_manifest,
                "limitations": [alert.limitation] if alert.limitation else [],
                "included_at": alert.first_seen_at,
            },
        )
    return report


def _seed_audit_event(
    db: Session,
    *,
    organization: Organization,
    admin_user_id: str,
    report: Report,
) -> AuditEvent:
    return _upsert(
        db,
        AuditEvent,
        lookup={
            "organization_id": organization.id,
            "request_id": "seed:projecttruth-demo:v1",
        },
        values={
            "actor_user_id": admin_user_id,
            "action": "demo.seeded",
            "resource_type": "organization",
            "resource_id": organization.id,
            "ip_address": "127.0.0.1",
            "user_agent": "python -m app.seed_demo",
            "before_json": None,
            "after_json": {
                "organization_slug": organization.slug,
                "watchlist": DEMO_WATCHLIST_NAME,
                "case_registrations": [
                    "HRERA-DEMO-101",
                    "HRERA-DEMO-202",
                    "HRERA-DEMO-303",
                    "HRERA-DEMO-404",
                ],
                "draft_report_id": report.id,
            },
            "metadata_json": {
                "synthetic": True,
                "idempotency_key": "projecttruth-demo-v1",
            },
            "created_at": _utc(2026, 7, 1),
        },
    )


def seed_demo(db: Session) -> dict[str, int | str]:
    organization, users = _seed_organization_and_users(db)
    sources = _seed_sources(
        db,
        organization_id=organization.id,
        reviewer_user_id=users["reviewer"].id,
    )
    projects, locations = _seed_projects(
        db,
        organization_id=organization.id,
        sources=sources,
    )
    watchlist = _seed_watchlist(
        db,
        organization_id=organization.id,
        analyst_user_id=users["analyst"].id,
        projects=projects,
    )
    case_a = _seed_case_a(
        db,
        organization_id=organization.id,
        analyst_user_id=users["analyst"].id,
        watchlist=watchlist,
        project=projects["aster"],
        source=sources["official"],
    )
    case_b = _seed_case_b(
        db,
        organization_id=organization.id,
        analyst_user_id=users["analyst"].id,
        watchlist=watchlist,
        project=projects["lakeview"],
        official_source=sources["official"],
        brochure_source=sources["brochure"],
    )
    case_c = _seed_case_c(
        db,
        organization_id=organization.id,
        analyst_user_id=users["analyst"].id,
        watchlist=watchlist,
        project=projects["orion_phase"],
        source=sources["official"],
    )
    case_d = _seed_case_d(
        db,
        organization_id=organization.id,
        analyst_user_id=users["analyst"].id,
        watchlist=watchlist,
        project=projects["metro"],
        official_source=sources["official"],
        brochure_source=sources["brochure"],
        developer_source=sources["developer"],
    )
    _seed_market_snapshot(
        db,
        organization_id=organization.id,
        sector_79=locations["sector79"],
    )
    report = _seed_report(
        db,
        organization_id=organization.id,
        analyst_user_id=users["analyst"].id,
        case_a=case_a,
        case_b=case_b,
        case_c=case_c,
        case_d=case_d,
    )
    _seed_audit_event(
        db,
        organization=organization,
        admin_user_id=users["admin"].id,
        report=report,
    )
    return {
        "organization": organization.slug,
        "users": 4,
        "watchlists": 1,
        "watchlist_projects": 4,
        "sources": len(sources),
        "draft_reports": 1,
    }


def main() -> None:
    with session_scope() as db:
        summary = seed_demo(db)
    print(
        "Seeded ProjectTruth demo: "
        f"organization={summary['organization']}, users={summary['users']}, "
        f"watchlist_projects={summary['watchlist_projects']}, sources={summary['sources']}."
    )


if __name__ == "__main__":
    main()
