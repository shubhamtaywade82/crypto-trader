from decimal import Decimal
import sqlite3
import uuid

from crypto_trader.wallet import DATA_DIR, EnhancedFuturesWallet, Playbook, PositionSide, OrderType


def _setup(side=PositionSide.LONG):
    return {
        "entry_price": "100",
        "side": side,
        "playbook": Playbook.INTRADAY,
        "sl_price": "90" if side == PositionSide.LONG else "110",
        "tp_price": "110" if side == PositionSide.LONG else "90",
    }


def _fresh_wallet(symbol: str):
    ns = f"test_{uuid.uuid4().hex}"
    for p in DATA_DIR.glob(f"wallet_{ns}_{symbol}*"):
        p.unlink(missing_ok=True)
    for p in DATA_DIR.glob(f"wallet_events_{ns}_{symbol}*"):
        p.unlink(missing_ok=True)
    return EnhancedFuturesWallet(symbol=symbol, state_namespace=ns)


def test_order_fill_lifecycle_and_replay(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    w = _fresh_wallet("BTCUSDT")
    p = w.open_position("BTCUSDT", _setup(), mark_price=100)
    assert p is not None
    assert len(w.orders) == 1
    assert len(w.fills) == 1

    replay = w.replay_portfolio_state()
    assert len(replay.orders) == 1
    assert len(replay.open_positions) == 1


def test_partial_close_and_close_updates_replay(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    w = _fresh_wallet("ETHUSDT")
    w.open_position("ETHUSDT", _setup(), mark_price=100)
    w.partial_close("ETHUSDT", mark_price=105, pct=0.5, reason="T1")
    w.close_position("ETHUSDT", mark_price=106, reason="T2")

    replay = w.replay_portfolio_state()
    assert len(replay.open_positions) == 0
    assert replay.realized_pnl_total != Decimal("0")


def test_invariant_filled_qty_leq_order_qty(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    w = _fresh_wallet("SOLUSDT")
    w.open_position("SOLUSDT", _setup(), mark_price=100)
    order = next(iter(w.orders.values()))
    assert order.filled_quantity <= order.quantity


def test_replay_deduplicates_duplicate_events(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    w = _fresh_wallet("XRPUSDT")
    w.open_position("XRPUSDT", _setup(), mark_price=100)

    # Inject duplicate ORDER_CREATED event row and ensure replay remains stable.
    with sqlite3.connect(w.db_file) as conn:
        row = conn.execute(
            "SELECT ts, event_type, symbol, namespace, payload_json FROM events WHERE event_type='ORDER_CREATED' LIMIT 1"
        ).fetchone()
        assert row is not None
        conn.execute(
            "INSERT INTO events (ts, event_type, symbol, namespace, payload_json) VALUES (?, ?, ?, ?, ?)",
            (row[0], row[1], row[2], w.state_namespace, row[4]),
        )
        conn.commit()
    state = w.replay_portfolio_state()
    assert len(state.orders) == 1


def test_multi_fill_emits_partial_fill_events(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    w = _fresh_wallet("ADAUSDT")
    s = _setup()
    s["fill_chunks"] = 3
    w.open_position("ADAUSDT", s, mark_price=100)
    order = next(iter(w.orders.values()))
    assert order.filled_quantity == order.quantity
    assert len(w.fills) == 3
    with sqlite3.connect(w.db_file) as conn:
        partial_count = conn.execute("SELECT COUNT(*) FROM events WHERE event_type='ORDER_PARTIALLY_FILLED'").fetchone()[0]
        filled_count = conn.execute("SELECT COUNT(*) FROM events WHERE event_type='ORDER_FILLED'").fetchone()[0]
    assert partial_count == 2
    assert filled_count >= 1


def test_limit_order_triggers_and_cancel_flow(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    w = _fresh_wallet("BNBUSDT")
    o = w.place_pending_order(
        symbol="BNBUSDT",
        side=PositionSide.LONG,
        quantity=Decimal("10"),
        order_type=OrderType.LIMIT,
        limit_price=Decimal("99"),
    )
    assert o.status.value == "PENDING"
    w.evaluate_pending_orders("BNBUSDT", mark_price=101)
    assert w.orders[o.id].status.value == "PENDING"
    w.evaluate_pending_orders("BNBUSDT", mark_price=99)
    assert w.orders[o.id].status.value == "FILLED"

    o2 = w.place_pending_order(
        symbol="BNBUSDT",
        side=PositionSide.SHORT,
        quantity=Decimal("2"),
        order_type=OrderType.STOP_MARKET,
        trigger_price=Decimal("105"),
    )
    assert w.cancel_order(o2.id) is True
    assert w.orders[o2.id].status.value == "CANCELLED"


def test_funding_event_and_reduce_only_rejection(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    w = _fresh_wallet("DOGEUSDT")
    # reduce-only without a position should reject on trigger
    ro = w.place_pending_order(
        symbol="DOGEUSDT",
        side=PositionSide.LONG,
        quantity=Decimal("1"),
        order_type=OrderType.LIMIT,
        limit_price=Decimal("99"),
        reduce_only=True,
    )
    w.evaluate_pending_orders("DOGEUSDT", mark_price=99)
    assert w.orders[ro.id].status.value == "REJECTED"

    # funding should apply once a position exists
    w2 = _fresh_wallet("DOGEUSDT")
    w2.open_position("DOGEUSDT", _setup(), mark_price=100)
    amt = w2.apply_funding("DOGEUSDT", Decimal("0.0001"))
    assert amt != Decimal("0")


def test_funding_scheduler_interval_and_snapshot_boot(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    w = _fresh_wallet("LTCUSDT")
    w.open_position("LTCUSDT", _setup(), mark_price=100)
    a1 = w.run_funding_scheduler("LTCUSDT", Decimal("0.0001"), interval_ms=1000, now_ms=1_000_000)
    a2 = w.run_funding_scheduler("LTCUSDT", Decimal("0.0001"), interval_ms=1000, now_ms=1_000_500)
    assert a1 != Decimal("0")
    assert a2 == Decimal("0")
    w._save_state()
    # new instance should load from db snapshot path without crashing
    w2 = EnhancedFuturesWallet(symbol=w.symbol, state_namespace=w.state_namespace)
    assert w2.wallet_balance == w.wallet_balance


def test_reduce_only_rejects_exposure_increase_and_halt_policy(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    events = []
    w = _fresh_wallet("AVAXUSDT")
    w.event_hook = lambda e: events.append(e)
    w.halt_on_invariant_violation = True
    w.open_position("AVAXUSDT", _setup(side=PositionSide.LONG), mark_price=100)
    ro = w.place_pending_order(
        symbol="AVAXUSDT",
        side=PositionSide.LONG,
        quantity=Decimal("1"),
        order_type=OrderType.LIMIT,
        limit_price=Decimal("99"),
        reduce_only=True,
    )
    w.evaluate_pending_orders("AVAXUSDT", mark_price=99)
    assert w.orders[ro.id].status.value == "REJECTED"

    # force an invariant violation and verify halt behavior
    o = next(iter(w.orders.values()))
    o.filled_quantity = o.quantity + Decimal("1")
    w._run_invariant_checks()
    assert w.halted is True
    assert any(e.get("event_type") == "INVARIANT_VIOLATION" for e in events)


def test_event_observability_helpers(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    w = _fresh_wallet("UNIUSDT")
    s = _setup()
    s["fill_chunks"] = 2
    w.open_position("UNIUSDT", s, mark_price=100)
    order = next(iter(w.orders.values()))

    timeline = w.get_order_timeline(order.id)
    assert len(timeline) >= 2
    assert timeline[0]["payload"]["order_id"] == order.id

    recent_fills = w.get_recent_events(limit=10, event_type="ORDER_FILLED")
    assert len(recent_fills) >= 1
