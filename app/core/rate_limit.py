from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

from fastapi import HTTPException, status


class SlidingWindowLimiter:
    """Small in-process limiter for local mode.

    Hosted deployments should replace this with a shared Redis-backed implementation.
    """

    def __init__(self) -> None:
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str, *, limit: int, window_seconds: int = 60) -> None:
        now = time.monotonic()
        cutoff = now - window_seconds
        with self._lock:
            events = self._events[key]
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= limit:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail={
                        "code": "rate_limited",
                        "message": "Too many requests. Please wait and try again.",
                    },
                    headers={"Retry-After": str(window_seconds)},
                )
            events.append(now)

    def clear(self) -> None:
        with self._lock:
            self._events.clear()


rate_limiter = SlidingWindowLimiter()

