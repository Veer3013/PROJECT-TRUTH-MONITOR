from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import func, select

from app.api.deps import AnalystActor, CSRFProtected, CurrentActor, DBSession
from app.api.utils import add_audit_event, commit_or_rollback
from app.api.v1.projects import get_tenant_project
from app.core.config import get_settings
from app.core.errors import DomainError
from app.core.rate_limit import rate_limiter
from app.core.uploads import resolve_private_artifact, store_pdf_upload
from app.domain import (
    AccessCompleteness,
    AccessStatus,
    FetchResult,
    SourceAccessPolicy,
    SourceClass,
)
from app.models import (
    Alert,
    DeletionTombstone,
    Document,
    DocumentClaim,
    DocumentExtraction,
    DocumentVersion,
    SnapshotField,
    Source,
    SourceConflict,
    SourceSnapshot,
)
from app.parsers import DisabledLLMClaimParser, DisabledOCRProvider
from app.parsers.base import ParserError
from app.schemas.common import DeleteResponse
from app.schemas.documents import (
    ClaimResponse,
    ComparisonResponse,
    DocumentResponse,
    DocumentVersionResponse,
    ExtractRequest,
)
from app.services.documents import DocumentProcessingService

router = APIRouter(tags=["Documents"])


def get_tenant_document(db: DBSession, organization_id: str, document_id: str) -> Document:
    document = db.scalar(
        select(Document).where(
            Document.id == document_id,
            Document.organization_id == organization_id,
            Document.deleted_at.is_(None),
        )
    )
    if document is None:
        raise HTTPException(status_code=404, detail={"code": "document_not_found"})
    return document


def _latest_document_version(
    db: DBSession, organization_id: str, document_id: str
) -> DocumentVersion | None:
    return db.scalar(
        select(DocumentVersion)
        .where(
            DocumentVersion.organization_id == organization_id,
            DocumentVersion.document_id == document_id,
        )
        .order_by(DocumentVersion.version_number.desc())
    )


def _document_source_policy(*, allow_ocr: bool) -> SourceAccessPolicy:
    return SourceAccessPolicy(
        source_name="User-authorized document",
        source_type="user_uploaded_document",
        base_url="upload://authorized-document",
        allowed_domains=(),
        enabled=True,
        permitted_access_modes=frozenset({"upload"}),
        may_store_raw_content=True,
        ocr_allowed=allow_ocr,
        llm_processing_allowed=False,
        allowed_schemes=frozenset({"upload"}),
        source_class=SourceClass.USER_AUTHORIZED,
    )


def _claim_value(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


@router.post(
    "/projects/{project_id}/documents",
    response_model=DocumentResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_document(
    project_id: str,
    request: Request,
    db: DBSession,
    actor: AnalystActor,
    csrf: CSRFProtected,
    file: UploadFile = File(...),
    authorization_attested: bool = Form(...),
    rights_attestation_text: str = Form(default="Authorized by the uploader"),
) -> Document:
    del csrf
    project = get_tenant_project(db, actor.organization_id, project_id)
    if not authorization_attested:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "authorization_required",
                "message": "Confirm that you are authorized to upload this document.",
            },
        )
    settings = get_settings()
    rate_limiter.check(
        f"upload:{actor.organization_id}:{actor.user_id}", limit=settings.upload_rate_limit
    )
    stored = await store_pdf_upload(
        file,
        storage_root=settings.upload_dir,
        organization_id=actor.organization_id,
        max_bytes=settings.max_upload_mb * 1024 * 1024,
    )
    document = Document(
        organization_id=actor.organization_id,
        project_id=project.id,
        document_type="brochure",
        title=Path(stored.sanitized_filename).stem,
        original_filename=stored.original_filename,
        sanitized_filename=stored.sanitized_filename,
        mime_type="application/pdf",
        authorization_attested=True,
        rights_attestation_text=rights_attestation_text[:2_000],
        attested_by_user_id=actor.user_id,
        created_by_user_id=actor.user_id,
        status="uploaded",
        storage_key=stored.storage_key,
        sha256=stored.sha256,
        size_bytes=stored.size_bytes,
    )
    db.add(document)
    db.flush()
    version = DocumentVersion(
        organization_id=actor.organization_id,
        document_id=document.id,
        version_number=1,
        content_hash=stored.sha256,
        file_size_bytes=stored.size_bytes,
        storage_key=stored.storage_key,
        uploaded_by_user_id=actor.user_id,
        is_encrypted=False,
        malware_scan_status="local_stub_not_configured",
    )
    db.add(version)
    add_audit_event(
        db,
        actor=actor,
        action="document.uploaded",
        resource_type="document",
        resource_id=document.id,
        request=request,
        metadata={"project_id": project.id, "sha256": stored.sha256, "size": stored.size_bytes},
    )
    commit_or_rollback(db)
    db.refresh(document)
    return document


