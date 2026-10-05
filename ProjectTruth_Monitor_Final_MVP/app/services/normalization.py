from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit, urlunsplit

_WHITESPACE = re.compile(r"\s+")
_NUMBER = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?")
_DATE_FORMATS = (
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%d.%m.%Y",
    "%d %B %Y",
    "%d %b %Y",
    "%B %d, %Y",
    "%b %d, %Y",
)


FIELD_ALIASES: dict[str, str] = {
    "project name": "project_name",
    "name of project": "project_name",
    "registration no": "registration_number",
    "registration number": "registration_number",
    "rera registration no": "registration_number",
    "rera registration number": "registration_number",
    "promoter": "promoter_or_developer",
    "promoter name": "promoter_or_developer",
    "developer": "promoter_or_developer",
    "developer name": "promoter_or_developer",
    "project type": "project_type",
    "state": "state",
    "city": "city",
    "district": "city",
    "sector": "locality_or_sector",
    "locality": "locality_or_sector",
    "locality / sector": "locality_or_sector",
    "registration date": "registration_date",
    "proposed completion date": "proposed_completion_date",
    "completion date": "proposed_completion_date",
    "revised completion date": "revised_completion_date",
    "extended completion date": "revised_completion_date",
    "possession date": "possession_date",
    "possession": "possession_date",
    "status": "current_status",
    "project status": "current_status",
    "lapsed": "lapsed_flag",
    "lapsed flag": "lapsed_flag",
    "qpr missing": "qpr_missing_flag",
    "qpr missing flag": "qpr_missing_flag",
    "phase": "phase",
    "number of buildings": "building_count",
    "building count": "building_count",
    "number of towers": "building_count",
    "tower count": "building_count",
    "number of units": "unit_count",
    "unit count": "unit_count",
    "project area": "land_or_project_area",
    "land area": "land_or_project_area",
    "official notices": "official_notices",
    "official notice": "official_notices",
    "official_notice": "official_notices",
    "official_notices": "official_notices",
    "document links": "document_links",
    "last updated": "source_last_updated_date",
    "source last updated date": "source_last_updated_date",
    "source last updated": "source_last_updated_date",
    "source_last_updated": "source_last_updated_date",
}


DATE_FIELDS = frozenset(
    {
        "registration_date",
        "proposed_completion_date",
        "revised_completion_date",
        "source_last_updated_date",
        "possession_date",
        "completion_date",
    }
)
FLAG_FIELDS = frozenset({"lapsed_flag", "qpr_missing_flag"})
COUNT_FIELDS = frozenset({"building_count", "unit_count"})
URL_FIELDS = frozenset({"document_links", "official_notices"})


@dataclass(frozen=True, slots=True)
class NormalizedFieldValue:
    value: Any
    unit: str | None = None
    warnings: tuple[str, ...] = ()


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value))
    text = text.replace("\u2010", "-").replace("\u2011", "-").replace("\u2013", "-").replace("\u2014", "-")
    return _WHITESPACE.sub(" ", text).strip()


def canonical_field_key(label: str) -> str | None:
    normalized = normalize_text(label).lower().rstrip(":")
    if normalized in FIELD_ALIASES:
        return FIELD_ALIASES[normalized]
    safe = re.sub(r"[^a-z0-9]+", "_", normalized).strip("_")
    return safe if safe in set(FIELD_ALIASES.values()) else None


def normalize_registration_number(value: Any) -> str | None:
    text = normalize_text(value).upper()
    if not text or text in {"N/A", "NA", "NOT AVAILABLE", "-"}:
        return None
    text = re.sub(r"\s*([/-])\s*", r"\1", text)
    text = re.sub(r"-{2,}", "-", text)
    text = re.sub(r"/{2,}", "/", text)
    return text


def registration_identity_key(value: Any) -> str | None:
    normalized = normalize_registration_number(value)
    if normalized is None:
        return None
    return re.sub(r"\s+", "", normalized)


def normalize_name(value: Any) -> str | None:
    text = normalize_text(value)
    return text or None


