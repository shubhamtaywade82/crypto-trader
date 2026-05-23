from decimal import Decimal
import sqlite3
import uuid
import pytest
import time
from crypto_trader.wallet import (
    DATA_DIR,
    EnhancedFuturesWallet,
    Playbook,
    PositionSide,
    OrderType,
    OrderStatus,
    PortfolioState,
)


def _fresh_wallet_with_namespace(symbol: str, ns: str, now_ms_fn=None):
    for p in DATA_DIR.glob(f"wallet_{ns}_{symbol}*"):
        p.unlink(missing_ok=True)
    for p in DATA_DIR.glob(f"wallet_events_{ns}_{symbol}*"):
        p.unlink(missing_ok=True)
    return EnhancedFuturesWallet(symbol=symbol, state_namespace=ns, now_ms_fn=now_ms_fn)


def _setup_position(side=PositionSide.LONG):
    return {
        "entry_price": "100",
        "side": side,
        "playbook": Playbook.INTRADAY,
        "sl_price": "90" if side == PositionSide.LONG else "110",
        "tp_price": "110" if side == PositionSide.LONG else "90",
    }


def test_continuous_simulation_drift_and_leaks(tmp_path, monkeypatch):
    """Simulates 24h of continuous fast-forwarded trading, checking for state drift and memory/data leaks."""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    ns = f"test_stability_{uuid.uuid4().hex}"
    
    current_time_ms = 1_000_000
    w = _fresh_wallet_with_namespace("BTCUSDT", ns, now_ms_fn=lambda: current_time_ms)
    
    # 500 steps of simulation
    for tick in range(1, 501):
        current_time_ms += 10_000  # Advance 10 seconds per step
        price = 100.0 + (tick % 10) - 5.0  # Oscillate price between 95 and 105
        
        # Every 20 ticks, evaluate position or order placements
        if tick % 20 == 1:
            if not w.get_open_position("BTCUSDT"):
                setup = _setup_position()
                setup["fill_chunks"] = 2
                w.open_position("BTCUSDT", setup, mark_price=price)
        
        # Evaluates stop and limit orders
        w.evaluate_pending_orders("BTCUSDT", mark_price=price)
        
        # Every 50 ticks, apply funding
        if tick % 50 == 0:
            w.apply_funding("BTCUSDT", Decimal("0.0001"))
            
        # Every 60 ticks, perform partial close if open
        if tick % 60 == 0:
            if w.get_open_position("BTCUSDT"):
                w.partial_close("BTCUSDT", mark_price=price, pct=Decimal("0.5"), reason=f"PartialTick_{tick}")
                
        # Every 100 ticks, close position if open
        if tick % 100 == 0:
            if w.get_open_position("BTCUSDT"):
                w.close_position("BTCUSDT", mark_price=price, reason=f"CloseTick_{tick}")
                
        # Periodic state saves
        if tick % 40 == 0:
            w._save_state()

    # Validate that replay and runtime match perfectly at the end of the simulation
    diff = w.compare_runtime_vs_replay()
    assert diff["in_sync"] is True, f"Drift detected: {diff['discrepancies']}"
    
    # Check that positions dictionary doesn't accumulate closed garbage positions
    assert len(w.positions) <= 1


def test_restart_storms(tmp_path, monkeypatch):
    """Simulates repeated crashes and restarts during orders, funding, and partial fills."""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    ns = f"test_storm_{uuid.uuid4().hex}"
    
    current_time_ms = 1_000_000
    
    # Loop over 15 iterations of crash & recovery
    for storm_step in range(1, 16):
        # Instantiate a new wallet to load existing state
        w = EnhancedFuturesWallet(symbol="ETHUSDT", state_namespace=ns, now_ms_fn=lambda: current_time_ms)
        
        # Apply some operations depending on the step
        current_time_ms += 5000
        
        if storm_step % 3 == 1:
            # Place some orders
            w.place_pending_order(
                symbol="ETHUSDT",
                side=PositionSide.LONG,
                quantity=Decimal("2.5"),
                order_type=OrderType.LIMIT,
                limit_price=Decimal("100") - storm_step,
            )
        elif storm_step % 3 == 2:
            # Fill partially or create position
            setup = _setup_position()
            setup["fill_chunks"] = 4
            w.open_position("ETHUSDT", setup, mark_price=100.0)
            w.evaluate_pending_orders("ETHUSDT", mark_price=100.0)
        else:
            # Apply funding and save
            w.apply_funding("ETHUSDT", Decimal("0.00015"))
            if w.get_open_position("ETHUSDT"):
                w.partial_close("ETHUSDT", mark_price=102.0, pct=Decimal("0.2"), reason=f"P_{storm_step}")
        
        # Save state to simulate crash persistence
        w._save_state()
        
        # Re-verify that recovery loading is consistent with full event replay
        diff = w.compare_runtime_vs_replay()
        assert diff["in_sync"] is True, f"Drift at storm step {storm_step}: {diff['discrepancies']}"