@router.post(
    "/documents/{document_id}/versions",
    response_model=DocumentVersionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_document_version(
    document_id: str,
    request: Request,
    db: DBSession,
    actor: AnalystActor,
    csrf: CSRFProtected,
    file: UploadFile = File(...),
    authorization_attested: bool = Form(...),
    rights_attestation_text: str = Form(default="Authorized by the uploader"),
) -> DocumentVersion:
    del csrf
    document = get_tenant_document(db, actor.organization_id, document_id)
    if not authorization_attested:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "authorization_required",
                "message": "Confirm that you are authorized to upload this revision.",
            },
        )
    settings = get_settings()
    rate_limiter.check(
        f"upload:{actor.organization_id}:{actor.user_id}",
        limit=settings.upload_rate_limit,
    )
    stored = await store_pdf_upload(
        file,
        storage_root=settings.upload_dir,
        organization_id=actor.organization_id,
        max_bytes=settings.max_upload_mb * 1024 * 1024,
    )
    duplicate = db.scalar(
        select(DocumentVersion).where(
            DocumentVersion.organization_id == actor.organization_id,
            DocumentVersion.document_id == document.id,
            DocumentVersion.content_hash == stored.sha256,
        )
    )
    if duplicate is not None:
        stored.path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=409,
            detail={
                "code": "duplicate_document_version",
                "message": "This exact document version is already stored.",
                "version_id": duplicate.id,
            },
        )
    latest_number = db.scalar(
        select(func.max(DocumentVersion.version_number)).where(
            DocumentVersion.organization_id == actor.organization_id,
            DocumentVersion.document_id == document.id,
        )
    )
    version = DocumentVersion(
        organization_id=actor.organization_id,
        document_id=document.id,
        version_number=int(latest_number or 0) + 1,
        content_hash=stored.sha256,
        file_size_bytes=stored.size_bytes,
        storage_key=stored.storage_key,
        uploaded_by_user_id=actor.user_id,
        is_encrypted=False,
        malware_scan_status="local_stub_not_configured",
    )
    db.add(version)
    document.original_filename = stored.original_filename
    document.sanitized_filename = stored.sanitized_filename
    document.storage_key = stored.storage_key
    document.sha256 = stored.sha256
    document.size_bytes = stored.size_bytes
    document.rights_attestation_text = rights_attestation_text[:2_000]
    document.attested_by_user_id = actor.user_id
    document.status = "uploaded"
    add_audit_event(
        db,
        actor=actor,
        action="document.version_uploaded",
        resource_type="document_version",
        resource_id=version.id,
        request=request,
        metadata={
            "document_id": document.id,
            "version_number": version.version_number,
            "sha256": stored.sha256,
            "size": stored.size_bytes,
        },
    )
    commit_or_rollback(db)
    db.refresh(version)
    return version


@router.get(
    "/documents/{document_id}/versions",
    response_model=list[DocumentVersionResponse],
)
def list_document_versions(
    document_id: str, db: DBSession, actor: CurrentActor
) -> list[DocumentVersion]:
    document = get_tenant_document(db, actor.organization_id, document_id)
    return list(
        db.scalars(
            select(DocumentVersion)
            .where(
                DocumentVersion.organization_id == actor.organization_id,
                DocumentVersion.document_id == document.id,
            )
            .order_by(DocumentVersion.version_number.desc())
        )
    )


@router.get(
    "/documents/{document_id}/versions/{version_id}/download",
    response_class=FileResponse,
)
def download_document_version(
    document_id: str,
    version_id: str,
    db: DBSession,
    actor: CurrentActor,
) -> FileResponse:
    document = get_tenant_document(db, actor.organization_id, document_id)
    version = db.scalar(
        select(DocumentVersion).where(
            DocumentVersion.id == version_id,
            DocumentVersion.organization_id == actor.organization_id,
            DocumentVersion.document_id == document.id,
        )
    )
    if version is None:
        raise HTTPException(
            status_code=404, detail={"code": "document_version_not_found"}
        )
    path = resolve_private_artifact(
        get_settings().upload_dir, actor.organization_id, version.storage_key
    )
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=f"v{version.version_number}-{document.sanitized_filename}",
        headers={"Cache-Control": "private, no-store"},
    )


@router.get("/documents/{document_id}", response_model=DocumentResponse)
def get_document(document_id: str, db: DBSession, actor: CurrentActor) -> Document:
    return get_tenant_document(db, actor.organization_id, document_id)


