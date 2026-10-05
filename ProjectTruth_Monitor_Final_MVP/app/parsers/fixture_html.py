from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from typing import Any

from app.domain import (
    EvidenceReference,
    ExtractedField,
    FetchResult,
    ParsedSourceSnapshot,
    ValidationStatus,
)
from app.services.normalization import canonical_field_key, normalize_field_value, normalize_text

from .base import LayoutChangeError, SourceParser


def _selector(element: Any) -> str:
    element_id = element.get("id")
    if element_id:
        return f"#{element_id}"
    classes = [str(value) for value in element.get("class", []) if str(value)]
    if classes:
        return f"{element.name}.{'.'.join(classes[:3])}"
    parent = getattr(element, "parent", None)
    if parent is not None and getattr(parent, "find_all", None):
        siblings = parent.find_all(element.name, recursive=False)
        try:
            position = siblings.index(element) + 1
        except ValueError:
            position = 1
        return f"{element.name}:nth-of-type({position})"
    return str(element.name or "html")


class FixtureHTMLParser(SourceParser):
    parser_name = "fixture_html"
    parser_version = "1.0.0"
    minimum_recognized_fields = 1
    required_core_fields: frozenset[str] = frozenset()

    def can_parse(self, fetch_result: FetchResult) -> bool:
        content_type = (fetch_result.content_type or "").lower()
        return fetch_result.content is not None and (
            "html" in content_type or b"<html" in fetch_result.content[:2_000].lower()
        )

    def _pairs(self, soup: Any) -> Iterable[tuple[str, str, Any, str | None]]:
        seen: set[int] = set()
        for element in soup.select("[data-field]"):
            seen.add(id(element))
            label = str(element.get("data-label") or element.get("data-field") or "")
            value = str(element.get("data-value") or element.get_text(" ", strip=True))
            href = element.get("href")
            yield label, str(href or value), element, href
        for row in soup.find_all("tr"):
            cells = row.find_all(["th", "td"], recursive=False)
            if len(cells) < 2:
                continue
            label_element, value_element = cells[0], cells[1]
            seen.add(id(label_element))
            seen.add(id(value_element))
            link = value_element.find("a", href=True)
            label = label_element.get_text(" ", strip=True)
            value = link.get("href") if link else value_element.get_text(" ", strip=True)
            yield label, str(value), value_element, link.get("href") if link else None
        for term in soup.find_all("dt"):
            description = term.find_next_sibling("dd")
            if description is None:
                continue
            seen.add(id(term))
            seen.add(id(description))
            link = description.find("a", href=True)
            value = link.get("href") if link else description.get_text(" ", strip=True)
            yield term.get_text(" ", strip=True), str(value), description, link.get("href") if link else None
        for container in soup.select(".field-row, .detail-row, .project-detail, .form-group"):
            if id(container) in seen:
                continue
            label_element = container.select_one(".label, label, .field-label, .title")
            value_element = container.select_one(".value, .field-value, .detail-value")
            if label_element is None or value_element is None:
                continue
            link = value_element.find("a", href=True)
            value = link.get("href") if link else value_element.get_text(" ", strip=True)
            yield label_element.get_text(" ", strip=True), str(value), value_element, link.get("href") if link else None

    def parse(self, fetch_result: FetchResult) -> ParsedSourceSnapshot:
        self.require_eligible(fetch_result)
        try:
            from bs4 import BeautifulSoup
        except ImportError as exc:
            raise RuntimeError("beautifulsoup4 is required for HTML parsing") from exc
        soup = BeautifulSoup(fetch_result.content or b"", "html.parser")
        parsed: list[ExtractedField] = []
        warnings: list[str] = []
        grouped_links: dict[str, list[tuple[str, Any]]] = defaultdict(list)
        seen_pairs: set[tuple[str, str]] = set()
        for raw_label, raw_value, element, _href in self._pairs(soup):
            explicit_key = element.get("data-field")
            key = canonical_field_key(str(explicit_key or raw_label))
            if key is None:
                continue
            if explicit_key and ":" in raw_value:
                prefix, candidate_value = raw_value.split(":", 1)
                if canonical_field_key(prefix) == key and normalize_text(candidate_value):
                    raw_label, raw_value = prefix, candidate_value
            pair_key = (key, normalize_text(raw_value))
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)
            if key in {"document_links", "official_notices"}:
                grouped_links[key].append((raw_value, element))
                continue
            normalized = normalize_field_value(key, raw_value)
            parsed.append(
                ExtractedField(
                    raw_label=normalize_text(raw_label),
                    raw_value=normalize_text(raw_value) or None,
                    canonical_field_key=key,
                    normalized_value=normalized.value,
                    unit=str(element.get("data-unit") or normalized.unit or "") or None,
                    selector=_selector(element),
                    extraction_method="html_label_value",
                    extraction_confidence=1.0 if not normalized.warnings else 0.65,
                    parser_name=self.parser_name,
                    parser_version=self.parser_version,
                    validation_warnings=normalized.warnings,
                )
            )
        for key, values in grouped_links.items():
            raw_values = [raw for raw, _ in values]
            normalized = normalize_field_value(key, raw_values)
            element = values[0][1]
            parsed.append(
                ExtractedField(
                    raw_label=key.replace("_", " ").title(),
                    raw_value="\n".join(raw_values),
                    canonical_field_key=key,
                    normalized_value=normalized.value,
                    selector=_selector(element),
                    extraction_method="html_link",
                    extraction_confidence=1.0 if normalized.value else 0.5,
                    parser_name=self.parser_name,
                    parser_version=self.parser_version,
                    validation_warnings=normalized.warnings,
                )
            )
        recognized = {field.canonical_field_key for field in parsed}
        if len(recognized) < self.minimum_recognized_fields:
            raise LayoutChangeError()
        missing_core = sorted(self.required_core_fields - recognized)
        status = ValidationStatus.VALID
        if missing_core:
            status = ValidationStatus.PARTIAL
            warnings.append(f"missing_core_fields:{','.join(missing_core)}")
        if any(field.validation_warnings for field in parsed):
            status = ValidationStatus.PARTIAL
        evidence = tuple(
            EvidenceReference(
                selector=field.selector,
                excerpt=field.raw_value,
                content_hash=fetch_result.content_hash,
            )
            for field in parsed
        )
        return ParsedSourceSnapshot(
            source_id=fetch_result.source_id,
            parser_name=self.parser_name,
            parser_version=self.parser_version,
            fields=tuple(parsed),
            validation_status=status,
            validation_warnings=tuple(warnings),
            evidence_references=evidence,
            source_date=(
                fetch_result.source_reported_date
                if hasattr(fetch_result.source_reported_date, "tzinfo")
                else None
            ),
        )


HTMLFixtureParser = FixtureHTMLParser
