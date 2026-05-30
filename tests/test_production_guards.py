"""Production-readiness guards G1–G5."""
import tempfile
import time
import uuid
from email.utils import formatdate
from pathlib import Path

import pytest

from crypto_trader.config import TradingConfig
from crypto_trader.exchanges.coindcx_client import CoinDCXClient, _parse_float
from crypto_trader.risk import RiskManager


def _clean_risk(**kw):
    r = RiskManager(**kw)
    # Isolate from the shared persisted state file (RiskManager.DATA_DIR is
    # import-time fixed, so redirect saves to a unique temp file).
    r.state_file = Path(tempfile.gettempdir()) / f"risk_test_{uuid.uuid4().hex}.json"
    r.kill_switch = False
    r.daily_count = 0
    r.consecutive_losses = 0
    r.last_loss_time = None
    r.peak_balance = None
    r._recent_open_times = []
    return r


# ── G1: clock-skew measurement ───────────────────────────────────────────────
class _Resp:
    def __init__(self, headers):
        self.headers = headers


def test_clock_skew_detects_drift(monkeypatch):
    client = CoinDCXClient(api_key="k", api_secret="s")
    server_epoch = time.time() - 5  # venue clock 5s behind local
    monkeypatch.setattr(client.session, "get",
                        lambda *a, **k: _Resp({"Date": formatdate(server_epoch, usegmt=True)}))
    skew = client.measure_clock_skew_ms()
    assert skew is not None
    assert 4000 < skew < 6500   # ~+5000ms (1s Date resolution + RTT slack)


def test_clock_skew_none_without_date_header(monkeypatch):
    client = CoinDCXClient(api_key="k", api_secret="s")
    monkeypatch.setattr(client.session, "get", lambda *a, **k: _Resp({}))
    assert client.measure_clock_skew_ms() is None


# ── G2: velocity circuit breaker ──────────────────────────────────────────────
def test_velocity_breaker_blocks_burst():
    r = _clean_risk(max_daily_trades=100, max_orders_per_minute=3)
    for _ in range(3):
        ok, _ = r.can_trade()
        assert ok
        r.record_open()
    ok, reason = r.can_trade()
    assert not ok
    assert "Velocity" in reason


def test_velocity_window_prunes_old_opens():
    r = _clean_risk(max_daily_trades=100, max_orders_per_minute=2)
    # Two opens older than 60s should not count against the window.
    r._recent_open_times = [time.time() - 120, time.time() - 90]
    ok, _ = r.can_trade()
    assert ok


def test_velocity_disabled_when_zero():
    r = _clean_risk(max_daily_trades=100, max_orders_per_minute=0)
    r._recent_open_times = [time.time()] * 50
    ok, _ = r.can_trade()
    assert ok


# ── G3: thread supervisor ─────────────────────────────────────────────────────
class _FakeThread:
    def __init__(self, alive):
        self._alive = alive

    def is_alive(self):
        return self._alive


def _engine(monkeypatch, tmp_path, feed_alive=True, pm_alive=True):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    from crypto_trader.engine_ws import WebSocketTradingEngine
    eng = WebSocketTradingEngine(symbol="SOLUSDT", use_llm=False, cfg=TradingConfig())
    eng.ws_feed = _FakeThread(feed_alive)
    eng.ws_pm = _FakeThread(pm_alive)
    eng.risk_manager = _clean_risk()
    return eng


def test_supervisor_passes_when_threads_alive(monkeypatch, tmp_path):
    eng = _engine(monkeypatch, tmp_path, feed_alive=True, pm_alive=True)
    assert eng._supervise_threads() is True
    assert not eng.risk_manager.kill_switch


def test_supervisor_fail_stops_on_dead_thread(monkeypatch, tmp_path):
    from crypto_trader import safe_mode
    halt = tmp_path / "HALT"
    monkeypatch.setattr(safe_mode, "HALT_FILE", halt)
    eng = _engine(monkeypatch, tmp_path, feed_alive=False, pm_alive=True)
    from crypto_trader.events import EventBus, SystemFailureEvent
    bus = EventBus()
    failures = []
    bus.subscribe(SystemFailureEvent, lambda e: failures.append(e))
    eng.event_bus = bus

    assert eng._supervise_threads() is False
    assert eng._halted is True
    assert eng.risk_manager.kill_switch is True
    assert halt.exists()
    assert len(failures) == 1


def test_supervisor_respects_disable_flag(monkeypatch, tmp_path):
    cfg = TradingConfig(thread_supervisor_enabled=False)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    from crypto_trader.engine_ws import WebSocketTradingEngine
    eng = WebSocketTradingEngine(symbol="SOLUSDT", use_llm=False, cfg=cfg)
    eng.ws_feed = _FakeThread(False)
    eng.ws_pm = _FakeThread(False)
    eng.risk_manager = _clean_risk()
    assert eng._supervise_threads() is True   # supervisor disabled -> no halt


