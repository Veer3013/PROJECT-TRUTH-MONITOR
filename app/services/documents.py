from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from app.domain import FetchResult, SourceAccessPolicy, ValidationStatus
from app.parsers import (
    MarketingClaim,
    OCRProvider,
    OptionalLLMClaimParser,
    PDFTextParser,
)
from app.services.conflicts import ClaimComparison, compare_brochure_claims
from app.services.normalization import canonical_field_key, normalize_field_value, normalize_text


@dataclass(frozen=True, slots=True)
class MalwareScanResult:
    scanner_name: str
    status: str
    safe: bool | None
    detail: str | None = None


class MalwareScanner(ABC):
    @abstractmethod
    async def scan(self, content: bytes) -> MalwareScanResult:
        raise NotImplementedError


class LocalMalwareScanStub(MalwareScanner):
    """Documented local stub; it never claims a document was malware-scanned."""

    async def scan(self, content: bytes) -> MalwareScanResult:
        return MalwareScanResult(
            "local_stub",
            "not_scanned",
            None,
            "Configure a real scanner for hosted use.",
        )


@dataclass(frozen=True, slots=True)
class DocumentExtractionResult:
    document_id: str
    document_version: str
    status: str
    extraction_method: str
    claims: tuple[MarketingClaim, ...]
    page_count: int
    confidence: float | None
    warnings: tuple[str, ...]
    requires_review: bool
    searchable_text_by_page: Mapping[int, str]
    malware_scan: MalwareScanResult


def _claims_from_parsed(
    parsed: Any,
    *,
    document_id: str,
    document_version: str,
    method: str | None = None,
) -> tuple[MarketingClaim, ...]:
    claims: list[MarketingClaim] = []
    for field in parsed.fields:
        if field.normalized_value is None or field.page is None:
            continue
        claims.append(
            MarketingClaim(
                field_key=field.canonical_field_key,
                raw_value=field.raw_value or "",
                normalized_value=field.normalized_value,
                document_id=document_id,
                document_version=document_version,
                page=field.page,
                evidence_text=(field.raw_value or "")[:1_000],
                coordinates=field.coordinates,
                extraction_confidence=field.extraction_confidence,
                extraction_method=method or field.extraction_method,
                validation_warnings=field.validation_warnings,
            )
        )
    return tuple(claims)


def _claims_from_ocr(
    pages: Mapping[int, str],
    confidences: Mapping[int, float],
    *,
    document_id: str,
    document_version: str,
) -> tuple[MarketingClaim, ...]:
    claims: list[MarketingClaim] = []
    for page, text in sorted(pages.items()):
        lines = [normalize_text(line) for line in text.splitlines() if normalize_text(line)]
        for index, line in enumerate(lines):
            match = re.match(r"^([^:]{2,80}):\s*(.+)$", line)
            if match:
                raw_label, raw_value = match.groups()
            elif canonical_field_key(line.rstrip(":")) and index + 1 < len(lines):
                raw_label, raw_value = line.rstrip(":"), lines[index + 1]
            else:
                continue
            key = canonical_field_key(raw_label)
            if key is None:
                continue
            normalized = normalize_field_value(key, raw_value)
            if normalized.value is None:
                continue
            confidence = max(0.0, min(1.0, confidences.get(page, 0.0)))
            core_fields = {
                "registration_number",
                "proposed_completion_date",
                "revised_completion_date",
                "current_status",
            }
            warnings = tuple(normalized.warnings) + (
                ("ocr_derived_core_field",) if key in core_fields else ()
            )
            claims.append(
                MarketingClaim(
                    field_key=key,
                    raw_value=raw_value,
                    normalized_value=normalized.value,
                    document_id=document_id,
                    document_version=document_version,
                    page=page,
                    evidence_text=line[:1_000],
                    extraction_confidence=confidence,
                    extraction_method="ocr",
                    validation_warnings=warnings,
                )
            )
    return tuple(claims)


