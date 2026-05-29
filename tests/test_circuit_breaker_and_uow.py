"""
Integration tests:
  1. LedgerUnitOfWork — atomic commit/rollback + event dispatch
  2. CoinDCXClient circuit breaker — trips on venue failures, fast-fails when open
"""
import pytest
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch, call
import time

from crypto_trader.ledger.db import LedgerDB
from crypto_trader.ledger.unit_of_work import LedgerUnitOfWork
from crypto_trader.patterns.circuit_breaker import CircuitBreaker, CircuitOpenError, CircuitState


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def db(tmp_path):
    d = LedgerDB(sqlite_path=str(tmp_path / "test_uow.db"))
    d.bootstrap()
    return d


# ─────────────────────────────────────────────────────────────────────────────
# LedgerUnitOfWork
# ─────────────────────────────────────────────────────────────────────────────

class TestLedgerUnitOfWork:

    def test_context_manager_commits_on_clean_exit(self, db):
        with LedgerUnitOfWork(db) as uow:
            uow.cursor.execute(
                "INSERT INTO currency_accounts (user_id, currency, ledger_balance, "
                "available_balance, locked_balance, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("test", "USDT", "1000", "1000", "0", 1),
            )
        # After commit, the row should persist
        with db.cursor() as cur:
            cur.execute("SELECT ledger_balance FROM currency_accounts WHERE user_id='test'")
            row = cur.fetchone()
        assert row is not None
        assert row[0] == "1000"

    def test_context_manager_rolls_back_on_exception(self, db):
        with pytest.raises(ValueError):
            with LedgerUnitOfWork(db) as uow:
                uow.cursor.execute(
                    "INSERT INTO currency_accounts (user_id, currency, ledger_balance, "
                    "available_balance, locked_balance, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    ("rollback_user", "USDT", "999", "999", "0", 1),
                )
                raise ValueError("something went wrong")
        # Row should NOT be in DB after rollback
        with db.cursor() as cur:
            cur.execute("SELECT * FROM currency_accounts WHERE user_id='rollback_user'")
            row = cur.fetchone()
        assert row is None

    def test_events_dispatched_after_commit(self, db):
        bus = MagicMock()

        class _Evt:
            pass

        evt = _Evt()
        with LedgerUnitOfWork(db, event_bus=bus) as uow:
            uow.collect_event(evt)

        bus.publish.assert_called_once_with(evt)

    def test_events_not_dispatched_after_rollback(self, db):
        bus = MagicMock()

        with pytest.raises(RuntimeError):
            with LedgerUnitOfWork(db, event_bus=bus) as uow:
                uow.collect_event("should_not_dispatch")
                raise RuntimeError("boom")

        bus.publish.assert_not_called()

    def test_multiple_events_all_dispatched(self, db):
        bus = MagicMock()
        with LedgerUnitOfWork(db, event_bus=bus) as uow:
            uow.collect_event("evt_1")
            uow.collect_event("evt_2")
            uow.collect_event("evt_3")

        assert bus.publish.call_count == 3

    def test_cursor_raises_before_begin(self, db):
        uow = LedgerUnitOfWork(db)
        with pytest.raises(RuntimeError, match="not started"):
            _ = uow.cursor

    def test_redis_invalidation_called_on_commit(self, db):
        redis = MagicMock()
        redis.delete_position = MagicMock()

        class _Evt:
            position_uid = "uid-123"
            user_id = "u1"
            venue = "binance"

        with LedgerUnitOfWork(db, redis_layer=redis) as uow:
            uow.collect_event(_Evt())

        redis.delete_position.assert_called_once_with("uid-123")

    def test_redis_invalidation_skipped_if_no_redis(self, db):
        """Should not raise when redis_layer is None."""
        with LedgerUnitOfWork(db) as uow:
            uow.collect_event("anything")

    def test_event_bus_failure_does_not_reraise(self, db):
        """A broken event bus should log but not abort after a successful commit."""
        bus = MagicMock()
        bus.publish.side_effect = RuntimeError("bus dead")
        # commit should succeed even if event dispatch fails
        with LedgerUnitOfWork(db, event_bus=bus) as uow:
            uow.collect_event("evt")
        # We reach here — no exception propagated


# ─────────────────────────────────────────────────────────────────────────────
# CoinDCXClient circuit breaker
# ─────────────────────────────────────────────────────────────────────────────

def _make_client(cb=None, max_retries=1):
    """Build a CoinDCXClient with a mock requests.Session."""
    from crypto_trader.exchanges.coindcx_client import CoinDCXClient
    client = CoinDCXClient(
        api_key="k", api_secret="s",
        max_retries=max_retries, backoff_base=0.01,
        circuit_breaker=cb,
    )
    client.session = MagicMock()
    return client


