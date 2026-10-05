from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.domain import AccessCompleteness, ConfidenceBand, SourceClass, ValidationStatus


@dataclass(frozen=True, slots=True)
class ConfidenceDimensions:
    source_reliability: ConfidenceBand
    source_access_completeness: ConfidenceBand
    project_identity_confidence: ConfidenceBand
    extraction_confidence: ConfidenceBand
    change_confidence: ConfidenceBand
    freshness: ConfidenceBand
    conflict_state: ConfidenceBand
    last_verified_at: datetime | None = None
    monitoring_message: str | None = None

    @property
    def requires_review(self) -> bool:
        return any(
            value in {
                ConfidenceBand.NEEDS_REVIEW,
                ConfidenceBand.UNVERIFIED,
                ConfidenceBand.CONFLICTING,
            }
            for value in (
                self.source_reliability,
                self.source_access_completeness,
                self.project_identity_confidence,
                self.extraction_confidence,
                self.change_confidence,
                self.freshness,
                self.conflict_state,
            )
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_reliability": self.source_reliability.value,
            "source_access_completeness": self.source_access_completeness.value,
            "project_identity_confidence": self.project_identity_confidence.value,
            "extraction_confidence": self.extraction_confidence.value,
            "change_confidence": self.change_confidence.value,
            "freshness": self.freshness.value,
            "conflict_state": self.conflict_state.value,
            "last_verified_at": self.last_verified_at.isoformat() if self.last_verified_at else None,
            "monitoring_message": self.monitoring_message,
        }


def confidence_band(score: float | None, *, confirmed: bool = False) -> ConfidenceBand:
    if score is None:
        return ConfidenceBand.UNVERIFIED
    if confirmed and score >= 0.99:
        return ConfidenceBand.CONFIRMED
    if score >= 0.85:
        return ConfidenceBand.HIGH
    if score >= 0.65:
        return ConfidenceBand.PROBABLE
    if score > 0:
        return ConfidenceBand.NEEDS_REVIEW
    return ConfidenceBand.UNVERIFIED


def assess_monitoring_confidence(
    *,
    source_class: SourceClass | str,
    access_completeness: AccessCompleteness | str,
    identity_confidence: float | None,
    extraction_confidence: float | None,
    change_confidence: float | None,
    captured_at: datetime | None,
    last_verified_at: datetime | None = None,
    validation_status: ValidationStatus | str = ValidationStatus.VALID,
    has_conflict: bool = False,
    now: datetime | None = None,
) -> ConfidenceDimensions:
    if isinstance(source_class, SourceClass):
        source = source_class
    else:
        source_value = str(source_class)
        try:
            source = SourceClass(source_value)
        except ValueError:
            source = {
                "user_authorized_document": SourceClass.USER_AUTHORIZED,
                "uploaded_document": SourceClass.USER_AUTHORIZED,
            }.get(source_value, SourceClass.UNKNOWN)
    completeness = (
        access_completeness
        if isinstance(access_completeness, AccessCompleteness)
        else AccessCompleteness(str(access_completeness))
    )
    validation = (
        validation_status
        if isinstance(validation_status, ValidationStatus)
        else ValidationStatus(str(validation_status))
    )
    reliability = {
        SourceClass.OFFICIAL_REGULATOR: ConfidenceBand.HIGH,
        SourceClass.REVIEWED_CORRECTION: ConfidenceBand.CONFIRMED,
        SourceClass.OFFICIAL_DEVELOPER: ConfidenceBand.PROBABLE,
        SourceClass.FIXTURE: ConfidenceBand.CONFIRMED,
        SourceClass.MARKETING_BROCHURE: ConfidenceBand.NEEDS_REVIEW,
        SourceClass.USER_AUTHORIZED: ConfidenceBand.NEEDS_REVIEW,
        SourceClass.UNKNOWN: ConfidenceBand.UNVERIFIED,
    }[source]
    access_band = {
        AccessCompleteness.COMPLETE: ConfidenceBand.HIGH,
        AccessCompleteness.PARTIAL: ConfidenceBand.NEEDS_REVIEW,
        AccessCompleteness.BLOCKED: ConfidenceBand.UNVERIFIED,
        AccessCompleteness.UNAVAILABLE: ConfidenceBand.UNVERIFIED,
    }[completeness]
    if validation != ValidationStatus.VALID:
        access_band = ConfidenceBand.NEEDS_REVIEW if validation == ValidationStatus.PARTIAL else ConfidenceBand.UNVERIFIED
    reference_now = now or datetime.now(UTC)
    if reference_now.tzinfo is None:
        reference_now = reference_now.replace(tzinfo=UTC)
    freshness = ConfidenceBand.UNVERIFIED
    if captured_at is not None:
        captured = captured_at if captured_at.tzinfo else captured_at.replace(tzinfo=UTC)
        age_days = max(0, (reference_now - captured).days)
        freshness = (
            ConfidenceBand.HIGH
            if age_days <= 30
            else ConfidenceBand.PROBABLE
            if age_days <= 90
            else ConfidenceBand.NEEDS_REVIEW
        )
    monitoring_message = None
    if completeness != AccessCompleteness.COMPLETE:
        verified = last_verified_at or captured_at
        suffix = verified.isoformat() if verified else "an unknown date"
        monitoring_message = f"Monitoring incomplete. Last verified on {suffix}."
    return ConfidenceDimensions(
        source_reliability=reliability,
        source_access_completeness=access_band,
        project_identity_confidence=confidence_band(identity_confidence, confirmed=identity_confidence == 1.0),
        extraction_confidence=confidence_band(extraction_confidence, confirmed=extraction_confidence == 1.0),
        change_confidence=confidence_band(change_confidence, confirmed=change_confidence == 1.0),
        freshness=freshness,
        conflict_state=ConfidenceBand.CONFLICTING if has_conflict else ConfidenceBand.CONFIRMED,
        last_verified_at=last_verified_at,
        monitoring_message=monitoring_message,
    )


assess_confidence = assess_monitoring_confidence