@router.get("/documents/{document_id}/download", response_class=FileResponse)
def download_document(document_id: str, db: DBSession, actor: CurrentActor) -> FileResponse:
    document = get_tenant_document(db, actor.organization_id, document_id)
    if not document.storage_key:
        raise HTTPException(status_code=404, detail={"code": "artifact_not_found"})
    path = resolve_private_artifact(
        get_settings().upload_dir, actor.organization_id, document.storage_key
    )
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=document.sanitized_filename,
        headers={"Cache-Control": "private, no-store"},
    )


@router.post("/documents/{document_id}/extract", response_model=list[ClaimResponse])
async def extract_document(
    document_id: str,
    payload: ExtractRequest,
    request: Request,
    db: DBSession,
    actor: AnalystActor,
    csrf: CSRFProtected,
) -> list[DocumentClaim]:
    del csrf
    document = get_tenant_document(db, actor.organization_id, document_id)
    version = _latest_document_version(db, actor.organization_id, document.id)
    if version is None:
        raise HTTPException(status_code=409, detail={"code": "document_version_missing"})
    existing = db.scalar(
        select(DocumentExtraction)
        .where(
            DocumentExtraction.organization_id == actor.organization_id,
            DocumentExtraction.document_version_id == version.id,
            DocumentExtraction.status.in_(["completed", "needs_review"]),
        )
        .order_by(DocumentExtraction.created_at.desc())
    )
    if existing:
        return list(
            db.scalars(
                select(DocumentClaim).where(
                    DocumentClaim.organization_id == actor.organization_id,
                    DocumentClaim.document_extraction_id == existing.id,
                )
            )
        )
    settings = get_settings()
    path = resolve_private_artifact(
        settings.upload_dir, actor.organization_id, version.storage_key
    )
    content = path.read_bytes()
    fetch = FetchResult(
        requested_url=f"upload://{document.id}",
        final_url=f"upload://{document.id}",
        source_id=document.source_id or f"document:{document.id}",
        http_status=200,
        fetched_at=datetime.now(UTC),
        content_type="application/pdf",
        access_status=AccessStatus.SUCCESS,
        parser_eligible=True,
        completeness=AccessCompleteness.COMPLETE,
        content=content,
    )
    processor = DocumentProcessingService(
        ocr_provider=DisabledOCRProvider(),
        llm_claim_parser=DisabledLLMClaimParser(),
    )
    extraction = DocumentExtraction(
        organization_id=actor.organization_id,
        document_version_id=version.id,
        provider_name="deterministic_document_pipeline",
        provider_version="1.0.0",
        extraction_method="pending",
        status="processing",
        started_at=datetime.now(UTC),
        deduplication_key=hashlib.sha256(
            f"{version.id}:deterministic_document_pipeline:1.0.0".encode()
        ).hexdigest(),
    )
    db.add(extraction)
    db.flush()
    try:
        result = await processor.extract(
            fetch,
            document_id=document.id,
            document_version=str(version.version_number),
            source_policy=_document_source_policy(allow_ocr=payload.use_ocr_if_needed),
            use_ocr_if_needed=payload.use_ocr_if_needed,
        )
    except ParserError as exc:
        extraction.status = "failed"
        extraction.warnings = [exc.code]
        extraction.requires_review = True
        extraction.completed_at = datetime.now(UTC)
        document.status = "needs_review"
        add_audit_event(
            db,
            actor=actor,
            action="document.extraction_failed",
            resource_type="document_extraction",
            resource_id=extraction.id,
            request=request,
            metadata={"error_category": exc.code},
        )
        commit_or_rollback(db)
        raise HTTPException(
            status_code=422,
            detail={
                "code": "pdf_unreadable",
                "message": (
                    "We could not reliably read this document. "
                    "Upload a clearer copy or review it manually."
                ),
            },
        ) from exc
    extraction.provider_name = (
        processor.pdf_parser.parser_name
        if result.extraction_method == "pdf_text"
        else settings.ocr_provider
    )
    extraction.provider_version = (
        processor.pdf_parser.parser_version
        if result.extraction_method == "pdf_text"
        else processor.ocr_provider.provider_version
        if processor.ocr_provider is not None
        else "not_configured"
    )
    extraction.extraction_method = result.extraction_method
    extraction.status = result.status
    extraction.page_count = result.page_count
    extraction.confidence = result.confidence
    extraction.warnings = list(result.warnings)
    extraction.requires_review = result.requires_review
    extraction.completed_at = datetime.now(UTC)
    extraction.text_content = "\n".join(
        result.searchable_text_by_page[page]
        for page in sorted(result.searchable_text_by_page)
    )
    extraction.extraction_metadata = {
        "document_version": version.version_number,
        "malware_scanner": result.malware_scan.scanner_name,
        "malware_scan_status": result.malware_scan.status,
        "ocr_requested": payload.use_ocr_if_needed,
        "ocr_provider": settings.ocr_provider,
        "llm_provider": settings.llm_provider,
    }
    version.malware_scan_status = result.malware_scan.status
    claims: list[DocumentClaim] = []
    for candidate in result.claims:
        claim = DocumentClaim(
            organization_id=actor.organization_id,
            document_extraction_id=extraction.id,
            document_version_id=version.id,
            project_id=document.project_id,
            field_key=candidate.field_key,
            raw_value=candidate.raw_value,
            normalized_value=_claim_value(candidate.normalized_value),
            value_type="text",
            unit=None,
            page_number=candidate.page,
            evidence_text=candidate.evidence_text[:2_000],
            coordinates=(
                {"box": list(candidate.coordinates)}
                if candidate.coordinates
                else None
            ),
            extraction_method=candidate.extraction_method,
            extraction_confidence=candidate.extraction_confidence,
            comparison_state=(
                "needs_review"
                if result.requires_review or candidate.validation_warnings
                else "unverified"
            ),
            review_state="pending",
        )
        db.add(claim)
        claims.append(claim)
    document.status = "needs_review" if result.requires_review else "extracted"
    add_audit_event(
        db,
        actor=actor,
        action="document.extracted",
        resource_type="document_extraction",
        resource_id=extraction.id,
        request=request,
        metadata={
            "claim_count": len(claims),
            "document_version": version.version_number,
            "extraction_method": result.extraction_method,
            "requires_review": result.requires_review,
        },
    )
    commit_or_rollback(db)
    for claim in claims:
        db.refresh(claim)
    return claims


