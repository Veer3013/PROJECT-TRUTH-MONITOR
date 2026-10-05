from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any

from .enums import ValidationStatus
from .source import utc_now

SUPPORTED_PROJECT_FIELDS = frozenset(
    {
        "project_name",
        "registration_number",
        "promoter_or_developer",
        "project_type",
        "state",
        "city",
        "locality_or_sector",
        "registration_date",
        "proposed_completion_date",
        "revised_completion_date",
        "possession_date",
        "current_status",
        "lapsed_flag",
        "qpr_missing_flag",
        "phase",
        "building_count",
        "unit_count",
        "land_or_project_area",
        "official_notices",
        "document_links",
        "source_last_updated_date",
    }
)


@dataclass(frozen=True, slots=True)
class EvidenceReference:
    selector: str | None = None
    page: int | None = None
    coordinates: tuple[float, float, float, float] | None = None
    excerpt: str | None = None
    content_hash: str | None = None
    document_id: str | None = None
    document_version: str | None = None
    safe_reference: str | None = None

    def __post_init__(self) -> None:
        if self.page is not None and self.page < 1:
            raise ValueError("Evidence page numbers are 1-based")
        if self.coordinates is not None and len(self.coordinates) != 4:
            raise ValueError("Evidence coordinates must contain four numbers")


@dataclass(frozen=True, slots=True)
class ExtractedField:
    raw_label: str
    raw_value: str | None
    canonical_field_key: str
    normalized_value: Any
    unit: str | None = None
    selector: str | None = None
    page: int | None = None
    coordinates: tuple[float, float, float, float] | None = None
    extraction_method: str = "deterministic"
    extraction_confidence: float = 1.0
    parser_name: str = "unknown"
    parser_version: str = "0"
    validation_warnings: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not 0 <= self.extraction_confidence <= 1:
            raise ValueError("extraction_confidence must be between 0 and 1")
        if self.page is not None and self.page < 1:
            raise ValueError("page must be 1-based")
        object.__setattr__(self, "validation_warnings", tuple(self.validation_warnings))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    @property
    def field_key(self) -> str:
        return self.canonical_field_key

    @property
    def canonical_value(self) -> Any:
        return self.normalized_value

    def evidence_reference(self, *, content_hash: str | None = None) -> EvidenceReference:
        excerpt = self.raw_value
        if excerpt and len(excerpt) > 1_000:
            excerpt = f"{excerpt[:997]}..."
        return EvidenceReference(
            selector=self.selector,
            page=self.page,
            coordinates=self.coordinates,
            excerpt=excerpt,
            content_hash=content_hash,
        )


@dataclass(frozen=True, slots=True)
class ParsedSourceSnapshot:
    source_id: str
    parser_name: str
    parser_version: str
    fields: tuple[ExtractedField, ...]
    parsed_at: datetime = field(default_factory=utc_now)
    validation_status: ValidationStatus = ValidationStatus.VALID
    validation_warnings: tuple[str, ...] = ()
    evidence_references: tuple[EvidenceReference, ...] = ()
    source_date: datetime | None = None
    schema_version: str = "1.0"

    def __post_init__(self) -> None:
        object.__setattr__(self, "fields", tuple(self.fields))
        object.__setattr__(self, "validation_warnings", tuple(self.validation_warnings))
        object.__setattr__(self, "evidence_references", tuple(self.evidence_references))
        for extracted in self.fields:
            if extracted.parser_name != self.parser_name or extracted.parser_version != self.parser_version:
                raise ValueError("Every field must record the snapshot parser name and version")

    @property
    def is_usable(self) -> bool:
        return self.validation_status in {ValidationStatus.VALID, ValidationStatus.PARTIAL}

    def fields_by_key(self) -> dict[str, ExtractedField]:
        result: dict[str, ExtractedField] = {}
        for extracted in self.fields:
            current = result.get(extracted.canonical_field_key)
            if current is None or extracted.extraction_confidence > current.extraction_confidence:
                result[extracted.canonical_field_key] = extracted
        return result

    @property
    def structured_facts(self) -> dict[str, Any]:
        return {
            key: extracted.normalized_value
            for key, extracted in self.fields_by_key().items()
        }


@dataclass(frozen=True, slots=True)
class FactValue:
    field_key: str
    canonical_value: Any
    raw_value: str | None = None
    raw_label: str | None = None
    extraction_confidence: float = 1.0
    extraction_method: str = "deterministic"
    unit: str | None = None
    evidence: EvidenceReference | None = None

    @classmethod
    def from_extracted(cls, field: ExtractedField, content_hash: str | None = None) -> FactValue:
        return cls(
            field_key=field.canonical_field_key,
            canonical_value=field.normalized_value,
            raw_value=field.raw_value,
            raw_label=field.raw_label,
            extraction_confidence=field.extraction_confidence,
            extraction_method=field.extraction_method,
            unit=field.unit,
            evidence=field.evidence_reference(content_hash=content_hash),
        )
