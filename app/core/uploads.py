from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from fastapi import UploadFile
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from app.core.errors import DomainError

PDF_SIGNATURE = b"%PDF-"
SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True)
class StoredUpload:
    storage_key: str
    original_filename: str
    sanitized_filename: str
    sha256: str
    size_bytes: int
    path: Path


def sanitize_filename(filename: str | None) -> str:
    raw = Path(filename or "document.pdf").name
    normalized = unicodedata.normalize("NFKD", raw).encode("ascii", "ignore").decode("ascii")
    safe = SAFE_NAME_RE.sub("_", normalized).strip("._")
    if not safe:
        safe = "document.pdf"
    if not safe.lower().endswith(".pdf"):
        safe = f"{safe}.pdf"
    return safe[:180]


async def store_pdf_upload(
    upload: UploadFile,
    *,
    storage_root: Path,
    organization_id: str,
    max_bytes: int,
) -> StoredUpload:
    declared_type = (upload.content_type or "").lower()
    if declared_type not in {"application/pdf", "application/x-pdf", ""}:
        raise DomainError("invalid_upload_type", "Only PDF documents are accepted.", 415)

    content = bytearray()
    while chunk := await upload.read(1024 * 1024):
        content.extend(chunk)
        if len(content) > max_bytes:
            raise DomainError("upload_too_large", "The PDF exceeds the configured upload limit.", 413)

    if not bytes(content[:5]).startswith(PDF_SIGNATURE):
        raise DomainError("invalid_pdf_signature", "The uploaded file is not a valid PDF.", 400)

    digest = hashlib.sha256(content).hexdigest()
    storage_key = str(uuid4())
    org_root = (storage_root / organization_id).resolve()
    root = storage_root.resolve()
    if root != org_root and root not in org_root.parents:
        raise DomainError("unsafe_storage_path", "Upload storage is not configured safely.", 500)
    org_root.mkdir(parents=True, exist_ok=True)
    target = (org_root / f"{storage_key}.pdf").resolve()
    if org_root not in target.parents:
        raise DomainError("unsafe_storage_path", "Upload storage is not configured safely.", 500)

    target.write_bytes(content)
    try:
        reader = PdfReader(str(target), strict=True)
        if reader.is_encrypted:
            raise DomainError(
                "encrypted_pdf",
                "Password-protected PDFs are not supported. Upload an authorized unlocked copy.",
                400,
            )
        if len(reader.pages) < 1:
            raise DomainError("pdf_unreadable", status_code=400)
    except DomainError:
        target.unlink(missing_ok=True)
        raise
    except (PdfReadError, ValueError, OSError) as exc:
        target.unlink(missing_ok=True)
        raise DomainError("pdf_unreadable", status_code=400) from exc

    return StoredUpload(
        storage_key=storage_key,
        original_filename=upload.filename or "document.pdf",
        sanitized_filename=sanitize_filename(upload.filename),
        sha256=digest,
        size_bytes=len(content),
        path=target,
    )


def resolve_private_artifact(root: Path, organization_id: str, storage_key: str) -> Path:
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", storage_key):
        raise DomainError("invalid_storage_key", "The requested artifact was not found.", 404)
    org_root = (root / organization_id).resolve()
    target = (org_root / f"{storage_key}.pdf").resolve()
    if org_root not in target.parents or not target.is_file():
        raise DomainError("not_found", "The requested artifact was not found.", 404)
    return target

