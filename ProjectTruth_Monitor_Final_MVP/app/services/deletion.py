from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.source import utc_now
from app.models import (
    DeletionTombstone,
    Document,
    DocumentVersion,
    MonitoringJob,
    MonitoringRun,
    Organization,
    Project,
    Report,
    Source,
    SourceAccessPolicy,
    Watchlist,
    WatchlistItem,
)


@dataclass(frozen=True, slots=True)
class DeletionTombstoneRecord:
    organization_id: str
    resource_type: str
    resource_id_hash: str
    deleted_at: datetime
    requested_by_user_id: str
    idempotency_key_hash: str
    minimal_metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DeletionResult:
    tombstone: DeletionTombstoneRecord
    already_deleted: bool
    monitoring_cancelled: bool
    artifacts_removed: int


class DeletionRepository(Protocol):
    def find(
        self, organization_id: str, resource_type: str, resource_id_hash: str
    ) -> DeletionTombstoneRecord | None: ...

    def add(self, tombstone: DeletionTombstoneRecord) -> DeletionTombstoneRecord: ...


class InMemoryDeletionRepository:
    def __init__(self) -> None:
        self._records: dict[tuple[str, str, str], DeletionTombstoneRecord] = {}
        self._lock = RLock()

    def find(
        self, organization_id: str, resource_type: str, resource_id_hash: str
    ) -> DeletionTombstoneRecord | None:
        with self._lock:
            return self._records.get((organization_id, resource_type, resource_id_hash))

    def add(self, tombstone: DeletionTombstoneRecord) -> DeletionTombstoneRecord:
        key = (tombstone.organization_id, tombstone.resource_type, tombstone.resource_id_hash)
        with self._lock:
            return self._records.setdefault(key, tombstone)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def organization_deletion_idempotency_key(organization_id: str) -> str:
    """Return the server-owned stable key for one organization's deletion."""

    return _hash(f"projecttruth|organization-deletion|{organization_id}")


def remove_private_artifact(path: Path, *, allowed_root: Path) -> bool:
    root = allowed_root.resolve()
    target = path.resolve()
    if target == root or root not in target.parents:
        raise ValueError("Artifact deletion target is outside the configured private storage root.")
    if not target.exists():
        return False
    if not target.is_file():
        raise ValueError("Artifact deletion targets must be files.")
    target.unlink()
    return True


class DeletionService:
    def __init__(self, repository: DeletionRepository) -> None:
        self.repository = repository

    def delete(
        self,
        *,
        organization_id: str,
        resource_type: str,
        resource_id: str,
        requested_by_user_id: str,
        idempotency_key: str,
        cancel_monitoring: Callable[[], None],
        remove_artifacts: Iterable[Callable[[], bool]],
        delete_or_anonymize_records: Callable[[], None],
        minimal_metadata: dict[str, str] | None = None,
    ) -> DeletionResult:
        resource_hash = _hash(f"{organization_id}|{resource_type}|{resource_id}")
        existing = self.repository.find(organization_id, resource_type, resource_hash)
        if existing:
            return DeletionResult(existing, True, True, 0)
        # Order matters: prevent new work and artifact access before relational cleanup.
        cancel_monitoring()
        removed = sum(bool(operation()) for operation in remove_artifacts)
        delete_or_anonymize_records()
        tombstone = DeletionTombstoneRecord(
            organization_id=organization_id,
            resource_type=resource_type,
            resource_id_hash=resource_hash,
            deleted_at=utc_now(),
            requested_by_user_id=requested_by_user_id,
            idempotency_key_hash=_hash(idempotency_key),
            minimal_metadata=dict(minimal_metadata or {}),
        )
        stored = self.repository.add(tombstone)
        return DeletionResult(stored, stored is not tombstone, True, removed)

    def is_deleted(self, organization_id: str, resource_type: str, resource_id: str) -> bool:
        resource_hash = _hash(f"{organization_id}|{resource_type}|{resource_id}")
        return self.repository.find(organization_id, resource_type, resource_hash) is not None