def test_partial_fill_floods(tmp_path, monkeypatch):
    """Floods the system with multi-chunk partial fills to test quantity bounds and invariants."""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    ns = f"test_flood_{uuid.uuid4().hex}"
    
    w = _fresh_wallet_with_namespace("SOLUSDT", ns)
    
    setup = _setup_position(PositionSide.LONG)
    setup["fill_chunks"] = 8  # Fill in 8 separate parts
    w.open_position("SOLUSDT", setup, mark_price=100.0)
    
    # Placed order should be filled in 8 chunks
    order = next(iter(w.orders.values()))
    assert order.status == OrderStatus.FILLED
    assert order.filled_quantity == order.quantity
    
    # We should have exactly 8 fills
    assert len(w.fills) == 8
    
    # Verify the sum of fills matches order quantity
    sum_fills = sum(f.quantity for f in w.fills)
    assert sum_fills == order.quantity
    
    # Verify all invariant checks pass
    diff = w.compare_runtime_vs_replay()
    assert diff["in_sync"] is True


def test_trigger_order_chaos(tmp_path, monkeypatch):
    """Stress tests concurrent combinations of limit, stop market, take profit, expirations, and cancellations."""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    ns = f"test_chaos_{uuid.uuid4().hex}"
    
    current_time_ms = 1_000_000
    w = _fresh_wallet_with_namespace("ADAUSDT", ns, now_ms_fn=lambda: current_time_ms)
    
    # Place a mix of active stop/limit/take profit orders
    o_limit = w.place_pending_order(
        symbol="ADAUSDT",
        side=PositionSide.LONG,
        quantity=Decimal("10"),
        order_type=OrderType.LIMIT,
        limit_price=Decimal("0.50"),
    )
    
    o_stop = w.place_pending_order(
        symbol="ADAUSDT",
        side=PositionSide.SHORT,
        quantity=Decimal("5"),
        order_type=OrderType.STOP_MARKET,
        trigger_price=Decimal("0.45"),
    )
    
    o_tp = w.place_pending_order(
        symbol="ADAUSDT",
        side=PositionSide.LONG,
        quantity=Decimal("8"),
        order_type=OrderType.TAKE_PROFIT,
        trigger_price=Decimal("0.55"),
    )
    
    o_expire = w.place_pending_order(
        symbol="ADAUSDT",
        side=PositionSide.LONG,
        quantity=Decimal("2"),
        order_type=OrderType.LIMIT,
        limit_price=Decimal("0.48"),
        expires_at=current_time_ms + 10_000,
    )
    
    # Evaluate at 0.52 (nothing triggers)
    w.evaluate_pending_orders("ADAUSDT", mark_price=0.52)
    assert w.orders[o_limit.id].status == OrderStatus.PENDING
    assert w.orders[o_stop.id].status == OrderStatus.PENDING
    assert w.orders[o_tp.id].status == OrderStatus.PENDING
    assert w.orders[o_expire.id].status == OrderStatus.PENDING
    
    # Advance time to expire the expiration order
    current_time_ms += 15_000
    w.evaluate_pending_orders("ADAUSDT", mark_price=0.52)
    assert w.orders[o_expire.id].status == OrderStatus.EXPIRED
    
    # Trigger the limit order (ADA drops to 0.49)
    w.evaluate_pending_orders("ADAUSDT", mark_price=0.49)
    assert w.orders[o_limit.id].status == OrderStatus.FILLED
    
    # Cancel the stop order
    w.cancel_order(o_stop.id, reason="Manual Chaos Cancellation")
    assert w.orders[o_stop.id].status == OrderStatus.CANCELLED
    
    # Trigger the TP order (ADA surges to 0.56)
    w.evaluate_pending_orders("ADAUSDT", mark_price=0.56)
    assert w.orders[o_tp.id].status == OrderStatus.FILLED
    
    # Diff check
    diff = w.compare_runtime_vs_replay()
    assert diff["in_sync"] is True
