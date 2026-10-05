from __future__ import annotations

import asyncio
import io
from datetime import UTC, datetime

from reportlab.pdfgen import canvas

from app.domain import (
    AccessCompleteness,
    AccessStatus,
    FetchResult,
    SourceAccessPolicy,
)
from app.parsers import OCRPage, OCRProvider, OCRResult
from app.services.documents import (
    DocumentProcessingService,
    MalwareScanner,
    MalwareScanResult,
)


class LowConfidenceOCR(OCRProvider):
    provider_name = "test_ocr"
    provider_version = "1"

    async def extract(self, pdf_content: bytes) -> OCRResult:
        assert pdf_content.startswith(b"%PDF-")
        return OCRResult(
            provider_name=self.provider_name,
            provider_version=self.provider_version,
            pages=(
                OCRPage(
                    page=1,
                    text="Proposed Completion Date: 31 December 2029",
                    confidence=0.42,
                ),
            ),
            available=True,
        )


class SafeScanner(MalwareScanner):
    async def scan(self, content: bytes) -> MalwareScanResult:
        return MalwareScanResult("test_scanner", "clean", True)


def _blank_pdf() -> bytes:
    output = io.BytesIO()
    document = canvas.Canvas(output)
    document.showPage()
    document.save()
    return output.getvalue()


def test_low_confidence_ocr_claim_stays_marketing_evidence_pending_review() -> None:
    content = _blank_pdf()
    fetch = FetchResult(
        requested_url="upload://document",
        final_url="upload://document",
        source_id="document:demo",
        http_status=200,
        fetched_at=datetime.now(UTC),
        content_type="application/pdf",
        access_status=AccessStatus.SUCCESS,
        parser_eligible=True,
        completeness=AccessCompleteness.COMPLETE,
        content=content,
    )
    policy = SourceAccessPolicy(
        source_name="Authorized PDF",
        source_type="user_uploaded_document",
        base_url="upload://document",
        allowed_domains=(),
        permitted_access_modes=frozenset({"upload"}),
        ocr_allowed=True,
        allowed_schemes=frozenset({"upload"}),
    )
    service = DocumentProcessingService(
        malware_scanner=SafeScanner(),
        ocr_provider=LowConfidenceOCR(),
    )
    result = asyncio.run(
        service.extract(
            fetch,
            document_id="document-id",
            document_version="2",
            source_policy=policy,
            use_ocr_if_needed=True,
        )
    )
    assert result.extraction_method == "ocr"
    assert result.status == "needs_review"
    assert result.requires_review is True
    assert len(result.claims) == 1
    claim = result.claims[0]
    assert claim.document_id == "document-id"
    assert claim.document_version == "2"
    assert claim.page == 1
    assert claim.extraction_confidence == 0.42
    assert "ocr_derived_core_field" in claim.validation_warnings

    official = {"proposed_completion_date": "30 June 2029"}
    comparison = service.compare(official, result)[0]
    assert comparison.state.value == "NEEDS_REVIEW"
    assert comparison.requires_review is True
    assert official == {"proposed_completion_date": "30 June 2029"}