def comparison_text(value: Any) -> str:
    text = normalize_text(value).casefold()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def parse_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = normalize_text(value)
    # A month/year does not carry enough information; unknown must remain unknown.
    if not re.search(r"\b\d{1,2}\b", text) or not re.search(r"\b\d{4}\b", text):
        return None
    text = re.sub(r"(?i)(\d{1,2})(st|nd|rd|th)\b", r"\1", text)
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def parse_boolean(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    text = comparison_text(value)
    if any(phrase in text for phrase in {"no pending", "not missing", "not lapsed", "flag cleared"}):
        return False
    if any(phrase in text for phrase in {"submission pending", "qpr pending", "qpr missing", "not submitted", "flag observed"}):
        return True
    if text in {"yes", "y", "true", "1", "present", "observed", "missing", "lapsed"}:
        return True
    if text in {"no", "n", "false", "0", "absent", "not observed", "available", "active"}:
        return False
    return None


def parse_integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    match = _NUMBER.search(normalize_text(value))
    if not match:
        return None
    try:
        number = Decimal(match.group(0).replace(",", ""))
    except InvalidOperation:
        return None
    if number != number.to_integral_value():
        return None
    return int(number)


def parse_area(value: Any) -> tuple[Decimal | None, str | None]:
    text = normalize_text(value)
    match = _NUMBER.search(text)
    if not match:
        return None, None
    try:
        number = Decimal(match.group(0).replace(",", ""))
    except InvalidOperation:
        return None, None
    rest = text[match.end() :].strip().lower().replace(".", "")
    unit_aliases = {
        "acre": "acre",
        "acres": "acre",
        "sq m": "sqm",
        "sqm": "sqm",
        "square metre": "sqm",
        "square metres": "sqm",
        "sq ft": "sqft",
        "sqft": "sqft",
        "hectare": "hectare",
        "hectares": "hectare",
    }
    return number, unit_aliases.get(rest, rest or None)


def normalize_url(value: Any) -> str | None:
    text = normalize_text(value)
    if not text:
        return None
    parsed = urlsplit(text)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return None
    path = parsed.path or "/"
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, parsed.query, ""))


def normalize_urls(value: Any) -> tuple[str, ...]:
    values: Iterable[Any]
    if isinstance(value, (tuple, list, set, frozenset)):
        values = value
    else:
        values = re.split(r"[\n,;]+", normalize_text(value))
    normalized = {url for item in values if (url := normalize_url(item)) is not None}
    return tuple(sorted(normalized))


def normalize_status(value: Any) -> str | None:
    text = comparison_text(value)
    if not text or text in {"na", "n a", "unknown", "not available"}:
        return None
    aliases = {
        "in progress": "under_construction",
        "under construction": "under_construction",
        "ongoing": "under_construction",
        "registered": "active",
        "registration active": "active",
        "complete": "completed",
        "cancelled": "cancelled",
        "canceled": "cancelled",
    }
    return aliases.get(text, text.replace(" ", "_"))


def normalize_field_value(field_key: str, raw_value: Any) -> NormalizedFieldValue:
    key = field_key.strip().lower()
    if raw_value is None or normalize_text(raw_value) in {"", "-", "--", "N/A", "NA"}:
        return NormalizedFieldValue(None)
    if key == "registration_number":
        return NormalizedFieldValue(normalize_registration_number(raw_value))
    if key in DATE_FIELDS:
        parsed = parse_date(raw_value)
        warning = () if parsed is not None else ("unrecognized_date",)
        return NormalizedFieldValue(parsed, warnings=warning)
    if key in FLAG_FIELDS or key.endswith("_flag"):
        parsed = parse_boolean(raw_value)
        warning = () if parsed is not None else ("unrecognized_boolean",)
        return NormalizedFieldValue(parsed, warnings=warning)
    if key in COUNT_FIELDS:
        parsed = parse_integer(raw_value)
        warning = () if parsed is not None else ("unrecognized_integer",)
        return NormalizedFieldValue(parsed, warnings=warning)
    if key == "land_or_project_area":
        number, unit = parse_area(raw_value)
        warning = () if number is not None else ("unrecognized_area",)
        return NormalizedFieldValue(number, unit=unit, warnings=warning)
    if key in URL_FIELDS:
        urls = normalize_urls(raw_value)
        return NormalizedFieldValue(urls, warnings=() if urls else ("invalid_or_missing_url",))
    if key == "current_status":
        return NormalizedFieldValue(normalize_status(raw_value))
    return NormalizedFieldValue(normalize_name(raw_value))


def normalize_mapping(raw_facts: dict[str, Any]) -> dict[str, Any]:
    return {key: normalize_field_value(key, value).value for key, value in raw_facts.items()}