class TestCoinDCXClientCircuitBreaker:

    def test_successful_call_records_success(self):
        cb = CircuitBreaker(name="test", failure_threshold=3)
        client = _make_client(cb)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"ok": True}
        mock_resp.headers = {}
        client.session.request.return_value = mock_resp

        result = client.get_public("/exchange/ticker")
        assert result == {"ok": True}
        assert cb.state == CircuitState.CLOSED
        assert cb.failure_count == 0

    def test_5xx_failures_trip_circuit(self):
        # threshold=1: one exhausted-retry sequence trips the circuit
        cb = CircuitBreaker(name="test", failure_threshold=1, recovery_timeout=60)
        client = _make_client(cb, max_retries=2)

        mock_resp = MagicMock()
        mock_resp.status_code = 503
        mock_resp.text = "service unavailable"
        mock_resp.headers = {}
        client.session.request.return_value = mock_resp

        from crypto_trader.exchanges.coindcx_client import CoinDCXError
        with pytest.raises(CoinDCXError):
            client.get_public("/exchange/ticker")

        assert cb.is_open

    def test_open_circuit_fast_fails_without_network_call(self):
        cb = CircuitBreaker(name="test", failure_threshold=1, recovery_timeout=60)
        cb.trip()  # force open
        client = _make_client(cb)

        with pytest.raises(CircuitOpenError):
            client.get_public("/exchange/ticker")

        # Session.request should never have been called
        client.session.request.assert_not_called()

    def test_4xx_does_not_trip_circuit(self):
        cb = CircuitBreaker(name="test", failure_threshold=3)
        client = _make_client(cb)

        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.text = "bad request"
        mock_resp.headers = {}
        mock_resp.json.side_effect = ValueError
        client.session.request.return_value = mock_resp

        from crypto_trader.exchanges.coindcx_client import CoinDCXError
        with pytest.raises(CoinDCXError):
            client.get_public("/exchange/ticker")

        # 4xx = client bug, not venue outage — CB stays CLOSED
        assert cb.state == CircuitState.CLOSED
        assert cb.failure_count == 0

    def test_429_does_not_permanently_trip_circuit(self):
        """Rate limiting is transient — the CB should not open from 429 alone."""
        cb = CircuitBreaker(name="test", failure_threshold=3)
        client = _make_client(cb, max_retries=1)

        # First call: 429 (non-retryable path for retry_safe=False)
        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_429.text = "rate limited"
        resp_429.headers = {"Retry-After": "0.01"}
        client.session.request.return_value = resp_429

        from crypto_trader.exchanges.coindcx_client import CoinDCXError
        with pytest.raises(CoinDCXError):
            client.post_signed("/some/endpoint", retry_safe=False)

        assert cb.state == CircuitState.CLOSED

    def test_network_error_trips_circuit_after_threshold(self):
        cb = CircuitBreaker(
            name="test", failure_threshold=2, recovery_timeout=60,
            expected_exception=Exception,
        )
        client = _make_client(cb, max_retries=1)
        client.session.request.side_effect = OSError("connection refused")

        for _ in range(2):
            try:
                client.get_public("/exchange/ticker")
            except Exception:
                pass

        assert cb.is_open

    def test_circuit_half_open_allows_probe(self):
        cb = CircuitBreaker(name="test", failure_threshold=1, recovery_timeout=0.05)
        client = _make_client(cb)

        # Trip
        mock_fail = MagicMock()
        mock_fail.status_code = 500
        mock_fail.text = "err"
        mock_fail.headers = {}
        client.session.request.return_value = mock_fail

        from crypto_trader.exchanges.coindcx_client import CoinDCXError
        with pytest.raises(CoinDCXError):
            client.get_public("/exchange/ticker")

        assert cb.is_open
        time.sleep(0.06)  # let recovery_timeout elapse
        assert cb.state == CircuitState.HALF_OPEN

        # Successful probe closes
        mock_ok = MagicMock()
        mock_ok.status_code = 200
        mock_ok.json.return_value = {}
        mock_ok.headers = {}
        client.session.request.return_value = mock_ok
        client.get_public("/exchange/ticker")
        assert cb.state == CircuitState.CLOSED

    def test_execution_engine_exposes_circuit_breaker(self):
        from crypto_trader.exchanges.coindcx_execution import CoinDCXExecutionEngine
        from crypto_trader.exchanges.coindcx_client import CoinDCXClient
        cb = CircuitBreaker(name="shared", failure_threshold=5)
        c = CoinDCXClient(circuit_breaker=cb)
        eng = CoinDCXExecutionEngine(client=c)
        assert eng.circuit_breaker is cb
        assert eng.circuit_breaker is c._cb
