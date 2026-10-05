from __future__ import annotations

from dataclasses import dataclass

USER_MESSAGES: dict[str, str] = {
    "unsupported_url": (
        "This source is not supported yet. Add a supported official URL or upload an "
        "authorized document."
    ),
    "access_restricted": (
        "Automatic access is blocked. No data was changed. You may upload an authorized copy."
    ),
    "rate_limited": (
        "The source could not be checked yet. Last verified data remains visible with its date."
    ),
    "timeout": (
        "The source could not be checked yet. Last verified data remains visible with its date."
    ),
    "layout_changed": (
        "The source format changed. This check needs maintenance; no factual alert was generated."
    ),
    "pdf_unreadable": (
        "We could not reliably read this document. Upload a clearer copy or review it manually."
    ),
    "identity_conflict": (
        "Multiple projects may match. Confirm the correct registration before monitoring."
    ),
    "source_conflict": (
        "Current sources disagree. Review both records before using this value."
    ),
    "value_removed": (
        "A previously displayed value is no longer present. This may be a source change and "
        "requires review."
    ),
    "partial_snapshot": "Only part of the source was readable. Some fields could not be verified.",
}


@dataclass
class DomainError(Exception):
    code: str
    message: str | None = None
    status_code: int = 400

    def __str__(self) -> str:
        return self.message or USER_MESSAGES.get(self.code, "The request could not be completed.")


class NotFoundError(DomainError):
    def __init__(self, resource: str = "resource") -> None:
        super().__init__("not_found", f"The requested {resource} was not found.", 404)


class ConflictError(DomainError):
    def __init__(self, message: str) -> None:
        super().__init__("conflict", message, 409)

