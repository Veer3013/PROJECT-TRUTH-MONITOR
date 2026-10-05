from __future__ import annotations

import csv
import hashlib
import io
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from app.services.identity import ProjectIdentity, canonical_project_key
from app.services.normalization import normalize_registration_number, normalize_text

CSV_PROJECT_FIELDS = (
    "registration_number",
    "project_name",
    "developer_name",
    "city",
    "locality_or_sector",
    "official_url",
    "monitoring_cadence",
)


@dataclass(frozen=True, slots=True)
class CSVPreviewRow:
    row_number: int
    normalized: Mapping[str, Any]
    errors: tuple[str, ...]
    duplicate: bool
    identity_key: str | None

    @property
    def is_valid(self) -> bool:
        return not self.errors


@dataclass(frozen=True, slots=True)
class CSVImportPreview:
    rows: tuple[CSVPreviewRow, ...]
    content_hash: str

    @property
    def valid_count(self) -> int:
        return sum(row.is_valid for row in self.rows)

    @property
    def error_count(self) -> int:
        return sum(not row.is_valid for row in self.rows)

    @property
    def duplicate_count(self) -> int:
        return sum(row.duplicate for row in self.rows)


class CSVImportService:
    def __init__(self, *, max_rows: int = 5_000) -> None:
        self.max_rows = max_rows

    def preview(
        self,
        csv_content: str | bytes,
        *,
        existing_identity_keys: Iterable[str] = (),
        url_validator: Callable[[str], Any] | None = None,
    ) -> CSVImportPreview:
        raw_bytes = csv_content if isinstance(csv_content, bytes) else csv_content.encode("utf-8")
        try:
            text = raw_bytes.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("CSV must be UTF-8 encoded.") from exc
        reader = csv.DictReader(io.StringIO(text, newline=""))
        headers = tuple(normalize_text(item) for item in (reader.fieldnames or ()) if item)
        if not headers:
            raise ValueError("CSV header row is missing.")
        unsupported = sorted(set(headers) - set(CSV_PROJECT_FIELDS))
        if unsupported:
            raise ValueError(f"Unsupported columns: {', '.join(unsupported)}")
        seen = set(existing_identity_keys)
        results: list[CSVPreviewRow] = []
        for row_number, raw in enumerate(reader, start=2):
            if len(results) >= self.max_rows:
                raise ValueError(f"CSV imports are limited to {self.max_rows} rows.")
            normalized = {field: normalize_text(raw.get(field)) or None for field in CSV_PROJECT_FIELDS}
            if not any(normalized.values()):
                continue
            normalized["registration_number"] = normalize_registration_number(normalized["registration_number"])
            normalized["city"] = normalized["city"] or "Gurugram"
            normalized["monitoring_cadence"] = (normalized["monitoring_cadence"] or "weekly").lower()
            errors: list[str] = []
            if not normalized["registration_number"] and not normalized["project_name"]:
                errors.append("Provide registration_number or project_name.")
            if normalized["monitoring_cadence"] not in {"manual", "daily", "weekly"}:
                errors.append("monitoring_cadence must be manual, daily or weekly.")
            if normalized["official_url"] and url_validator:
                try:
                    url_validator(str(normalized["official_url"]))
                except Exception:
                    errors.append("official_url is unsupported or not approved by source policy.")
            identity = ProjectIdentity(
                project_id=f"preview-row-{row_number}",
                registration_number=normalized["registration_number"],
                project_name=normalized["project_name"],
                developer_name=normalized["developer_name"],
                city=normalized["city"],
                locality_or_sector=normalized["locality_or_sector"],
            )
            identity_key = canonical_project_key(identity)
            duplicate = bool(identity_key and identity_key in seen)
            if identity_key:
                seen.add(identity_key)
            results.append(CSVPreviewRow(row_number, normalized, tuple(errors), duplicate, identity_key))
        return CSVImportPreview(tuple(results), hashlib.sha256(raw_bytes).hexdigest())


preview_project_csv = CSVImportService().preview