# ── G4: rate-limit backpressure ───────────────────────────────────────────────
def test_backpressure_sleeps_when_quota_low(monkeypatch):
    client = CoinDCXClient(api_key="k", api_secret="s")
    slept = []
    monkeypatch.setattr("crypto_trader.exchanges.coindcx_client.time.sleep", lambda s: slept.append(s))
    client._apply_backpressure(_Resp({"X-RateLimit-Remaining": "2", "X-RateLimit-Limit": "100"}))
    assert slept and slept[0] > 0


def test_backpressure_noop_when_quota_high(monkeypatch):
    client = CoinDCXClient(api_key="k", api_secret="s")
    slept = []
    monkeypatch.setattr("crypto_trader.exchanges.coindcx_client.time.sleep", lambda s: slept.append(s))
    client._apply_backpressure(_Resp({"X-RateLimit-Remaining": "90", "X-RateLimit-Limit": "100"}))
    assert not slept


def test_parse_float_helper():
    assert _parse_float("12") == 12.0
    assert _parse_float(None) is None
    assert _parse_float("abc") is None


# ── G5: order-status query, cancel-all, strict reconcile ──────────────────────
class _FakeExecEngine:
    def __init__(self, open_orders=None, fills=None, positions=None):
        self._open = open_orders or []
        self._fills = fills or []
        self._positions = positions or []
        self.cancelled = []
        self.cancel_all_calls = 0

    def get_open_orders(self, symbol=None):
        return self._open

    def get_fills(self, symbol=None, **kw):
        return self._fills

    def get_positions(self):
        return self._positions

    def get_balances(self):
        return {}

    def cancel_order(self, oid):
        self.cancelled.append(oid)
        return True

    def cancel_all_orders(self, symbol=None):
        self.cancel_all_calls += 1
        n = len(self._open)
        self._open = []
        return n

    # delegate get_order_status / sync to the real implementation under test
    from crypto_trader.exchanges.coindcx_execution import CoinDCXExecutionEngine
    get_order_status = CoinDCXExecutionEngine.get_order_status


def test_get_order_status_open_filled_unknown():
    eng = _FakeExecEngine(
        open_orders=[{"exchange_order_id": "A", "status": "open", "filled_quantity": 0.0}],
        fills=[{"exchange_order_id": "B", "fill_quantity": 1.5}],
    )
    assert eng.get_order_status("A", "SOLUSDT")["status"] == "open"
    assert eng.get_order_status("B", "SOLUSDT")["status"] == "filled"
    assert eng.get_order_status("C", "SOLUSDT")["status"] == "unknown"


def test_strict_reconcile_cancels_all_on_unresolved(monkeypatch, tmp_path):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    import uuid
    from crypto_trader.wallet import DATA_DIR, EnhancedFuturesWallet, Playbook, PositionSide
    from crypto_trader.execution.reconciler import Reconciler

    ns = f"test_{uuid.uuid4().hex}"
    wallet = EnhancedFuturesWallet(symbol="SOLUSDT", state_namespace=ns, leverage=1)
    wallet.live_execution = True
    wallet.open_position("SOLUSDT", {
        "entry_price": "100", "side": PositionSide.LONG, "playbook": Playbook.INTRADAY,
        "sl_price": "90", "tp_price": "110",
    }, mark_price=100, custom_quantity=1)

    # Venue has NO position (ghost) and one stray open order -> unresolved desync.
    eng = _FakeExecEngine(open_orders=[{"exchange_order_id": "Z", "symbol": "DOGEUSDT", "status": "open"}],
                          positions=[])
    risk = _clean_risk()
    rec = Reconciler(wallet, eng, risk, strict_cancel=True)
    mismatches = rec.reconcile("SOLUSDT")

    assert any(m.kind == "ghost_position" for m in mismatches)
    assert eng.cancel_all_calls == 1          # strict mode flattened the book
    assert risk.kill_switch is True           # and halted


# ── Production-readiness safety fixes ─────────────────────────────────────────

