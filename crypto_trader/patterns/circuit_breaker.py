"""
Generic Circuit Breaker — wraps any callable to stop cascading failures.

States
------
  CLOSED    Normal operation; failures accumulate toward threshold.
  OPEN      All calls rejected immediately; protects downstream services.
  HALF_OPEN One probe call allowed after recovery_timeout; success → CLOSED,
            failure → back to OPEN (timer reset).

Usage — wrapping a call::

    cb = CircuitBreaker(name="coindcx_rest", failure_threshold=5, recovery_timeout=60)
    result = cb.call(my_api_fn, arg1, kwarg=val)

Usage — as a decorator::

    @CircuitBreaker(name="coindcx_rest", failure_threshold=5, recovery_timeout=60)
    def my_api_fn(): ...

Usage — manual admin::

    cb.trip()   # force OPEN
    cb.reset()  # force CLOSED (after human investigation)
"""
from __future__ import annotations

import logging
import threading
import time
from enum import Enum
from functools import wraps
from typing import Callable, Optional, Tuple, Type, Union

logger = logging.getLogger("crypto_trader.patterns.circuit_breaker")


class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(Exception):
    """Raised when a call is rejected because the circuit is OPEN."""


class CircuitBreaker:
    """Thread-safe generic circuit breaker."""

    def __init__(
        self,
        name: str = "",
        failure_threshold: int = 5,
        recovery_timeout: float = 60.0,
        expected_exception: Union[Type[Exception], Tuple[Type[Exception], ...]] = Exception,
    ) -> None:
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.expected_exception = expected_exception

        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._last_failure_time: Optional[float] = None
        self._lock = threading.Lock()

    # ── State ─────────────────────────────────────────────────────────────────

    @property
    def state(self) -> CircuitState:
        with self._lock:
            if (self._state == CircuitState.OPEN
                    and self._last_failure_time is not None
                    and time.monotonic() - self._last_failure_time >= self.recovery_timeout):
                self._state = CircuitState.HALF_OPEN
                logger.info("[CB:%s] HALF_OPEN — probing recovery", self.name)
            return self._state

    @property
    def is_open(self) -> bool:
        return self.state == CircuitState.OPEN

    @property
    def failure_count(self) -> int:
        return self._failure_count

    # ── Call entry ────────────────────────────────────────────────────────────

    def call(self, func: Callable, *args, **kwargs):
        """Execute *func* through the circuit breaker."""
        if self.state == CircuitState.OPEN:
            raise CircuitOpenError(
                f"Circuit '{self.name}' is OPEN — call rejected to prevent cascade"
            )
        try:
            result = func(*args, **kwargs)
            self._on_success()
            return result
        except self.expected_exception:
            self._on_failure()
            raise

    # ── Decorator usage ───────────────────────────────────────────────────────

    def __call__(self, func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs):
            return self.call(func, *args, **kwargs)
        return wrapper

    # ── Transitions ───────────────────────────────────────────────────────────

    def _on_success(self) -> None:
        with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                logger.info("[CB:%s] probe succeeded — CLOSED", self.name)
            self._failure_count = 0
            self._state = CircuitState.CLOSED

    def _on_failure(self) -> None:
        with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.monotonic()
            if (self._state == CircuitState.HALF_OPEN
                    or self._failure_count >= self.failure_threshold):
                logger.warning(
                    "[CB:%s] OPEN after %d failure(s)", self.name, self._failure_count
                )
                self._state = CircuitState.OPEN

    # ── Manual control ────────────────────────────────────────────────────────

    def reset(self) -> None:
        """Force CLOSED — use after human investigation confirms the issue is resolved."""
        with self._lock:
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            self._last_failure_time = None
        logger.info("[CB:%s] manually reset to CLOSED", self.name)

    def trip(self) -> None:
        """Force OPEN — use when an external health check detects a problem."""
        with self._lock:
            self._state = CircuitState.OPEN
            self._last_failure_time = time.monotonic()
        logger.warning("[CB:%s] manually tripped to OPEN", self.name)

    def __repr__(self) -> str:
        return (
            f"CircuitBreaker(name={self.name!r}, state={self._state.value}, "
            f"failures={self._failure_count}/{self.failure_threshold})"
        )
