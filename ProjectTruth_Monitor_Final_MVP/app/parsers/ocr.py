from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class OCRPage:
    page: int
    text: str
    confidence: float
    coordinates_available: bool = False


@dataclass(frozen=True, slots=True)
class OCRResult:
    provider_name: str
    provider_version: str
    pages: tuple[OCRPage, ...]
    available: bool
    warnings: tuple[str, ...] = ()

    @property
    def minimum_confidence(self) -> float | None:
        return min((page.confidence for page in self.pages), default=None)


class OCRProvider(ABC):
    provider_name = "base"
    provider_version = "0"

    @abstractmethod
    async def extract(self, pdf_content: bytes) -> OCRResult:
        raise NotImplementedError


class DisabledOCRProvider(OCRProvider):
    provider_name = "disabled"
    provider_version = "1"

    async def extract(self, pdf_content: bytes) -> OCRResult:
        return OCRResult(
            self.provider_name,
            self.provider_version,
            (),
            False,
            ("OCR is not configured; manual review is required.",),
        )

