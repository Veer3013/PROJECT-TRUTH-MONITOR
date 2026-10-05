from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from .enums import ReviewRequirement, Severity
from .source import utc_now


class SignalRuleCode(StrEnum):
    COMPLETION_DATE_EXTENDED = "COMPLETION_DATE_EXTENDED"
    COMPLETION_DATE_MOVED_EARLIER = "COMPLETION_DATE_MOVED_EARLIER"
    REPEATED_EXTENSION = "REPEATED_EXTENSION"
    PROJECT_MARKED_LAPSED = "PROJECT_MARKED_LAPSED"
    PROJECT_STATUS_CHANGED = "PROJECT_STATUS_CHANGED"
    QPR_FLAG_OBSERVED = "QPR_FLAG_OBSERVED"
    QPR_FLAG_CLEARED = "QPR_FLAG_CLEARED"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    SOURCE_MONITORING_DEGRADED = "SOURCE_MONITORING_DEGRADED"
    IMPORTANT_FIELD_REMOVED = "IMPORTANT_FIELD_REMOVED"
    NEW_PHASE_OR_REGISTRATION = "NEW_PHASE_OR_REGISTRATION"
    OFFICIAL_VS_MARKETING_CONFLICT = "OFFICIAL_VS_MARKETING_CONFLICT"
    OFFICIAL_VS_DEVELOPER_CONFLICT = "OFFICIAL_VS_DEVELOPER_CONFLICT"
    EXACT_LINKED_NOTICE_ADDED = "EXACT_LINKED_NOTICE_ADDED"
    VALUE_REVERSED = "VALUE_REVERSED"


@dataclass(frozen=True, slots=True)
class Signal:
    project_id: str
    rule_code: SignalRuleCode
    rule_version: str
    supporting_change_ids: tuple[str, ...]
    observed_facts: Mapping[str, Any]
    severity: Severity
    confidence: float | None
    confidence_dimensions: Mapping[str, Any]
    limitations: tuple[str, ...]
    required_review_level: ReviewRequirement
    deduplication_key: str
    detected_at: datetime = field(default_factory=utc_now)
    primary_detected_change_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "supporting_change_ids", tuple(self.supporting_change_ids))
        object.__setattr__(self, "observed_facts", MappingProxyType(dict(self.observed_facts)))
        object.__setattr__(self, "confidence_dimensions", MappingProxyType(dict(self.confidence_dimensions)))
        object.__setattr__(self, "limitations", tuple(self.limitations))
        if self.confidence is not None and not 0 <= self.confidence <= 1:
            raise ValueError("Signal confidence must be between 0 and 1")

    @property
    def signal_id(self) -> str:
        return hashlib.sha256(f"signal|{self.deduplication_key}".encode()).hexdigest()[:32]


__all__ = ["Signal", "SignalRuleCode"]