@dataclass(frozen=True, slots=True)
class OrganizationDeletionResult:
    tombstone: DeletionTombstone
    already_deleted: bool
    monitoring_jobs_cancelled: int
    monitoring_runs_cancelled: int
    projects_deleted: int
    watchlists_deleted: int
    documents_deleted: int
    reports_deleted: int
    sources_disabled: int
    artifacts_removed: int

    def audit_metadata(self) -> dict[str, int | bool]:
        return {
            "already_deleted": self.already_deleted,
            "monitoring_jobs_cancelled": self.monitoring_jobs_cancelled,
            "monitoring_runs_cancelled": self.monitoring_runs_cancelled,
            "projects_deleted": self.projects_deleted,
            "watchlists_deleted": self.watchlists_deleted,
            "documents_deleted": self.documents_deleted,
            "reports_deleted": self.reports_deleted,
            "sources_disabled": self.sources_disabled,
            "artifacts_removed": self.artifacts_removed,
        }


_UUID_STORAGE_KEY = re.compile(r"[0-9a-fA-F-]{36}")
_REPORT_FORMAT_FILENAMES = ("pdf", "csv", "json")


class PersistentOrganizationDeletionService:
    """Tenant-scoped organization deletion using the relational tombstone table.

    Core monitoring history stays immutable. Mutable entry points are disabled or
    soft-deleted, private artifacts are removed, and a single stable tombstone
    makes retries safe.
    """

    def __init__(self, session: Session, *, upload_root: Path, report_root: Path) -> None:
        self.session = session
        self.upload_root = upload_root.resolve()
        self.report_root = report_root.resolve()

    def _existing_tombstone(self, organization_id: str) -> DeletionTombstone | None:
        return self.session.scalar(
            select(DeletionTombstone).where(
                DeletionTombstone.organization_id == organization_id,
                DeletionTombstone.resource_type == "organization",
                DeletionTombstone.resource_id == organization_id,
            )
        )

    @staticmethod
    def _count(metadata: dict[str, Any], key: str) -> int:
        try:
            return int(metadata.get(key, 0))
        except (TypeError, ValueError):
            return 0

    def _result_from_tombstone(
        self, tombstone: DeletionTombstone, *, already_deleted: bool
    ) -> OrganizationDeletionResult:
        metadata = dict(tombstone.minimal_metadata or {})
        return OrganizationDeletionResult(
            tombstone=tombstone,
            already_deleted=already_deleted,
            monitoring_jobs_cancelled=self._count(metadata, "monitoring_jobs_cancelled"),
            monitoring_runs_cancelled=self._count(metadata, "monitoring_runs_cancelled"),
            projects_deleted=self._count(metadata, "projects_deleted"),
            watchlists_deleted=self._count(metadata, "watchlists_deleted"),
            documents_deleted=self._count(metadata, "documents_deleted"),
            reports_deleted=self._count(metadata, "reports_deleted"),
            sources_disabled=self._count(metadata, "sources_disabled"),
            artifacts_removed=self._count(metadata, "artifacts_removed"),
        )

    @staticmethod
    def _safe_storage_token(value: str | None) -> str | None:
        token = (value or "").strip()
        return token if _UUID_STORAGE_KEY.fullmatch(token) else None

    def _artifact_targets(
        self,
        *,
        organization_id: str,
        documents: Iterable[Document],
        versions: Iterable[DocumentVersion],
        reports: Iterable[Report],
    ) -> tuple[tuple[Path, Path], ...]:
        targets: set[tuple[Path, Path]] = set()
        upload_tokens = {
            token
            for token in (
                self._safe_storage_token(item.storage_key)
                for item in (*tuple(documents), *tuple(versions))
            )
            if token
        }
        for token in upload_tokens:
            targets.add((self.upload_root / organization_id / f"{token}.pdf", self.upload_root))

        report_tokens: set[str] = set()
        for report in reports:
            report_tokens.add(report.id)
            if token := self._safe_storage_token(report.storage_key):
                report_tokens.add(token)
        for token in report_tokens:
            for suffix in _REPORT_FORMAT_FILENAMES:
                targets.add(
                    (
                        self.report_root / organization_id / f"{token}.{suffix}",
                        self.report_root,
                    )
                )
            targets.add(
                (self.report_root / organization_id / f"{token}-evidence.csv", self.report_root)
            )
        return tuple(sorted(targets, key=lambda item: str(item[0])))

    def delete(
        self,
        *,
        organization: Organization,
        requested_by_user_id: str,
        request_id: str | None,
    ) -> OrganizationDeletionResult:
        organization_id = organization.id
        existing = self._existing_tombstone(organization_id)
        if existing is not None:
            return self._result_from_tombstone(existing, already_deleted=True)

        now = utc_now()
        projects = list(
            self.session.scalars(select(Project).where(Project.organization_id == organization_id))
        )
        watchlists = list(
            self.session.scalars(
                select(Watchlist).where(Watchlist.organization_id == organization_id)
            )
        )
        watchlist_items = list(
            self.session.scalars(
                select(WatchlistItem).where(WatchlistItem.organization_id == organization_id)
            )
        )
        documents = list(
            self.session.scalars(
                select(Document).where(Document.organization_id == organization_id)
            )
        )
        versions = list(
            self.session.scalars(
                select(DocumentVersion).where(DocumentVersion.organization_id == organization_id)
            )
        )
        reports = list(
            self.session.scalars(select(Report).where(Report.organization_id == organization_id))
        )
        sources = list(
            self.session.scalars(select(Source).where(Source.organization_id == organization_id))
        )
        policies = list(
            self.session.scalars(
                select(SourceAccessPolicy).where(
                    SourceAccessPolicy.organization_id == organization_id
                )
            )
        )
        jobs = list(
            self.session.scalars(
                select(MonitoringJob).where(MonitoringJob.organization_id == organization_id)
            )
        )
        runs = list(
            self.session.scalars(
                select(MonitoringRun).where(
                    MonitoringRun.organization_id == organization_id,
                    MonitoringRun.status.in_(("queued", "running", "retry")),
                )
            )
        )

        artifacts_removed = sum(
            remove_private_artifact(path, allowed_root=root)
            for path, root in self._artifact_targets(
                organization_id=organization_id,
                documents=documents,
                versions=versions,
                reports=reports,
            )
        )

        projects_deleted = sum(item.deleted_at is None for item in projects)
        for item in projects:
            item.deleted_at = item.deleted_at or now

        watchlists_deleted = sum(item.deleted_at is None for item in watchlists)
        for item in watchlists:
            item.deleted_at = item.deleted_at or now
            item.is_active = False
        for item in watchlist_items:
            item.status = "deleted"

        documents_deleted = sum(item.deleted_at is None for item in documents)
        for item in documents:
            item.deleted_at = item.deleted_at or now
            item.status = "deleted"

        reports_deleted = sum(item.deleted_at is None for item in reports)
        for item in reports:
            item.deleted_at = item.deleted_at or now
            item.expires_at = now
            item.state = "deleted"

        sources_disabled = sum(item.is_enabled for item in sources)
        for item in sources:
            item.is_enabled = False
            item.health_status = "disabled"
            item.circuit_breaker_state = "open"
        for item in policies:
            item.is_enabled = False

        monitoring_jobs_cancelled = sum(
            item.state != "cancelled" or item.next_run_at is not None for item in jobs
        )
        for item in jobs:
            item.state = "cancelled"
            item.next_run_at = None

        monitoring_runs_cancelled = len(runs)
        for item in runs:
            item.status = "cancelled"
            item.completed_at = item.completed_at or now
            item.error_category = item.error_category or "organization_deleted"

        organization.is_active = False
        metadata: dict[str, int | bool] = {
            "monitoring_jobs_cancelled": monitoring_jobs_cancelled,
            "monitoring_runs_cancelled": monitoring_runs_cancelled,
            "projects_deleted": projects_deleted,
            "watchlists_deleted": watchlists_deleted,
            "documents_deleted": documents_deleted,
            "reports_deleted": reports_deleted,
            "sources_disabled": sources_disabled,
            "artifacts_removed": artifacts_removed,
            "organization_deactivated": True,
        }
        tombstone = DeletionTombstone(
            organization_id=organization_id,
            resource_type="organization",
            resource_id=organization_id,
            request_id=request_id,
            idempotency_key=organization_deletion_idempotency_key(organization_id),
            deleted_by_user_id=requested_by_user_id,
            deleted_at=now,
            reason_code="organization_data_deleted",
            minimal_metadata=metadata,
        )
        self.session.add(tombstone)
        self.session.flush()
        return self._result_from_tombstone(tombstone, already_deleted=False)
