from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from threading import RLock
from types import MappingProxyType
from typing import Any, Protocol

from app.domain import (
    AccessCompleteness,
    AccessStatus,
    EvidenceReference,
    ExtractedField,
    FetchResult,
    ParsedSourceSnapshot,
    SourceAccessPolicy,
    ValidationStatus,
)


class SnapshotError(RuntimeError):
    pass


class SnapshotImmutabilityError(SnapshotError):
    pass


def _json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_json_value(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class ImmutableSnapshot:
    id: str
    project_id: str
    source_id: str
    monitoring_run_id: str
    fetch_status: str
    fetched_at: datetime
    source_date: date | datetime | None
    content_hash: str
    parser_name: str
    parser_version: str
    access_completeness: AccessCompleteness
    structured_facts: Mapping[str, Any]
    fields: tuple[ExtractedField, ...]
    evidence_references: tuple[EvidenceReference, ...]
    validation_status: ValidationStatus
    schema_version: str = "1.0"
    policy_version: str | None = None
    raw_content: bytes | None = field(default=None, repr=False)
    derived_from_snapshot_id: str | None = None
    deduplication_key: str = ""
    requested_url: str | None = None
    final_url: str | None = None
    http_status: int | None = None
    content_type: str | None = None
    response_metadata: Mapping[str, Any] = field(default_factory=dict)
    latency_ms: int | None = None
    validation_warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "structured_facts", MappingProxyType(dict(self.structured_facts)))
        object.__setattr__(self, "fields", tuple(self.fields))
        object.__setattr__(self, "evidence_references", tuple(self.evidence_references))
        object.__setattr__(self, "response_metadata", MappingProxyType(dict(self.response_metadata)))
        object.__setattr__(self, "validation_warnings", tuple(self.validation_warnings))

    @property
    def snapshot_id(self) -> str:
        return self.id

    @property
    def is_verified(self) -> bool:
        return (
            self.fetch_status in {AccessStatus.SUCCESS.value, AccessStatus.PARTIAL.value}
            and self.validation_status == ValidationStatus.VALID
        )

    def json_facts(self) -> dict[str, Any]:
        return _json_value(self.structured_facts)


@dataclass(frozen=True, slots=True)
class SnapshotCaptureResult:
    snapshot: ImmutableSnapshot | None
    duplicate: bool
    updates_verified_state: bool
    monitoring_history_recorded: bool
    reason: str


class SnapshotRepository(Protocol):
    def add(self, snapshot: ImmutableSnapshot) -> ImmutableSnapshot: ...

    def get_by_deduplication_key(self, key: str) -> ImmutableSnapshot | None: ...

    def latest(self, project_id: str, source_id: str, *, verified_only: bool = False) -> ImmutableSnapshot | None: ...

    def history(self, project_id: str, source_id: str) -> tuple[ImmutableSnapshot, ...]: ...


class InMemorySnapshotRepository:
    """Reference repository with append-only semantics for tests/local adapters."""

    def __init__(self) -> None:
        self._items: dict[str, ImmutableSnapshot] = {}
        self._dedup: dict[str, str] = {}
        self._lock = RLock()

    def add(self, snapshot: ImmutableSnapshot) -> ImmutableSnapshot:
        with self._lock:
            if snapshot.id in self._items:
                if self._items[snapshot.id] != snapshot:
                    raise SnapshotImmutabilityError("Snapshot rows cannot be updated in place.")
                return self._items[snapshot.id]
            existing_id = self._dedup.get(snapshot.deduplication_key)
            if existing_id:
                return self._items[existing_id]
            self._items[snapshot.id] = snapshot
            self._dedup[snapshot.deduplication_key] = snapshot.id
            return snapshot

    def get_by_deduplication_key(self, key: str) -> ImmutableSnapshot | None:
        with self._lock:
            snapshot_id = self._dedup.get(key)
            return self._items.get(snapshot_id) if snapshot_id else None

    def get(self, snapshot_id: str) -> ImmutableSnapshot | None:
        with self._lock:
            return self._items.get(snapshot_id)

    def latest(self, project_id: str, source_id: str, *, verified_only: bool = False) -> ImmutableSnapshot | None:
        candidates = self.history(project_id, source_id)
        if verified_only:
            candidates = tuple(item for item in candidates if item.is_verified)
        return candidates[-1] if candidates else None

    def history(self, project_id: str, source_id: str) -> tuple[ImmutableSnapshot, ...]:
        with self._lock:
            return tuple(
                sorted(
                    (
                        item
                        for item in self._items.values()
                        if item.project_id == project_id and item.source_id == source_id
                    ),
                    key=lambda item: (item.fetched_at, item.id),
                )
            )

    def update(self, *_: Any, **__: Any) -> None:
        raise SnapshotImmutabilityError("Snapshot rows cannot be updated in place.")


