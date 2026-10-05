from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any

from .enums import ChangeType, ReviewState, Severity
from .source import utc_now


@dataclass(frozen=True, slots=True)
class DetectedChange:
    project_id: str
    field_key: str
    old_snapshot_id: str
    new_snapshot_id: str
    old_raw_value: str | None
    old_canonical_value: Any
    new_raw_value: str | None
    new_canonical_value: Any
    change_type: ChangeType
    magnitude: int | float | None
    confidence_dimensions: Mapping[str, Any]
    severity: Severity
    reason_codes: tuple[str, ...]
    requires_review: bool
    deduplication_key: str
    detected_at: datetime = field(default_factory=utc_now)
    review_state: ReviewState = ReviewState.PENDING
    change_id: str | None = None
    source_id: str | None = None
    limitations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "confidence_dimensions", MappingProxyType(dict(self.confidence_dimensions)))
        object.__setattr__(self, "reason_codes", tuple(self.reason_codes))
        object.__setattr__(self, "limitations", tuple(self.limitations))
        if not self.old_snapshot_id or not self.new_snapshot_id:
            raise ValueError("Every detected change must link old and new snapshot references")

    @property
    def magnitude_days(self) -> int | None:
        return int(self.magnitude) if isinstance(self.magnitude, int) else None

    @property
    def old_value(self) -> Any:
        return self.old_canonical_value

    @property
    def new_value(self) -> Any:
        return self.new_canonical_value


__all__ = ["DetectedChange"]

