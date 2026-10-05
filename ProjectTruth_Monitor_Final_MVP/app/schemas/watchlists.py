from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl, field_validator

from app.core.config import get_settings
from app.schemas.common import ORMModel

Cadence = Literal["manual", "daily", "weekly"]


def _configured_default_cadence() -> Cadence:
    return get_settings().default_monitoring_cadence


class WatchlistCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    monitoring_cadence: Cadence = Field(default_factory=_configured_default_cadence)
    alert_preferences: dict[str, bool] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("name cannot be blank")
        return value


class WatchlistUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    monitoring_cadence: Cadence | None = None
    alert_preferences: dict[str, bool] | None = None


class WatchlistResponse(ORMModel):
    id: str
    organization_id: str
    name: str
    monitoring_cadence: str
    alert_preferences: dict[str, bool] | None = None
    created_at: datetime
    updated_at: datetime


class WatchlistItemCreate(BaseModel):
    registration_number: str | None = Field(default=None, max_length=100)
    project_id: str | None = None
    project_name: str | None = Field(default=None, max_length=240)
    developer_name: str | None = Field(default=None, max_length=240)
    city: str | None = Field(default="Gurugram", max_length=120)
    locality_or_sector: str | None = Field(default=None, max_length=160)
    official_url: HttpUrl | None = None
    monitoring_cadence: Cadence | None = None

    @field_validator("registration_number")
    @classmethod
    def normalize_registration(cls, value: str | None) -> str | None:
        return value.strip().upper() if value and value.strip() else None


class WatchlistItemResponse(ORMModel):
    id: str
    watchlist_id: str
    project_id: str
    monitoring_cadence: str
    status: str
    created_at: datetime


class CSVPreviewRequest(BaseModel):
    csv_text: str = Field(min_length=1, max_length=2_000_000)


class CSVPreviewRow(BaseModel):
    row_number: int
    normalized: dict[str, str | None]
    errors: list[str]
    duplicate: bool = False


class CSVPreviewResponse(BaseModel):
    preview_token: str
    rows: list[CSVPreviewRow]
    valid_count: int
    error_count: int


class CSVCommitRequest(BaseModel):
    watchlist_id: str
    preview_token: str
    idempotency_key: str = Field(min_length=8, max_length=200)
