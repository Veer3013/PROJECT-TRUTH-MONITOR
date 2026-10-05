from __future__ import annotations

from abc import ABC, abstractmethod

from app.domain import ExtractedField, FetchResult, ParsedSourceSnapshot


class ParserError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ParserIneligibleError(ParserError):
    def __init__(self, message: str = "The fetch result is not eligible for this parser.") -> None:
        super().__init__("parser_ineligible", message)


class LayoutChangeError(ParserError):
    def __init__(self, message: str = "No recognized source fields were found.") -> None:
        super().__init__("layout_changed", message)


class EncryptedPDFError(ParserError):
    def __init__(self) -> None:
        super().__init__("encrypted_pdf", "Password-protected or encrypted PDFs are not supported.")


class SourceParser(ABC):
    parser_name = "base"
    parser_version = "0"

    @abstractmethod
    def can_parse(self, fetch_result: FetchResult) -> bool:
        raise NotImplementedError

    @abstractmethod
    def parse(self, fetch_result: FetchResult) -> ParsedSourceSnapshot:
        raise NotImplementedError

    def require_eligible(self, fetch_result: FetchResult) -> None:
        if not fetch_result.parser_eligible or not self.can_parse(fetch_result):
            raise ParserIneligibleError()


__all__ = [
    "EncryptedPDFError",
    "ExtractedField",
    "LayoutChangeError",
    "ParsedSourceSnapshot",
    "ParserError",
    "ParserIneligibleError",
    "SourceParser",
]

