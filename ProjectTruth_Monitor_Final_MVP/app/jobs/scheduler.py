from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.services.scheduler import (
    ActiveJobGate,
    ProcessSchedulerLock,
    monitoring_job_deduplication_key,
    monitoring_window,
    next_run_at,
)


class LocalMonitoringScheduler:
    """Small APScheduler lifecycle wrapper suitable for FastAPI lifespan hooks."""

    def __init__(
        self,
        callback: Callable[[], Any],
        *,
        enabled: bool,
        lock_path: Path,
        interval_seconds: int = 60,
    ) -> None:
        if interval_seconds < 5:
            raise ValueError("Scheduler interval must be at least five seconds")
        self.callback = callback
        self.enabled = enabled
        self.interval_seconds = interval_seconds
        self.lock = ProcessSchedulerLock(lock_path)
        self._scheduler: Any | None = None

    @property
    def running(self) -> bool:
        return bool(self._scheduler and self._scheduler.running)

    def start(self) -> bool:
        if not self.enabled:
            return False
        if self.running:
            return True
        if not self.lock.acquire():
            return False
        try:
            from apscheduler.schedulers.asyncio import AsyncIOScheduler
        except ImportError as exc:
            self.lock.release()
            raise RuntimeError("APScheduler is required when ENABLE_SCHEDULER is true") from exc
        try:
            self._scheduler = AsyncIOScheduler(timezone="UTC")
            self._scheduler.add_job(
                self.callback,
                trigger="interval",
                seconds=self.interval_seconds,
                id="projecttruth-monitoring-dispatch",
                max_instances=1,
                coalesce=True,
                replace_existing=True,
            )
            self._scheduler.start()
            return True
        except Exception:
            self.lock.release()
            self._scheduler = None
            raise

    def stop(self, *, wait: bool = False) -> None:
        try:
            if self._scheduler and self._scheduler.running:
                self._scheduler.shutdown(wait=wait)
        finally:
            self._scheduler = None
            self.lock.release()


__all__ = [
    "ActiveJobGate",
    "LocalMonitoringScheduler",
    "ProcessSchedulerLock",
    "monitoring_job_deduplication_key",
    "monitoring_window",
    "next_run_at",
]

