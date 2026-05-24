from decimal import Decimal
import sqlite3
import uuid

import pandas as pd

from crypto_trader.wallet import DATA_DIR, EnhancedFuturesWallet, Playbook, PositionSide
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


def test_empty_tp_levels_no_crash(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    w = _fresh_wallet("SOLUSDT")
    p = w.open_position("SOLUSDT", _setup(), mark_price=100)
    assert p is not None
    # Clear tp_levels to simulate state after reducer re-sync loses them
    p.tp_levels = []
    # Must not raise IndexError — mark_price above SL, no TP to check
    w.update_positions("SOLUSDT", mark_price=101.5, candle_close_time=1000)
    assert w.get_open_position("SOLUSDT") is not None


def test_liquidation_uses_strict_less_than(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    w = _fresh_wallet("SOLUSDT")
    w.open_position("SOLUSDT", _setup(), mark_price=100)
    pos = w.get_open_position("SOLUSDT")
    assert pos is not None

    entry = pos.entry_price  # ~100.05 after slippage
    # At entry: equity >> maintenance → not liquidated
    assert not w._is_liquidation_required(pos, entry)
    # At catastrophically low price: equity << maintenance → liquidated
    assert w._is_liquidation_required(pos, Decimal("1.0"))
    # With lev=10, mr=0.005: liquidation boundary ≈ entry / 1.095 ≈ 91.37
    # A price clearly safe above boundary (~95.0)
    assert not w._is_liquidation_required(pos, entry * Decimal("0.95"))
    # A price clearly below boundary (~88.0)
    assert w._is_liquidation_required(pos, entry * Decimal("0.88"))


def test_journal_llm_contribution_correct(tmp_path, monkeypatch):
    import crypto_trader.journal as jmod
    journal_dir = tmp_path / "journal"
    journal_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(jmod, "DATA_DIR", journal_dir)

    j = jmod.TradeJournal()

    # Trade 1: with LLM, profitable
    j.log_open(
        trade_id="abc123",
        symbol="SOLUSDT",
        regime="TRENDING_UP",
        regime_score=0.8,
        setup={},
        technical_score=0.7,
        llm_advice={"bias": "bullish", "confidence": 0.8, "latency_ms": 200, "age_seconds": 5},
        llm_weight=0.20,
        final_score=0.78,
        margin=500.0,
        notional=5000.0,
        entry_price=150.0,
        funding_rate=0.0001,
        oi_delta=0.5,
        taker_ratio=1.1,
    )
    j.log_close(trade_id="abc123", exit_price=155.0, realized_pnl=25.0, exit_reason="TP_HIT")

    # Trade 2: without LLM, losing
    j.log_open(
        trade_id="def456",
        symbol="SOLUSDT",
        regime="TRENDING_UP",
        regime_score=0.7,
        setup={},
        technical_score=0.65,
        llm_advice=None,
        llm_weight=0.0,
        final_score=0.65,
        margin=500.0,
        notional=5000.0,
        entry_price=148.0,
        funding_rate=0.0001,
        oi_delta=0.3,
        taker_ratio=1.0,
    )
    j.log_close(trade_id="def456", exit_price=145.0, realized_pnl=-15.0, exit_reason="SL_HIT")

    result = j.analyze_llm_contribution(days=7)

    assert result["total_trades"] == 2
    assert result["with_llm_count"] == 1
    assert result["without_llm_count"] == 1
    assert result["with_llm_avg_pnl"] == 25.0
    assert result["without_llm_avg_pnl"] == -15.0
    assert result["total_pnl"] == 10.0
    assert result["win_rate"] == 0.5
    assert result["llm_value_add"] == 40.0


def test_adx_no_crash_on_zero_di():
    from crypto_trader.regime import compute_adx
    # Constant high/low: ATR > 0 but plus_dm = minus_dm = 0 after filtering
    n = 30
    df = pd.DataFrame({
        "open": [100.0] * n,
        "high": [101.0] * n,
        "low": [99.0] * n,
        "close": [100.0] * n,
    })
    result = compute_adx(df, period=14)
    assert result is not None
    assert len(result) == n
    # After warmup, non-NaN values should all be 0 (fix replaces 0/0 with 0)
    non_nan = result.dropna()
    assert len(non_nan) > 0
    assert (non_nan == 0.0).all()
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


def test_runtime_equals_replay_after_lifecycle_sequence(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    w = _fresh_wallet("ATOMUSDT")
    setup = _setup()
    setup["fill_chunks"] = 3
    w.open_position("ATOMUSDT", setup, mark_price=100)
    w.partial_close("ATOMUSDT", mark_price=103, pct=Decimal("0.25"), reason="P1")
    w.evaluate_pending_orders("ATOMUSDT", mark_price=101)
    w.apply_funding("ATOMUSDT", Decimal("0.0001"))
    w.close_position("ATOMUSDT", mark_price=104, reason="FINAL")

    replay = w.replay_portfolio_state()
    assert replay.wallet_balance == w.wallet_balance
    assert replay.realized_pnl_total == w.realized_pnl_total
    assert len(replay.open_positions) == len([p for p in w.positions.values() if p.status == "OPEN"])


def test_restart_recovery_matches_replay(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    w = _fresh_wallet("LINKUSDT")
    w.open_position("LINKUSDT", _setup(), mark_price=100)
    w.partial_close("LINKUSDT", mark_price=102, pct=Decimal("0.5"), reason="P1")
    w.run_funding_scheduler("LINKUSDT", Decimal("0.0001"), interval_ms=1, now_ms=10_000)
    replay_before = w.replay_portfolio_state()
    w._save_state()

    w2 = EnhancedFuturesWallet(symbol=w.symbol, state_namespace=w.state_namespace)
    assert w2.wallet_balance == replay_before.wallet_balance
    assert w2.realized_pnl_total == replay_before.realized_pnl_total


def test_phase4_snapshot_features_and_phase5_observability(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    
    # Fresh wallet with max_snapshots=3 to test rotation
    w = _fresh_wallet("OPUSDT")
    w.max_snapshots = 3
    
    # Check initial metrics
    assert w.replay_duration_ms == 0.0
    assert w.delta_size == 0
    
    # Apply some state transitions
    w.open_position("OPUSDT", _setup(), mark_price=100)
    w._save_state()  # Snapshot 1
    
    w.apply_funding("OPUSDT", Decimal("0.0001"))
    w._save_state()  # Snapshot 2
    
    w.partial_close("OPUSDT", mark_price=105, pct=Decimal("0.5"), reason="P1")
    w._save_state()  # Snapshot 3
    
    w.close_position("OPUSDT", mark_price=106, reason="Close")
    w._save_state()  # Snapshot 4
    
    # 1. Test Snapshot Rotation in SQLite
    with sqlite3.connect(w.db_file) as conn:
        count = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
        assert count <= 3, f"Rotation failed: count={count}"
        
        # Grab state_json from the latest snapshot
        state_json = conn.execute("SELECT state_json FROM snapshots ORDER BY ts DESC LIMIT 1").fetchone()[0]
        assert state_json.startswith("v1_gzip_b64:"), "Encoding/Compression format mismatch"

    # 2. Test Observability methods (Phase 5)
    timeline = w.get_position_timeline("OPUSDT")
    assert len(timeline) >= 4  # ORDER_CREATED, ORDER_FILLED, POSITION_OPENED, etc.
    
    funding_history = w.get_funding_history("OPUSDT")
    assert len(funding_history) == 1
    assert funding_history[0]["symbol"] == "OPUSDT"
    
    past_state = w.replay_until(w._now_ms() - 1000)
    assert past_state is not None
    
    # Test compare_runtime_vs_replay
    diff_report = w.compare_runtime_vs_replay()
    assert diff_report["in_sync"] is True
    
    # 3. Test Bootstrap Metrics recovery (Phase 4)
    w2 = EnhancedFuturesWallet(symbol=w.symbol, state_namespace=w.state_namespace, max_snapshots=3)
    assert w2.recovery_latency_ms > 0
    assert w2.delta_size == 0
    
    # Emit an event without saving, then reload to verify delta_size > 0
    w2._emit_event("ORDER_CREATED", {
        "order_id": "test-delta-1",
        "symbol": "OPUSDT",
        "side": "LONG",
        "quantity": "1.0",
        "status": "PENDING",
        "order_type": "MARKET",
        "reduce_only": False,
        "trigger_price": None,
        "limit_price": None,
        "expires_at": None,
    })
    
    w3 = EnhancedFuturesWallet(symbol=w.symbol, state_namespace=w.state_namespace, max_snapshots=3)
    assert w3.delta_size == 1
    assert w3.replay_duration_ms >= 0

    # 4. Test Snapshot Integrity Check failure
    with sqlite3.connect(w.db_file) as conn:
        conn.execute("UPDATE snapshots SET state_json = 'v1_gzip_b64:corrupt_hash:corrupt_base64'")
        conn.commit()
    
    assert w3._load_from_db_snapshot_and_replay() is False


def test_peak_trading_stop_exit(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    w = _fresh_wallet("SOLUSDT")
    p = w.open_position("SOLUSDT", _setup(), mark_price=100)
    assert p is not None
    
    # 1. Price moves to 103 (3% price change, triggers peak trailing stop activation)
    w.update_positions("SOLUSDT", mark_price=103, candle_close_time=1000)
    pos = w.get_open_position("SOLUSDT")
    assert pos is not None
    assert pos.peak_pnl_pct >= Decimal("0.02")
    assert pos.highest_price == Decimal("103")
    
    # 2. Price retraces to 102.2 (which has unrealized PnL less than 75% of peak profit)
    w.update_positions("SOLUSDT", mark_price=102.2, candle_close_time=2000)
    assert w.get_open_position("SOLUSDT") is None
    
    # Verify close reason
    history = w.position_history
    assert len(history) == 1
    assert history[0]["close_reason"] == "PEAK_TRADING_STOP"


def test_windfall_ro_margin_exit(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    w = _fresh_wallet("SOLUSDT")
    p = w.open_position("SOLUSDT", _setup(), mark_price=100)
    assert p is not None
    
    # Return on Margin (ROM) = unrealized_pnl / margin_used
    # At 10x leverage, a 4% price increase (to 104) yields a 40% ROM.
    # We use 104.5 to account for entry slippage.
    w.update_positions("SOLUSDT", mark_price=104.5, candle_close_time=1000)
    
    # The position should exit fully due to WINDFALL_RO_MARGIN_EXIT
    assert w.get_open_position("SOLUSDT") is None
    
    # Verify close reason
    history = w.position_history
    assert len(history) == 1
    assert history[0]["close_reason"] == "WINDFALL_RO_MARGIN_EXIT"


def test_ai_and_regime_reversal_exits(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    
    # Test 1: AI Reversal Exit
    w1 = _fresh_wallet("SOLUSDT")
    p1 = w1.open_position("SOLUSDT", _setup(), mark_price=100)
    assert p1 is not None
    
    # AI Reversal: Long position, Bearish advice, confidence >= 0.70, price >= 99.5% entry (99.5)
    w1.update_positions(
        "SOLUSDT",
        mark_price=100,
        candle_close_time=1000,
        current_llm_advice={"bias": "bearish", "confidence": 0.8}
    )
    assert w1.get_open_position("SOLUSDT") is None
    assert w1.position_history[0]["close_reason"] == "AI_REVERSAL_EXIT"

    # Test 2: AI Reversal Exit blocked if below 99.5% entry (to avoid locking in major losses)
    w2 = _fresh_wallet("SOLUSDT")
    p2 = w2.open_position("SOLUSDT", _setup(), mark_price=100)
    assert p2 is not None
    
    # Bearish advice, but price is 99.0 (below 99.5)
    w2.update_positions(
        "SOLUSDT",
        mark_price=99.0,
        candle_close_time=1000,
        current_llm_advice={"bias": "bearish", "confidence": 0.8}
    )
    assert w2.get_open_position("SOLUSDT") is not None

    # Test 3: Regime Reversal Exit
    w3 = _fresh_wallet("SOLUSDT")
    p3 = w3.open_position("SOLUSDT", _setup(), mark_price=100)
    assert p3 is not None
    
    # Regime Reversal: Long position, TRENDING_DOWN regime, price >= 99.5
    w3.update_positions(
        "SOLUSDT",
        mark_price=100,
        candle_close_time=1000,
        current_regime="TRENDING_DOWN"
    )
    assert w3.get_open_position("SOLUSDT") is None
    assert w3.position_history[0]["close_reason"] == "REGIME_REVERSAL_EXIT"

