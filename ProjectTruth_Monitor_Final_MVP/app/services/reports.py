from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from xml.sax.saxutils import escape as xml_escape

PRODUCT_DISCLAIMER = (
    "ProjectTruth Monitor is a public-source and user-authorized document monitoring tool for "
    "decision support. It does not replace legal due diligence, title verification, valuation, "
    "regulatory advice or professional property advice."
)


class ReportApprovalError(ValueError):
    def __init__(self, unresolved_ids: Sequence[str]) -> None:
        super().__init__("Client reports require resolution of every included review item.")
        self.unresolved_ids = tuple(unresolved_ids)


class ReportEvidenceError(ValueError):
    def __init__(self, missing_fields: Mapping[str, Sequence[str]]) -> None:
        super().__init__("Client reports require complete evidence provenance.")
        self.missing_fields = {
            str(identifier): tuple(fields)
            for identifier, fields in missing_fields.items()
        }


def escape_csv_formula(value: Any) -> str:
    """Neutralize spreadsheet formulas while retaining the visible source value."""

    if value is None:
        return ""
    if isinstance(value, (date, datetime)):
        text = value.isoformat()
    elif isinstance(value, Decimal):
        text = format(value, "f")
    else:
        text = str(value)
    dangerous_view = text.lstrip(" \t\r\n")
    if dangerous_view.startswith(("=", "+", "-", "@")):
        return f"'{text}"
    return text


csv_safe_value = escape_csv_formula
neutralize_csv_formula = escape_csv_formula


def _read(item: Mapping[str, Any] | Any, name: str, default: Any = None) -> Any:
    return item.get(name, default) if isinstance(item, Mapping) else getattr(item, name, default)


def _plain(value: Any) -> Any:
    if is_dataclass(value):
        return _plain(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_plain(item) for item in value]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if hasattr(value, "value") and isinstance(value.value, (str, int, float, bool)):
        return value.value
    return value


def export_csv(
    rows: Iterable[Mapping[str, Any]],
    *,
    fieldnames: Sequence[str] | None = None,
) -> bytes:
    materialized = [dict(row) for row in rows]
    if fieldnames is None:
        fieldnames = tuple(dict.fromkeys(key for row in materialized for key in row))
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=list(fieldnames), extrasaction="ignore", lineterminator="\r\n")
    writer.writeheader()
    for row in materialized:
        writer.writerow({key: escape_csv_formula(row.get(key)) for key in fieldnames})
    return output.getvalue().encode("utf-8-sig")


def export_json(payload: Any, *, indent: int = 2) -> bytes:
    return json.dumps(_plain(payload), indent=indent, sort_keys=True, ensure_ascii=False).encode("utf-8")


@dataclass(frozen=True, slots=True)
class EvidenceManifestRow:
    alert_id: str
    project_id: str
    old_snapshot_id: str
    new_snapshot_id: str
    old_content_hash: str
    new_content_hash: str
    source_reference: str | None
    page_reference: str | None
    capture_timestamp: str
    content_hash: str
    parser_version: str
    reviewer: str | None
    review_decision: str | None


def build_evidence_manifest(items: Iterable[Mapping[str, Any] | Any]) -> tuple[EvidenceManifestRow, ...]:
    rows: list[EvidenceManifestRow] = []
    for item in items:
        source_reference = (
            _read(item, "source_reference")
            or _read(item, "source_url")
            or _read(item, "evidence_reference")
        )
        if source_reference and (str(source_reference).startswith("file:") or "\\" in str(source_reference)):
            source_reference = None
        captured = _read(item, "capture_timestamp") or _read(item, "captured_at")
        reviewer = _read(item, "reviewer")
        review_decision = _read(item, "review_decision")
        rows.append(
            EvidenceManifestRow(
                alert_id=str(_read(item, "alert_id") or _read(item, "change_id") or ""),
                project_id=str(_read(item, "project_id") or ""),
                old_snapshot_id=str(_read(item, "old_snapshot_id") or ""),
                new_snapshot_id=str(_read(item, "new_snapshot_id") or ""),
                old_content_hash=str(_read(item, "old_content_hash") or ""),
                new_content_hash=str(
                    _read(item, "new_content_hash") or _read(item, "content_hash") or ""
                ),
                source_reference=str(source_reference) if source_reference else None,
                page_reference=(
                    str(_read(item, "page_reference"))
                    if _read(item, "page_reference")
                    else None
                ),
                capture_timestamp=captured.isoformat() if hasattr(captured, "isoformat") else str(captured or ""),
                content_hash=str(_read(item, "content_hash") or ""),
                parser_version=str(_read(item, "parser_version") or ""),
                reviewer=str(reviewer) if reviewer else None,
                review_decision=str(review_decision) if review_decision else None,
            )
        )
    return tuple(rows)


