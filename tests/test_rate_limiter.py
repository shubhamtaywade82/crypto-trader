"""Phase 5 — shared token-bucket REST rate limiter."""
import threading
import time

from crypto_trader.infra.rate_limiter import (
    TokenBucket, get_rest_limiter, reset_rest_limiter,
)


def test_try_acquire_until_empty():
    tb = TokenBucket(rate_per_sec=0, capacity=3)
    assert tb.try_acquire()
    assert tb.try_acquire()
    assert tb.try_acquire()
    assert not tb.try_acquire()  # bucket empty, no refill


def test_refill_over_time():
    tb = TokenBucket(rate_per_sec=100, capacity=2)
    assert tb.try_acquire(2)        # drain
    assert not tb.try_acquire(1)
    time.sleep(0.05)                # ~5 tokens refill at 100/s, capped at 2
    assert tb.try_acquire(1)


def test_acquire_blocks_then_succeeds():
    tb = TokenBucket(rate_per_sec=50, capacity=1)
    assert tb.try_acquire(1)        # drain
    start = time.monotonic()
    assert tb.acquire(1, timeout=1.0) is True
    assert time.monotonic() - start >= 0.01  # waited for refill


def test_acquire_timeout_returns_false():
    tb = TokenBucket(rate_per_sec=0, capacity=1)
    assert tb.try_acquire(1)
    assert tb.acquire(1, timeout=0.1) is False


def test_observed_rate_under_limit():
    """Hammer from many threads; total grants must not exceed capacity + refill."""
    tb = TokenBucket(rate_per_sec=100, capacity=10)
    grants = []
    lock = threading.Lock()

    def worker():
        for _ in range(20):
            if tb.try_acquire(1):
                with lock:
                    grants.append(time.monotonic())

    threads = [threading.Thread(target=worker) for _ in range(8)]
    t0 = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.monotonic() - t0
    # upper bound: initial capacity + refill over elapsed window (+1 slack)
    max_expected = 10 + elapsed * 100 + 1
    assert len(grants) <= max_expected


def test_singleton_identity_and_reset():
    reset_rest_limiter()
    a = get_rest_limiter()
    b = get_rest_limiter()
    assert a is b
    reset_rest_limiter()
    c = get_rest_limiter()
    assert c is not a
    reset_rest_limiter()
