from decimal import Decimal
import sqlite3
import uuid

import pandas as pd

from crypto_trader.wallet import DATA_DIR, EnhancedFuturesWallet, Playbook, PositionSide


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
    w.update_positions("SOLUSDT", mark_price=105, candle_close_time=1000)
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
