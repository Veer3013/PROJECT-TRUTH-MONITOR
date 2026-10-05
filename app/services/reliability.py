from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from threading import RLock
from typing import Generic, TypeVar

from app.domain import ErrorCategory, FetchResult

T = TypeVar("T")


NON_RETRYABLE_CATEGORIES = frozenset(
    {
        ErrorCategory.UNSUPPORTED_URL,
        ErrorCategory.POLICY_BLOCKED,
        ErrorCategory.AUTHENTICATION_REQUIRED,
        ErrorCategory.CAPTCHA,
        ErrorCategory.ACCESS_RESTRICTED,
        ErrorCategory.LAYOUT_CHANGED,
        ErrorCategory.PDF_UNREADABLE,
    }
)
RETRYABLE_CATEGORIES = frozenset(
    {
        ErrorCategory.NETWORK,
        ErrorCategory.TIMEOUT,
        ErrorCategory.RATE_LIMITED,
    }
)


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay_seconds: float = 1.0
    maximum_delay_seconds: float = 30.0
    jitter_ratio: float = 0.2

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least one")
        if self.base_delay_seconds < 0 or self.maximum_delay_seconds < self.base_delay_seconds:
            raise ValueError("Retry delays are invalid")
        if not 0 <= self.jitter_ratio <= 1:
            raise ValueError("jitter_ratio must be between zero and one")

    def delay(self, completed_attempt: int, *, random_value: float | None = None) -> float:
        if completed_attempt < 1:
            raise ValueError("completed_attempt must be positive")
        base = min(self.maximum_delay_seconds, self.base_delay_seconds * (2 ** (completed_attempt - 1)))
        sample = random.random() if random_value is None else random_value  # noqa: S311
        jitter = ((sample * 2) - 1) * self.jitter_ratio * base
        return max(0.0, base + jitter)


@dataclass(frozen=True, slots=True)
class RetryOutcome(Generic[T]):
    value: T
    attempts: int


async def retry_async(
    operation: Callable[[], Awaitable[T]],
    *,
    policy: RetryPolicy = RetryPolicy(),
    should_retry: Callable[[Exception], bool] | None = None,
    sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> RetryOutcome[T]:
    predicate = should_retry or (lambda exc: getattr(exc, "code", None) in {item.value for item in RETRYABLE_CATEGORIES})
    last_error: Exception | None = None
    for attempt in range(1, policy.max_attempts + 1):
        try:
            return RetryOutcome(await operation(), attempt)
        except Exception as exc:
            last_error = exc
            if attempt >= policy.max_attempts or not predicate(exc):
                raise
            await sleeper(policy.delay(attempt))
    assert last_error is not None
    raise last_error


async def retry_fetch(
    operation: Callable[[], Awaitable[FetchResult]],
    *,
    policy: RetryPolicy = RetryPolicy(),
    sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> RetryOutcome[FetchResult]:
    result: FetchResult | None = None
    for attempt in range(1, policy.max_attempts + 1):
        result = await operation()
        if result.is_successful or result.error_category not in RETRYABLE_CATEGORIES:
            return RetryOutcome(result, attempt)
        if attempt < policy.max_attempts:
            await sleeper(policy.delay(attempt))
    assert result is not None
    return RetryOutcome(result, policy.max_attempts)


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(RuntimeError):
    pass


class CircuitBreaker:
    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        recovery_timeout_seconds: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if failure_threshold < 1 or recovery_timeout_seconds < 0:
            raise ValueError("Circuit-breaker configuration is invalid")
        self.failure_threshold = failure_threshold
        self.recovery_timeout_seconds = recovery_timeout_seconds
        self._clock = clock
        self._state = CircuitState.CLOSED
        self._failures = 0
        self._opened_at: float | None = None
        self._half_open_in_flight = False
        self._lock = RLock()

    @property
    def state(self) -> CircuitState:
        with self._lock:
            if self._state == CircuitState.OPEN and self._opened_at is not None:
                if self._clock() - self._opened_at >= self.recovery_timeout_seconds:
                    self._state = CircuitState.HALF_OPEN
                    self._half_open_in_flight = False
            return self._state

    @property
    def consecutive_failures(self) -> int:
        return self._failures

    def allow_request(self) -> bool:
        with self._lock:
            state = self.state
            if state == CircuitState.CLOSED:
                return True
            if state == CircuitState.OPEN:
                return False
            if self._half_open_in_flight:
                return False
            self._half_open_in_flight = True
            return True

    def require_request(self) -> None:
        if not self.allow_request():
            raise CircuitOpenError("The source circuit is open.")

    def record_success(self) -> None:
        with self._lock:
            self._state = CircuitState.CLOSED
            self._failures = 0
            self._opened_at = None
            self._half_open_in_flight = False

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            self._half_open_in_flight = False
            if self._failures >= self.failure_threshold:
                self._state = CircuitState.OPEN
                self._opened_at = self._clock()

    def reset_after_review(self) -> None:
        self.record_success()
