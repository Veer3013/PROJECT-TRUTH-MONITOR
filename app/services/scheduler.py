from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import RLock
from typing import Any

from .idempotency import monitoring_run_key


def monitoring_window(timestamp: datetime, cadence: str) -> str:
    value = timestamp if timestamp.tzinfo else timestamp.replace(tzinfo=UTC)
    value = value.astimezone(UTC)
    if cadence == "daily":
        return value.strftime("%Y-%m-%d")
    if cadence == "weekly":
        year, week, _ = value.isocalendar()
        return f"{year}-W{week:02d}"
    if cadence == "manual":
        return value.isoformat(timespec="microseconds")
    raise ValueError("cadence must be manual, daily or weekly")


def monitoring_job_deduplication_key(
    *,
    organization_id: str,
    source_id: str,
    watchlist_item_id: str,
    scheduled_for: datetime,
    cadence: str,
) -> str:
    return monitoring_run_key(
        organization_id=organization_id,
        source_id=source_id,
        watchlist_item_id=watchlist_item_id,
        window=monitoring_window(scheduled_for, cadence),
    )


def next_run_at(last_run: datetime | None, cadence: str, *, now: datetime | None = None) -> datetime | None:
    if cadence == "manual":
        return None
    current = now or datetime.now(UTC)
    baseline = last_run or current
    if baseline.tzinfo is None:
        baseline = baseline.replace(tzinfo=UTC)
    delta = timedelta(days=1 if cadence == "daily" else 7 if cadence == "weekly" else 0)
    if not delta:
        raise ValueError("cadence must be manual, daily or weekly")
    candidate = baseline + delta
    while candidate <= current:
        candidate += delta
    return candidate


class ActiveJobGate:
    """Enforces one active job for a source/item/window in a single process."""

    def __init__(self) -> None:
        self._active: set[str] = set()
        self._cancelled: set[str] = set()
        self._lock = RLock()

    def start(self, key: str) -> bool:
        with self._lock:
            if key in self._active or key in self._cancelled:
                return False
            self._active.add(key)
            return True

    def finish(self, key: str) -> None:
        with self._lock:
            self._active.discard(key)

    def cancel(self, key: str) -> None:
        with self._lock:
            self._cancelled.add(key)
            self._active.discard(key)

    def is_cancelled(self, key: str) -> bool:
        with self._lock:
            return key in self._cancelled


@dataclass(slots=True)
class ProcessSchedulerLock:
    """Atomic lock-file guard used to prevent duplicate local APScheduler instances."""

    lock_path: Path
    stale_after_seconds: int = 86_400
    _acquired: bool = False

    def acquire(self) -> bool:
        path = self.lock_path.resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            age = datetime.now(UTC).timestamp() - path.stat().st_mtime
            if age > self.stale_after_seconds:
                path.unlink(missing_ok=True)
            else:
                return False
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            return False
        with os.fdopen(descriptor, "w", encoding="ascii") as stream:
            stream.write(str(os.getpid()))
        self._acquired = True
        return True

    def release(self) -> None:
        if self._acquired:
            self.lock_path.resolve().unlink(missing_ok=True)
            self._acquired = False

    def __enter__(self) -> ProcessSchedulerLock:
        if not self.acquire():
            raise RuntimeError("Another local scheduler process already holds the lock.")
        return self

    def __exit__(self, *_: Any) -> None:
        self.release()