def validate_client_report(items: Iterable[Mapping[str, Any] | Any]) -> tuple[Any, ...]:
    materialized = tuple(items)
    unresolved: list[str] = []
    for item in materialized:
        requires_review = bool(_read(item, "requires_review", False))
        severity = str(_read(item, "severity", "")).lower()
        state = str(
            _read(item, "review_state", _read(item, "state", "pending"))
        ).lower()
        identifier = str(
            _read(item, "alert_id", _read(item, "id", _read(item, "change_id", "unknown")))
        )
        if (requires_review or severity in {"high", "critical"}) and state not in {
            "confirmed",
            "confirm",
            "expected",
            "mark_expected",
            "corrected",
            "correct",
        }:
            unresolved.append(identifier)
    if unresolved:
        raise ReportApprovalError(unresolved)
    return materialized


def validate_evidence_manifest(
    items: Iterable[Mapping[str, Any] | Any],
) -> tuple[EvidenceManifestRow, ...]:
    materialized = tuple(items)
    manifests = build_evidence_manifest(materialized)
    missing_by_alert: dict[str, tuple[str, ...]] = {}
    for item, manifest in zip(materialized, manifests, strict=True):
        missing = [
            field
            for field in (
                "source_reference",
                "old_snapshot_id",
                "new_snapshot_id",
                "capture_timestamp",
                "content_hash",
                "parser_version",
            )
            if not getattr(manifest, field)
        ]
        if bool(_read(item, "requires_review", False)):
            missing.extend(
                field
                for field in ("reviewer", "review_decision")
                if not getattr(manifest, field)
            )
        if missing:
            identifier = manifest.alert_id or str(_read(item, "alert_id", "unknown"))
            missing_by_alert[identifier] = tuple(missing)
    if missing_by_alert:
        raise ReportEvidenceError(missing_by_alert)
    return manifests


def weekly_digest_metrics(
    *,
    monitoring_runs: Iterable[Mapping[str, Any] | Any],
    signals: Iterable[Mapping[str, Any] | Any],
    alerts: Iterable[Mapping[str, Any] | Any],
) -> dict[str, int]:
    runs = tuple(monitoring_runs)
    signal_items = tuple(signals)
    alert_items = tuple(alerts)
    rule_codes = [str(_read(item, "rule_code", "")) for item in signal_items]
    return {
        "total_projects_checked": len(
            {
                str(_read(item, "project_id", ""))
                for item in runs
                if _read(item, "project_id")
            }
        ),
        "successful_checks": sum(
            str(_read(item, "status", "")).lower() in {"success", "succeeded"}
            for item in runs
        ),
        "degraded_or_unavailable_sources": sum(
            str(_read(item, "access_completeness", "")).lower() != "complete"
            for item in runs
        ),
        "confirmed_changes": sum(
            str(
                _read(item, "review_state", _read(item, "state", ""))
            ).lower()
            in {"confirmed", "confirm"}
            for item in alert_items
        ),
        "review_required_alerts": sum(
            bool(_read(item, "requires_review", False)) for item in alert_items
        ),
        "completion_date_changes": sum(code in {"COMPLETION_DATE_EXTENDED", "COMPLETION_DATE_MOVED_EARLIER"} for code in rule_codes),
        "qpr_lapsed_status_signals": sum(code in {"QPR_FLAG_OBSERVED", "QPR_FLAG_CLEARED", "PROJECT_MARKED_LAPSED", "PROJECT_STATUS_CHANGED"} for code in rule_codes),
        "new_phase_or_registration_events": rule_codes.count("NEW_PHASE_OR_REGISTRATION"),
        "brochure_or_source_conflicts": sum(code in {"OFFICIAL_VS_MARKETING_CONFLICT", "OFFICIAL_VS_DEVELOPER_CONFLICT"} for code in rule_codes),
    }


def export_pdf_report(
    *,
    title: str,
    sections: Iterable[tuple[str, Iterable[str]]],
    disclaimer: str = PRODUCT_DISCLAIMER,
) -> bytes:
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer
    except ImportError as exc:
        raise RuntimeError("reportlab is required for PDF report export") from exc
    output = io.BytesIO()
    document = SimpleDocTemplate(
        output,
        pagesize=A4,
        rightMargin=18 * mm,
        leftMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        title=title,
    )
    styles = getSampleStyleSheet()
    story: list[Any] = [Paragraph(xml_escape(title), styles["Title"]), Spacer(1, 8 * mm)]
    for heading, lines in sections:
        story.append(Paragraph(xml_escape(str(heading)), styles["Heading2"]))
        for line in lines:
            story.append(Paragraph(xml_escape(str(line)), styles["BodyText"]))
            story.append(Spacer(1, 2 * mm))
        story.append(Spacer(1, 3 * mm))
    story.extend((Paragraph("Limitations", styles["Heading2"]), Paragraph(xml_escape(disclaimer), styles["BodyText"])))
    document.build(story)
    return output.getvalue()


class ReportExporter:
    def csv(self, rows: Iterable[Mapping[str, Any]], *, fieldnames: Sequence[str] | None = None) -> bytes:
        return export_csv(rows, fieldnames=fieldnames)

    def json(self, payload: Any) -> bytes:
        return export_json(payload)

    def pdf(self, *, title: str, sections: Iterable[tuple[str, Iterable[str]]]) -> bytes:
        return export_pdf_report(title=title, sections=sections)