@router.post("/documents/{document_id}/compare", response_model=ComparisonResponse)
def compare_document(
    document_id: str,
    request: Request,
    db: DBSession,
    actor: AnalystActor,
    csrf: CSRFProtected,
) -> ComparisonResponse:
    del csrf
    document = get_tenant_document(db, actor.organization_id, document_id)
    if not document.project_id:
        raise HTTPException(status_code=409, detail={"code": "document_project_missing"})
    version = _latest_document_version(db, actor.organization_id, document.id)
    if version is None:
        raise HTTPException(status_code=409, detail={"code": "document_version_missing"})
    claims = list(
        db.scalars(
            select(DocumentClaim)
            .where(
                DocumentClaim.organization_id == actor.organization_id,
                DocumentClaim.project_id == document.project_id,
                DocumentClaim.document_version_id == version.id,
            )
            .order_by(DocumentClaim.field_key)
        )
    )
    if not claims:
        raise HTTPException(
            status_code=409,
            detail={"code": "document_not_extracted", "message": "Extract the document first."},
        )
    latest_snapshot = db.scalar(
        select(SourceSnapshot)
        .join(Source, Source.id == SourceSnapshot.source_id)
        .where(
            SourceSnapshot.organization_id == actor.organization_id,
            SourceSnapshot.project_id == document.project_id,
            SourceSnapshot.validation_status.in_(["valid", "partial"]),
            Source.organization_id == actor.organization_id,
            Source.source_class == SourceClass.OFFICIAL_REGULATOR.value,
        )
        .order_by(SourceSnapshot.fetched_at.desc())
    )
    official: dict[str, SnapshotField] = {}
    if latest_snapshot:
        official = {
            field.field_key: field
            for field in db.scalars(
                select(SnapshotField).where(
                    SnapshotField.organization_id == actor.organization_id,
                    SnapshotField.snapshot_id == latest_snapshot.id,
                )
            )
        }
    results: list[dict[str, object]] = []
    needs_review = 0
    for claim in claims:
        fact = official.get(claim.field_key)
        official_value = fact.canonical_value if fact else None
        claim.official_snapshot_field_id = fact.id if fact else None
        if claim.extraction_confidence is None or claim.extraction_confidence < 0.8:
            state = "needs_review"
        elif fact is None:
            state = "marketing_only"
        elif (claim.normalized_value or "").strip().casefold() == (
            official_value or ""
        ).strip().casefold():
            state = "matched"
        else:
            state = "conflicting"
        claim.comparison_state = state
        if state in {"needs_review", "conflicting"}:
            needs_review += 1
        if state == "conflicting" and latest_snapshot:
            dedup = hashlib.sha256(
                f"{document.project_id}:{document.id}:{claim.field_key}:{official_value}:{claim.normalized_value}".encode()
            ).hexdigest()
            conflict = db.scalar(
                select(SourceConflict).where(
                    SourceConflict.organization_id == actor.organization_id,
                    SourceConflict.deduplication_key == dedup,
                )
            )
            if conflict is None:
                conflict = SourceConflict(
                    organization_id=actor.organization_id,
                    project_id=document.project_id,
                    field_key=claim.field_key,
                    source_a_id=latest_snapshot.source_id,
                    source_b_id=document.source_id,
                    source_a_class="official_regulator",
                    source_b_class="marketing_brochure",
                    source_a_value=official_value,
                    source_b_value=claim.normalized_value,
                    source_a_reference={"snapshot_id": latest_snapshot.id},
                    source_b_reference={
                        "document_id": document.id,
                        "document_version_id": version.id,
                        "document_version": version.version_number,
                        "content_hash": version.content_hash,
                        "page": claim.page_number,
                    },
                    source_a_freshness_at=latest_snapshot.fetched_at,
                    source_b_freshness_at=document.created_at,
                    comparison_state="conflicting",
                    is_material=True,
                    state="open",
                    deduplication_key=dedup,
                )
                db.add(conflict)
                db.flush()
                db.add(
                    Alert(
                        organization_id=actor.organization_id,
                        project_id=document.project_id,
                        title=f"{claim.field_key.replace('_', ' ').title()} source conflict",
                        neutral_description=(
                            "Marketing claim conflicts with the currently captured official record."
                        ),
                        old_value=official_value,
                        new_value=claim.normalized_value,
                        state="open",
                        severity="high",
                        required_review_level="reviewer",
                        requires_review=True,
                        source_class="marketing_brochure",
                        limitation="Sources disagree; both values are preserved for review.",
                        suggested_review_action="Review both evidence records before client use.",
                        deduplication_key=f"conflict:{dedup}",
                    )
                )
        results.append(
            {
                "claim_id": claim.id,
                "document_version_id": version.id,
                "document_version": version.version_number,
                "field_key": claim.field_key,
                "marketing_value": claim.normalized_value,
                "official_value": official_value,
                "state": state.upper(),
                "page": claim.page_number,
                "evidence_text": claim.evidence_text,
                "coordinates": claim.coordinates,
                "wording": (
                    "Marketing claim conflicts with the currently captured official record."
                    if state == "conflicting"
                    else None
                ),
            }
        )
    document.status = "compared"
    add_audit_event(
        db,
        actor=actor,
        action="document.compared",
        resource_type="document",
        resource_id=document.id,
        request=request,
        metadata={"results": len(results), "needs_review": needs_review},
    )
    commit_or_rollback(db)
    return ComparisonResponse(
        project_id=document.project_id,
        document_id=document.id,
        results=results,
        needs_review_count=needs_review,
    )


