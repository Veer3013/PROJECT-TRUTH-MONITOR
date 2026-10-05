from __future__ import annotations

from datetime import datetime
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class MessageResponse(BaseModel):
    message: str


class DeleteResponse(BaseModel):
    deleted: bool
    idempotent: bool = True


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)


class AuditResponse(ORMModel):
    id: str
    action: str | None
    resource_type: str | None
    resource_id: str | None = None
    created_at: datetime
    metadata_json: dict[str, Any] | None = None