def test_signal_consumer_retry_does_not_double_count_opens(monkeypatch):
    from crypto_trader.execution.signal_bus import SignalConsumer, Signal
    from decimal import Decimal

    class MockBus:
        def __init__(self):
            self.acks = []
            self.marks = []
        def is_processed(self, k): return False
        def delivery_count(self, s, g, m): return 1
        def mark_processed(self, k, ttl): self.marks.append(k)
        def ack(self, s, g, m): self.acks.append(m)

    class FailingAdapter:
        def __init__(self):
            self.should_fail = True
            self.calls = 0
        def execute(self, sig):
            self.calls += 1
            if self.should_fail:
                raise ValueError("temporary error")

    bus = MockBus()
    adapter = FailingAdapter()
    recorded = 0
    def record_fn():
        nonlocal recorded
        recorded += 1

    consumer = SignalConsumer(
        bus, {"paper": adapter},
        record_open_fn=record_fn,
        risk_gate=lambda s: True
    )

    sig = Signal(strategy_id="test", symbol="SOLUSDT", side="LONG", quantity=1.0, mode="paper")

    # 1. First execution fails: should not increment recorded, should not mark/ack
    consumer._handle("msg_1", sig.to_payload())
    assert recorded == 0
    assert len(bus.acks) == 0
    assert adapter.calls == 1

    # 2. Second execution succeeds: should increment recorded and ack
    adapter.should_fail = False
    consumer._handle("msg_1", sig.to_payload())
    assert recorded == 1
    assert len(bus.acks) == 1
    assert adapter.calls == 2


def test_ws_engine_health_consecutive_failures_trigger_kill_switch(monkeypatch, tmp_path):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    from crypto_trader.engine_ws import WebSocketTradingEngine
    from crypto_trader.config import TradingConfig
    from crypto_trader.wallet import EnhancedFuturesWallet, PositionSide, Playbook, EnhancedPosition
    from crypto_trader.exchanges.coindcx_execution import CoinDCXExecutionEngine
    from decimal import Decimal

    cfg = TradingConfig(mode="live", symbol="SOLUSDT")
    eng = WebSocketTradingEngine(symbol="SOLUSDT", use_llm=False, cfg=cfg)
    eng.risk_manager = _clean_risk()
    
    class BadEngine(CoinDCXExecutionEngine):
        def __init__(self):
            pass
        def get_cross_margin_details(self):
            raise ValueError("API error")

    eng.wallet.attach_execution_engine(BadEngine(), live=True)

    pos = EnhancedPosition(
        symbol="SOLUSDT", side=PositionSide.LONG, playbook=Playbook.INTRADAY,
        entry_price=Decimal("100.0"), original_quantity=Decimal("1.0"),
        remaining_quantity=Decimal("1.0"), notional=Decimal("100.0"),
        margin_used=Decimal("10.0"), leverage=10, open_time=int(time.time()*1000),
        sl_price=Decimal("90.0")
    )
    eng.wallet.positions["SOLUSDT"] = pos

    # Call health check 2 times - should not trip kill switch
    eng._check_authoritative_health()
    eng._check_authoritative_health()
    assert eng.risk_manager.kill_switch is False

    # 3rd call - should trip kill switch
    eng._check_authoritative_health()
    assert eng.risk_manager.kill_switch is True
    assert eng._halted is True


def test_wallet_sl_placement_failure_flattens_when_sl_required(monkeypatch, tmp_path):
    monkeypatch.setattr("crypto_trader.wallet.DATA_DIR", tmp_path)
    from crypto_trader.wallet import EnhancedFuturesWallet, Order, OrderType, OrderStatus, PositionSide, Playbook
    import pytest
    from decimal import Decimal

    w = EnhancedFuturesWallet(
        symbol="SOLUSDT", initial_balance=1000, leverage=2,
        state_namespace="slfailtest", require_venue_sl=True, venue_sl_enabled=True
    )

    class MockExec:
        def __init__(self):
            self.orders_placed = []
        def place_order(self, symbol, side, qty, otype, *, trigger_price=None, limit_price=None,
                        reduce_only=False, expires_at=None, client_order_id=None):
            self.orders_placed.append((otype, side, qty))
            if otype == OrderType.STOP_MARKET:
                raise ValueError("Exchange SL placement failed")
            return Order(id="ex-9", symbol=symbol, side=side, order_type=otype, quantity=qty,
                         status=OrderStatus.FILLED, created_at=0, reduce_only=reduce_only,
                         filled_quantity=qty, avg_fill_price=Decimal("120.00"))
        def cancel_order(self, oid): return True
        def sync_positions(self): return {}

    mock_exec = MockExec()
    w.attach_execution_engine(mock_exec, live=True)

    setup = {
        "entry_price": 120.0, "side": PositionSide.LONG, "playbook": Playbook.INTRADAY,
        "sl_price": 118.0, "tp_price": 125.0,
    }

    with pytest.raises(RuntimeError, match="Stop Loss placement failed"):
        w.open_position("SOLUSDT", setup, mark_price=120.0)

    # Verify that the position was flattened on the exchange (a MARKET order was sent to exit)
    assert any(otype == OrderType.MARKET and side == PositionSide.SHORT for otype, side, qty in mock_exec.orders_placed)
    # Verify that the position is not active in the wallet
    assert w.get_open_position("SOLUSDT") is None

