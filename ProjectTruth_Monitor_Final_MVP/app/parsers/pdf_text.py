from __future__ import annotations

import io
import re
from collections import defaultdict

from app.domain import (
    EvidenceReference,
    ExtractedField,
    FetchResult,
    ParsedSourceSnapshot,
    ValidationStatus,
)
from app.services.normalization import canonical_field_key, normalize_field_value, normalize_text

from .base import EncryptedPDFError, ParserError, SourceParser

_LABEL_VALUE = re.compile(r"^\s*([^:\n]{2,80})\s*:\s*(.+?)\s*$")


class PDFTextParser(SourceParser):
    parser_name = "pdf_text"
    parser_version = "1.0.0"

    def can_parse(self, fetch_result: FetchResult) -> bool:
        content_type = (fetch_result.content_type or "").lower()
        return fetch_result.content is not None and (
            "pdf" in content_type or fetch_result.content.startswith(b"%PDF-")
        )

    def _extract_with_fitz(self, content: bytes) -> list[tuple[int, tuple[float, float, float, float] | None, str]]:
        try:
            import fitz
        except ImportError:
            return []
        try:
            document = fitz.open(stream=content, filetype="pdf")
        except Exception as exc:
            raise ParserError("corrupted_pdf", "The PDF could not be opened reliably.") from exc
        try:
            if document.needs_pass or document.is_encrypted:
                raise EncryptedPDFError()
            blocks: list[tuple[int, tuple[float, float, float, float] | None, str]] = []
            for page_number, page in enumerate(document, start=1):
                for block in page.get_text("blocks"):
                    coordinates = tuple(float(item) for item in block[:4])
                    text = "\n".join(
                        normalize_text(line) for line in str(block[4]).splitlines() if normalize_text(line)
                    )
                    if text:
                        blocks.append((page_number, coordinates, text))
            return blocks
        finally:
            document.close()

    def _extract_with_pdfplumber(self, content: bytes) -> list[tuple[int, None, str]]:
        try:
            import pdfplumber
        except ImportError as exc:
            raise RuntimeError("PyMuPDF or pdfplumber is required for PDF parsing") from exc
        try:
            with pdfplumber.open(io.BytesIO(content)) as document:
                if getattr(document, "is_encrypted", False):
                    raise EncryptedPDFError()
                pages: list[tuple[int, None, str]] = []
                for page_number, page in enumerate(document.pages, start=1):
                    raw_text = page.extract_text() or ""
                    text = "\n".join(
                        normalize_text(line) for line in raw_text.splitlines() if normalize_text(line)
                    )
                    if text:
                        pages.append((page_number, None, text))
                return pages
        except EncryptedPDFError:
            raise
        except Exception as exc:
            if "password" in str(exc).casefold() or "encrypt" in str(exc).casefold():
                raise EncryptedPDFError() from exc
            raise ParserError("corrupted_pdf", "The PDF could not be read reliably.") from exc

    @staticmethod
    def _candidate_pairs(text: str) -> list[tuple[str, str]]:
        lines = [normalize_text(line) for line in re.split(r"[\r\n]+", text) if normalize_text(line)]
        pairs: list[tuple[str, str]] = []
        for index, line in enumerate(lines):
            match = _LABEL_VALUE.match(line)
            if match:
                pairs.append((match.group(1), match.group(2)))
                continue
            if canonical_field_key(line.rstrip(":")) and index + 1 < len(lines):
                next_line = lines[index + 1]
                if canonical_field_key(next_line.rstrip(":")) is None:
                    pairs.append((line.rstrip(":"), next_line))
        return pairs

    def parse(self, fetch_result: FetchResult) -> ParsedSourceSnapshot:
        self.require_eligible(fetch_result)
        content = fetch_result.content or b""
        blocks = self._extract_with_fitz(content)
        if not blocks:
            blocks = self._extract_with_pdfplumber(content)
        if not blocks:
            return ParsedSourceSnapshot(
                source_id=fetch_result.source_id,
                parser_name=self.parser_name,
                parser_version=self.parser_version,
                fields=(),
                validation_status=ValidationStatus.UNREADABLE,
                validation_warnings=("no_searchable_text; optional OCR or manual review required",),
            )
        extracted: list[ExtractedField] = []
        evidence: list[EvidenceReference] = []
        seen: set[tuple[str, str, int]] = set()
        grouped_links: dict[str, list[str]] = defaultdict(list)
        for page, coordinates, block_text in blocks:
            for raw_label, raw_value in self._candidate_pairs(block_text):
                key = canonical_field_key(raw_label)
                if key is None:
                    continue
                identity = (key, normalize_text(raw_value), page)
                if identity in seen:
                    continue
                seen.add(identity)
                normalized = normalize_field_value(key, raw_value)
                if key in {"document_links", "official_notices"}:
                    grouped_links[key].extend(normalized.value or ())
                    continue
                confidence = 0.9 if normalized.value is not None and not normalized.warnings else 0.55
                field = ExtractedField(
                    raw_label=normalize_text(raw_label),
                    raw_value=normalize_text(raw_value),
                    canonical_field_key=key,
                    normalized_value=normalized.value,
                    unit=normalized.unit,
                    page=page,
                    coordinates=coordinates,
                    extraction_method="pdf_text",
                    extraction_confidence=confidence,
                    parser_name=self.parser_name,
                    parser_version=self.parser_version,
                    validation_warnings=normalized.warnings,
                )
                extracted.append(field)
                evidence.append(
                    EvidenceReference(
                        page=page,
                        coordinates=coordinates,
                        excerpt=field.raw_value,
                        content_hash=fetch_result.content_hash,
                    )
                )
        for key, links in grouped_links.items():
            unique_links = tuple(sorted(set(links)))
            extracted.append(
                ExtractedField(
                    raw_label=key.replace("_", " ").title(),
                    raw_value="\n".join(unique_links),
                    canonical_field_key=key,
                    normalized_value=unique_links,
                    extraction_method="pdf_text_link",
                    extraction_confidence=0.85,
                    parser_name=self.parser_name,
                    parser_version=self.parser_version,
                )
            )
        status = ValidationStatus.VALID if extracted else ValidationStatus.NEEDS_REVIEW
        warnings = () if extracted else ("searchable_text_found_but_no_supported_fields",)
        return ParsedSourceSnapshot(
            source_id=fetch_result.source_id,
            parser_name=self.parser_name,
            parser_version=self.parser_version,
            fields=tuple(extracted),
            validation_status=status,
            validation_warnings=warnings,
            evidence_references=tuple(evidence),
        )

    def extract_text_pages(self, fetch_result: FetchResult) -> dict[int, str]:
        self.require_eligible(fetch_result)
        blocks = self._extract_with_fitz(fetch_result.content or b"")
        if not blocks:
            blocks = self._extract_with_pdfplumber(fetch_result.content or b"")
        pages: dict[int, list[str]] = defaultdict(list)
        for page, _, text in blocks:
            pages[page].append(text)
        return {page: "\n".join(texts) for page, texts in sorted(pages.items())}


PDFParser = PDFTextParser