@router.delete("/documents/{document_id}", response_model=DeleteResponse)
def delete_document(
    document_id: str,
    request: Request,
    db: DBSession,
    actor: AnalystActor,
    csrf: CSRFProtected,
) -> DeleteResponse:
    del csrf
    document = db.scalar(
        select(Document).where(
            Document.id == document_id,
            Document.organization_id == actor.organization_id,
        )
    )
    if document is None or document.deleted_at is not None:
        return DeleteResponse(deleted=False)
    storage_keys = {
        version.storage_key
        for version in db.scalars(
            select(DocumentVersion).where(
                DocumentVersion.organization_id == actor.organization_id,
                DocumentVersion.document_id == document.id,
            )
        )
    }
    if document.storage_key:
        storage_keys.add(document.storage_key)
    for storage_key in storage_keys:
        try:
            path = resolve_private_artifact(
                get_settings().upload_dir, actor.organization_id, storage_key
            )
            path.unlink(missing_ok=True)
        except (DomainError, OSError):
            # A missing artifact is already in the desired deleted state.
            path = None
    document.deleted_at = datetime.now(UTC)
    document.status = "deleted"
    tombstone = DeletionTombstone(
        organization_id=actor.organization_id,
        resource_type="document",
        resource_id=document.id,
        request_id=request.state.request_id,
        idempotency_key=f"document:{actor.organization_id}:{document.id}",
        deleted_by_user_id=actor.user_id,
        reason_code="user_requested",
        minimal_metadata={"sha256": document.sha256},
    )
    db.add(tombstone)
    add_audit_event(
        db,
        actor=actor,
        action="document.deleted",
        resource_type="document",
        resource_id=document.id,
        request=request,
    )
    commit_or_rollback(db)
    return DeleteResponse(deleted=True)