def snapshot_deduplication_key(
    project_id: str,
    source_id: str,
    monitoring_run_id: str,
    content_hash: str,
    parser_name: str,
    parser_version: str,
    *,
    derived_from_snapshot_id: str | None = None,
) -> str:
    # monitoring_run_id is intentionally not part of the fact-snapshot identity:
    # later successful no-change runs retain their own run history but reuse the
    # immutable source snapshot. Reprocessing remains distinct through parser
    # version and derived_from_snapshot_id.
    del monitoring_run_id
    payload = "|".join(
        (
            project_id,
            source_id,
            content_hash,
            parser_name,
            parser_version,
            derived_from_snapshot_id or "",
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class SnapshotService:
    def __init__(self, repository: SnapshotRepository) -> None:
        self.repository = repository

    def capture(
        self,
        *,
        project_id: str,
        monitoring_run_id: str,
        fetch_result: FetchResult,
        parsed: ParsedSourceSnapshot | None,
        policy: SourceAccessPolicy,
        derived_from_snapshot_id: str | None = None,
    ) -> SnapshotCaptureResult:
        # Failed or blocked checks are monitoring history, never replacement facts.
        if not fetch_result.is_successful:
            return SnapshotCaptureResult(
                None,
                False,
                False,
                True,
                "source_access_incomplete_last_verified_facts_preserved",
            )
        # A conditional 304 is a successful no-change run, but contains no new source payload.
        if fetch_result.http_status == 304:
            return SnapshotCaptureResult(None, True, False, True, "source_not_modified")
        if parsed is None:
            return SnapshotCaptureResult(None, False, False, True, "no_parse_available")
        if parsed.source_id != fetch_result.source_id:
            raise SnapshotError("Parsed source and fetch source do not match.")
        if not fetch_result.content_hash:
            raise SnapshotError("A captured source snapshot requires a content hash.")
        latest = self.repository.latest(project_id, fetch_result.source_id)
        if (
            latest is not None
            and derived_from_snapshot_id is None
            and latest.content_hash == fetch_result.content_hash
            and latest.parser_name == parsed.parser_name
            and latest.parser_version == parsed.parser_version
        ):
            return SnapshotCaptureResult(
                latest,
                True,
                latest.is_verified,
                True,
                "content_not_modified",
            )
        dedup_key = snapshot_deduplication_key(
            project_id,
            fetch_result.source_id,
            monitoring_run_id,
            fetch_result.content_hash,
            parsed.parser_name,
            parsed.parser_version,
            derived_from_snapshot_id=derived_from_snapshot_id,
        )
        existing = self.repository.get_by_deduplication_key(dedup_key)
        if existing:
            return SnapshotCaptureResult(existing, True, existing.is_verified, True, "duplicate_capture")
        snapshot_id = hashlib.sha256(f"snapshot|{dedup_key}".encode()).hexdigest()[:32]
        snapshot = ImmutableSnapshot(
            id=snapshot_id,
            project_id=project_id,
            source_id=fetch_result.source_id,
            monitoring_run_id=monitoring_run_id,
            fetch_status=fetch_result.access_status.value,
            fetched_at=fetch_result.fetched_at,
            source_date=fetch_result.source_reported_date,
            content_hash=fetch_result.content_hash,
            parser_name=parsed.parser_name,
            parser_version=parsed.parser_version,
            access_completeness=fetch_result.completeness,
            structured_facts=parsed.structured_facts,
            fields=parsed.fields,
            evidence_references=parsed.evidence_references,
            validation_status=parsed.validation_status,
            schema_version=parsed.schema_version,
            policy_version=policy.policy_version,
            raw_content=fetch_result.content if policy.may_store_raw_content else None,
            derived_from_snapshot_id=derived_from_snapshot_id,
            deduplication_key=dedup_key,
            requested_url=fetch_result.requested_url,
            final_url=fetch_result.final_url,
            http_status=fetch_result.http_status,
            content_type=fetch_result.content_type,
            response_metadata=fetch_result.response_metadata,
            latency_ms=fetch_result.elapsed_ms,
            validation_warnings=parsed.validation_warnings,
        )
        stored = self.repository.add(snapshot)
        return SnapshotCaptureResult(
            stored,
            stored is not snapshot,
            stored.is_verified,
            True,
            "snapshot_captured" if stored is snapshot else "duplicate_capture",
        )

    def reprocess(
        self,
        *,
        prior_snapshot: ImmutableSnapshot,
        monitoring_run_id: str,
        parsed: ParsedSourceSnapshot,
        policy: SourceAccessPolicy,
    ) -> SnapshotCaptureResult:
        fetch = FetchResult(
            requested_url="reprocess://stored-evidence",
            final_url="reprocess://stored-evidence",
            source_id=prior_snapshot.source_id,
            http_status=200,
            fetched_at=prior_snapshot.fetched_at,
            source_reported_date=prior_snapshot.source_date,
            content_type=None,
            access_status=AccessStatus.SUCCESS,
            parser_eligible=True,
            completeness=prior_snapshot.access_completeness,
            content=prior_snapshot.raw_content or prior_snapshot.content_hash.encode("ascii"),
            content_hash=prior_snapshot.content_hash,
            policy_version=prior_snapshot.policy_version,
        )
        return self.capture(
            project_id=prior_snapshot.project_id,
            monitoring_run_id=monitoring_run_id,
            fetch_result=fetch,
            parsed=parsed,
            policy=policy,
            derived_from_snapshot_id=prior_snapshot.id,
        )


SourceSnapshot = ImmutableSnapshot


class SQLAlchemySnapshotRepository:
    """Append-only adapter for the SQLAlchemy models in :mod:`app.models`.

    The adapter flushes but does not commit so monitoring orchestration retains
    transaction control and can persist its run/change/audit records atomically.
    """

    def __init__(self, session: Any, organization_id: str) -> None:
        self.session = session
        self.organization_id = organization_id

    @staticmethod
    def _evidence_json(reference: EvidenceReference) -> dict[str, Any]:
        return {
            "selector": reference.selector,
            "page": reference.page,
            "coordinates": list(reference.coordinates) if reference.coordinates else None,
            "excerpt": reference.excerpt,
            "content_hash": reference.content_hash,
            "document_id": reference.document_id,
            "document_version": reference.document_version,
            "safe_reference": reference.safe_reference,
        }

    @staticmethod
    def _canonical_text(value: Any) -> str | None:
        if value is None:
            return None
        json_value = _json_value(value)
        if isinstance(json_value, (dict, list)):
            return json.dumps(json_value, sort_keys=True, separators=(",", ":"))
        return str(json_value)

    def _to_domain(self, row: Any) -> ImmutableSnapshot:
        from sqlalchemy import select

        from app.models import SnapshotField

        field_rows = tuple(
            self.session.scalars(
                select(SnapshotField)
                .where(
                    SnapshotField.organization_id == self.organization_id,
                    SnapshotField.snapshot_id == row.id,
                )
                .order_by(SnapshotField.field_key, SnapshotField.occurrence_index)
            )
        )
        fields: list[ExtractedField] = []
        for item in field_rows:
            canonical: Any
            if item.normalized_date is not None:
                canonical = item.normalized_date
            elif item.normalized_number is not None:
                canonical = item.normalized_number
            elif item.normalized_text is not None:
                canonical = item.normalized_text
            else:
                canonical = item.canonical_value
                if canonical and canonical[:1] in {"[", "{"}:
                    try:
                        canonical = json.loads(canonical)
                    except json.JSONDecodeError:
                        pass
            coords = None
            if item.coordinates:
                values = item.coordinates
                if isinstance(values, Mapping):
                    if "box" in values:
                        coords = tuple(float(value) for value in values["box"])
                    elif all(key in values for key in ("x0", "y0", "x1", "y1")):
                        coords = tuple(float(values[key]) for key in ("x0", "y0", "x1", "y1"))
            fields.append(
                ExtractedField(
                    raw_label=item.raw_label or item.field_key,
                    raw_value=item.raw_value,
                    canonical_field_key=item.field_key,
                    normalized_value=canonical,
                    unit=item.unit,
                    selector=item.selector,
                    page=item.page_number,
                    coordinates=coords,
                    extraction_method=item.extraction_method,
                    extraction_confidence=float(item.extraction_confidence or 0),
                    parser_name=item.parser_name,
                    parser_version=item.parser_version,
                    validation_warnings=tuple(item.validation_warnings or ()),
                )
            )
        evidence = tuple(
            EvidenceReference(
                selector=item.get("selector"),
                page=item.get("page"),
                coordinates=tuple(item["coordinates"]) if item.get("coordinates") else None,
                excerpt=item.get("excerpt"),
                content_hash=item.get("content_hash"),
                document_id=item.get("document_id"),
                document_version=item.get("document_version"),
                safe_reference=item.get("safe_reference"),
            )
            for item in (row.evidence_references or ())
        )
        return ImmutableSnapshot(
            id=row.id,
            project_id=row.project_id or row.entity_id,
            source_id=row.source_id,
            monitoring_run_id=row.monitoring_run_id,
            fetch_status=row.fetch_status,
            fetched_at=row.fetched_at,
            source_date=row.source_date,
            content_hash=row.content_hash,
            parser_name=row.parser_name,
            parser_version=row.parser_version,
            access_completeness=_coerce_access(row.access_completeness),
            structured_facts=row.structured_facts or {},
            fields=tuple(fields),
            evidence_references=evidence,
            validation_status=_coerce_validation(row.validation_status),
            deduplication_key=row.deduplication_key,
            requested_url=row.requested_url,
            final_url=row.final_url,
            http_status=row.http_status,
            content_type=row.content_type,
            response_metadata=row.response_metadata or {},
            latency_ms=row.latency_ms,
            validation_warnings=tuple(row.validation_warnings or ()),
        )

    def add(self, snapshot: ImmutableSnapshot) -> ImmutableSnapshot:
        from sqlalchemy import select

        from app.models import SnapshotField, SourceSnapshot

        existing = self.session.scalar(
            select(SourceSnapshot).where(
                SourceSnapshot.organization_id == self.organization_id,
                SourceSnapshot.deduplication_key == snapshot.deduplication_key,
            )
        )
        if existing:
            return self._to_domain(existing)
        source_date = snapshot.source_date
        if isinstance(source_date, date) and not isinstance(source_date, datetime):
            source_date = datetime.combine(source_date, datetime.min.time())
        row = SourceSnapshot(
            id=snapshot.id,
            organization_id=self.organization_id,
            source_id=snapshot.source_id,
            project_id=snapshot.project_id,
            monitoring_run_id=snapshot.monitoring_run_id,
            entity_type="project",
            entity_id=snapshot.project_id,
            fetch_status=snapshot.fetch_status,
            requested_url=snapshot.requested_url,
            final_url=snapshot.final_url,
            http_status=snapshot.http_status,
            fetched_at=snapshot.fetched_at,
            source_date=source_date,
            content_type=snapshot.content_type,
            content_hash=snapshot.content_hash,
            parser_name=snapshot.parser_name,
            parser_version=snapshot.parser_version,
            access_completeness=snapshot.access_completeness.value,
            structured_facts=snapshot.json_facts(),
            evidence_references=[self._evidence_json(item) for item in snapshot.evidence_references],
            validation_status=snapshot.validation_status.value,
            validation_warnings=list(snapshot.validation_warnings),
            raw_payload_hash=snapshot.content_hash if snapshot.raw_content is not None else None,
            response_metadata=_json_value(snapshot.response_metadata),
            latency_ms=snapshot.latency_ms,
            deduplication_key=snapshot.deduplication_key,
        )
        self.session.add(row)
        self.session.flush()
        occurrences: dict[str, int] = {}
        for field_value in snapshot.fields:
            occurrence = occurrences.get(field_value.canonical_field_key, 0)
            occurrences[field_value.canonical_field_key] = occurrence + 1
            canonical = field_value.normalized_value
            normalized_date = canonical if isinstance(canonical, date) else None
            normalized_number = canonical if isinstance(canonical, (int, float, Decimal)) and not isinstance(canonical, bool) else None
            normalized_text = canonical if isinstance(canonical, str) else None
            value_type = (
                "date" if normalized_date is not None else "number" if normalized_number is not None else "boolean" if isinstance(canonical, bool) else "json" if isinstance(canonical, (dict, list, tuple)) else "text"
            )
            coordinates = None
            if field_value.coordinates:
                coordinates = dict(zip(("x0", "y0", "x1", "y1"), field_value.coordinates, strict=True))
            self.session.add(
                SnapshotField(
                    organization_id=self.organization_id,
                    snapshot_id=row.id,
                    field_key=field_value.canonical_field_key,
                    occurrence_index=occurrence,
                    raw_label=field_value.raw_label,
                    raw_value=field_value.raw_value,
                    canonical_value=self._canonical_text(canonical),
                    normalized_text=normalized_text,
                    normalized_number=normalized_number,
                    normalized_date=normalized_date,
                    value_type=value_type,
                    unit=field_value.unit,
                    selector=field_value.selector,
                    page_number=field_value.page,
                    coordinates=coordinates,
                    extraction_method=field_value.extraction_method,
                    extraction_confidence=field_value.extraction_confidence,
                    parser_name=field_value.parser_name,
                    parser_version=field_value.parser_version,
                    validation_warnings=list(field_value.validation_warnings),
                )
            )
        self.session.flush()
        return snapshot

    def get_by_deduplication_key(self, key: str) -> ImmutableSnapshot | None:
        from sqlalchemy import select

        from app.models import SourceSnapshot

        row = self.session.scalar(
            select(SourceSnapshot).where(
                SourceSnapshot.organization_id == self.organization_id,
                SourceSnapshot.deduplication_key == key,
            )
        )
        return self._to_domain(row) if row else None

    def latest(self, project_id: str, source_id: str, *, verified_only: bool = False) -> ImmutableSnapshot | None:
        history = self.history(project_id, source_id)
        if verified_only:
            history = tuple(item for item in history if item.is_verified)
        return history[-1] if history else None

    def history(self, project_id: str, source_id: str) -> tuple[ImmutableSnapshot, ...]:
        from sqlalchemy import select

        from app.models import SourceSnapshot

        rows = self.session.scalars(
            select(SourceSnapshot)
            .where(
                SourceSnapshot.organization_id == self.organization_id,
                SourceSnapshot.project_id == project_id,
                SourceSnapshot.source_id == source_id,
            )
            .order_by(SourceSnapshot.fetched_at, SourceSnapshot.id)
        )
        return tuple(self._to_domain(row) for row in rows)


def _coerce_access(value: Any) -> AccessCompleteness:
    return value if isinstance(value, AccessCompleteness) else AccessCompleteness(str(value))


def _coerce_validation(value: Any) -> ValidationStatus:
    return value if isinstance(value, ValidationStatus) else ValidationStatus(str(value))
