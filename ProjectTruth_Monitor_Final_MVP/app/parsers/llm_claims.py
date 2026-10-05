from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class MarketingClaim:
    field_key: str
    raw_value: str
    normalized_value: Any
    document_id: str
    document_version: str
    page: int
    evidence_text: str
    coordinates: tuple[float, float, float, float] | None = None
    extraction_confidence: float = 0.0
    extraction_method: str = "llm_claim"
    validation_warnings: tuple[str, ...] = ()


class OptionalLLMClaimParser(ABC):
    provider_name = "base"
    provider_version = "0"

    @abstractmethod
    async def parse_claims(
        self,
        pages: Mapping[int, str],
        *,
        document_id: str,
        document_version: str,
    ) -> tuple[MarketingClaim, ...]:
        """Return schema-constrained claims; callers must never write them as official facts."""

        raise NotImplementedError


class DisabledLLMClaimParser(OptionalLLMClaimParser):
    provider_name = "disabled"
    provider_version = "1"

    async def parse_claims(
        self,
        pages: Mapping[int, str],
        *,
        document_id: str,
        document_version: str,
    ) -> tuple[MarketingClaim, ...]:
        return ()


LLMClaimParser = OptionalLLMClaimParser

