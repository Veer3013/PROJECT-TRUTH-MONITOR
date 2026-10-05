from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.domain import (
    FetchRequestContext,
    FetchResult,
    RequestContext,
    SourceAccessPolicy,
    SourceHealth,
    SourceHealthStatus,
    SourcePolicy,
)


class CollectorError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class CollectorPolicyError(CollectorError):
    pass


@dataclass(slots=True)
class CollectorMetrics:
    attempts: int = 0
    successes: int = 0
    parser_eligible_fetches: int = 0
    total_latency_ms: int = 0
    consecutive_failures: int = 0
    degradation_incidents: int = 0
    last_successful_fetch: datetime | None = None
    last_content_hash: str | None = None

    @property
    def success_rate(self) -> float | None:
        return self.successes / self.attempts if self.attempts else None

    @property
    def average_latency_ms(self) -> float | None:
        return self.total_latency_ms / self.attempts if self.attempts else None


class SourceCollector(ABC):
    """Policy-aware source collector contract.

    Implementations return explicit blocked/unavailable results for expected source
    failures. They raise only for programmer errors or an invalid access policy.
    """

    collector_name = "base"

    @abstractmethod
    def can_handle(self, source: Any) -> bool:
        raise NotImplementedError

    @abstractmethod
    def validate_policy(self, source_policy: SourceAccessPolicy) -> None:
        raise NotImplementedError

    @abstractmethod
    async def fetch(self, request_context: FetchRequestContext) -> FetchResult:
        raise NotImplementedError

    @abstractmethod
    def health_check(self) -> SourceHealth:
        raise NotImplementedError


__all__ = [
    "CollectorError",
    "CollectorMetrics",
    "CollectorPolicyError",
    "FetchRequestContext",
    "FetchResult",
    "RequestContext",
    "SourceAccessPolicy",
    "SourceCollector",
    "SourceHealth",
    "SourceHealthStatus",
    "SourcePolicy",
]

