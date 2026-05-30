"""
crypto_trader.infra.rate_limiter — process-global token-bucket REST limiter.

Each per-symbol engine has its own BinanceDataFeed; with many symbols their
independent retry/backoff loops can collectively blow past Binance's weight
budget (~1200/min) before any single feed sees a 429. A shared token bucket
throttles all REST calls proactively; the existing 429/418 backoff in
data_feed stays as the reactive safety net below it.

Weight-aware: callers pass the request weight (klines=1, most endpoints=1,
some heavier). Thread-safe; blocks until enough tokens accrue.
"""
from __future__ import annotations

import threading
import time
from typing import Optional


class TokenBucket:
    def __init__(self, rate_per_sec: float, capacity: float):
        """rate_per_sec: tokens refilled per second (e.g. 1200/60 = 20).
        capacity: max burst (bucket size)."""
        self.rate = float(rate_per_sec)
        self.capacity = float(capacity)
        self._tokens = float(capacity)
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def _refill_locked(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last
        if elapsed > 0:
            self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
            self._last = now

    def try_acquire(self, weight: float = 1.0) -> bool:
        """Non-blocking: take *weight* tokens if available, else False."""
        with self._lock:
            self._refill_locked()
            if self._tokens >= weight:
                self._tokens -= weight
                return True
            return False

    def acquire(self, weight: float = 1.0, timeout: Optional[float] = None) -> bool:
        """Block until *weight* tokens are available (or timeout). Returns True
        on acquisition, False on timeout."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._lock:
                self._refill_locked()
                if self._tokens >= weight:
                    self._tokens -= weight
                    return True
                # time until enough tokens accrue
                deficit = weight - self._tokens
                wait = deficit / self.rate if self.rate > 0 else 0.05
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                wait = min(wait, remaining)
            time.sleep(max(0.0, min(wait, 0.25)))

    @property
    def available(self) -> float:
        with self._lock:
            self._refill_locked()
            return self._tokens


# ── process-global singleton ─────────────────────────────────────────────────

_rest_limiter: Optional[TokenBucket] = None
_singleton_lock = threading.Lock()

# Binance USDⓈ-M weight budget is ~1200/min. Default to a conservative 1000/min
# (≈16.7/s) with a modest burst, leaving headroom for order placement.
_DEFAULT_RATE = 1000.0 / 60.0
_DEFAULT_CAPACITY = 40.0


def get_rest_limiter() -> TokenBucket:
    """Lazily build and return the shared REST limiter (overridable via env)."""
    global _rest_limiter
    if _rest_limiter is None:
        with _singleton_lock:
            if _rest_limiter is None:
                import os
                try:
                    rate = float(os.getenv("REST_RATE_PER_MIN", "1000")) / 60.0
                except (TypeError, ValueError):
                    rate = _DEFAULT_RATE
                try:
                    cap = float(os.getenv("REST_BURST_CAPACITY", str(_DEFAULT_CAPACITY)))
                except (TypeError, ValueError):
                    cap = _DEFAULT_CAPACITY
                _rest_limiter = TokenBucket(rate_per_sec=rate, capacity=cap)
    return _rest_limiter


def reset_rest_limiter() -> None:
    """Test hook: drop the singleton so the next get_rest_limiter rebuilds it."""
    global _rest_limiter
    with _singleton_lock:
        _rest_limiter = None