class DocumentProcessingService:
    def __init__(
        self,
        *,
        pdf_parser: PDFTextParser | None = None,
        malware_scanner: MalwareScanner | None = None,
        ocr_provider: OCRProvider | None = None,
        llm_claim_parser: OptionalLLMClaimParser | None = None,
        minimum_ocr_confidence: float = 0.8,
    ) -> None:
        self.pdf_parser = pdf_parser or PDFTextParser()
        self.malware_scanner = malware_scanner or LocalMalwareScanStub()
        self.ocr_provider = ocr_provider
        self.llm_claim_parser = llm_claim_parser
        self.minimum_ocr_confidence = minimum_ocr_confidence

    async def extract(
        self,
        fetch_result: FetchResult,
        *,
        document_id: str,
        document_version: str,
        source_policy: SourceAccessPolicy,
        use_ocr_if_needed: bool = True,
    ) -> DocumentExtractionResult:
        if fetch_result.content is None:
            raise ValueError("Document extraction requires captured PDF content.")
        scan = await self.malware_scanner.scan(fetch_result.content)
        if scan.safe is False:
            return DocumentExtractionResult(
                document_id,
                document_version,
                "blocked",
                "none",
                (),
                0,
                None,
                ("The document failed malware scanning and was not processed.",),
                True,
                {},
                scan,
            )
        parsed = self.pdf_parser.parse(fetch_result)
        pages = self.pdf_parser.extract_text_pages(fetch_result)
        claims = _claims_from_parsed(
            parsed,
            document_id=document_id,
            document_version=document_version,
        )
        method = "pdf_text"
        warnings = list(parsed.validation_warnings)
        confidence_values = [claim.extraction_confidence for claim in claims]
        if parsed.validation_status == ValidationStatus.UNREADABLE or not pages:
            if (
                use_ocr_if_needed
                and source_policy.ocr_allowed
                and self.ocr_provider is not None
            ):
                ocr = await self.ocr_provider.extract(fetch_result.content)
                warnings.extend(ocr.warnings)
                if ocr.available and ocr.pages:
                    pages = {page.page: page.text for page in ocr.pages}
                    page_confidences = {
                        page.page: page.confidence for page in ocr.pages
                    }
                    claims = _claims_from_ocr(
                        pages,
                        page_confidences,
                        document_id=document_id,
                        document_version=document_version,
                    )
                    method = "ocr"
                    confidence_values = [
                        claim.extraction_confidence for claim in claims
                    ]
                else:
                    warnings.append(
                        "No reliable searchable text; OCR is unavailable or returned no pages."
                    )
            else:
                warnings.append("No reliable searchable text; OCR is unavailable or not permitted.")
        if pages and source_policy.llm_processing_allowed and self.llm_claim_parser is not None:
            llm_claims = await self.llm_claim_parser.parse_claims(
                pages,
                document_id=document_id,
                document_version=document_version,
            )
            # Preserve deterministic claims; add LLM claims only for fields not
            # already found. These remain marketing claims, never official facts.
            existing_keys = {claim.field_key for claim in claims}
            claims = claims + tuple(
                claim for claim in llm_claims if claim.field_key not in existing_keys
            )
            confidence_values.extend(claim.extraction_confidence for claim in llm_claims)
        minimum = min(confidence_values, default=None)
        requires_review = (
            not claims
            or method == "ocr"
            or (minimum is not None and minimum < self.minimum_ocr_confidence)
            or scan.safe is None
        )
        status = "needs_review" if requires_review else "completed"
        return DocumentExtractionResult(
            document_id=document_id,
            document_version=document_version,
            status=status,
            extraction_method=method,
            claims=claims,
            page_count=len(pages),
            confidence=minimum,
            warnings=tuple(warnings),
            requires_review=requires_review,
            searchable_text_by_page=pages,
            malware_scan=scan,
        )

    def compare(
        self,
        official_facts: Mapping[str, Any],
        extraction: DocumentExtractionResult,
    ) -> tuple[ClaimComparison, ...]:
        return compare_brochure_claims(official_facts, extraction.claims)
