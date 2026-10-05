from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from threading import RLock
from typing import Any, Generic, TypeVar

T = TypeVar("T")


class IdempotencyConflict(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class IdempotentResult(Generic[T]):
    value: T
    replayed: bool
    idempotency_key: str


def request_fingerprint(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class IdempotencyRegistry(Generic[T]):
    """Reference atomic idempotency registry; hosted adapters can use a DB unique key."""

    def __init__(self) -> None:
        self._records: dict[str, tuple[str, T]] = {}
        self._lock = RLock()

    def execute(
        self,
        key: str,
        payload: Any,
        operation: Callable[[], T],
    ) -> IdempotentResult[T]:
        if not key or len(key) > 255:
            raise ValueError("A non-empty idempotency key of at most 255 characters is required.")
        fingerprint = request_fingerprint(payload)
        with self._lock:
            existing = self._records.get(key)
            if existing:
                if existing[0] != fingerprint:
                    raise IdempotencyConflict("The idempotency key was already used for different input.")
                return IdempotentResult(existing[1], True, key)
            value = operation()
            self._records[key] = (fingerprint, value)
            return IdempotentResult(value, False, key)


def monitoring_run_key(
    *,
    organization_id: str,
    source_id: str,
    watchlist_item_id: str,
    window: str,
) -> str:
    value = f"{organization_id}|{source_id}|{watchlist_item_id}|{window}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()

