from decimal import Decimal

from crypto_trader.wallet import EnhancedFuturesWallet, Playbook, PositionSide


def _setup(side=PositionSide.LONG):
    return {
        "entry_price": "100",
        "side": side,
        "playbook": Playbook.INTRADAY,
        "sl_price": "90" if side == PositionSide.LONG else "110",
        "tp_price": "110" if side == PositionSide.LONG else "90",
    }


def test_order_fill_lifecycle_and_replay(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    w = EnhancedFuturesWallet(symbol="BTCUSDT", state_namespace="t1")
    p = w.open_position("BTCUSDT", _setup(), mark_price=100)
    assert p is not None
    assert len(w.orders) == 1
    assert len(w.fills) == 1

    replay = w.replay_portfolio_state()
    assert len(replay.orders) == 1
    assert len(replay.open_positions) == 1


def test_partial_close_and_close_updates_replay(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    w = EnhancedFuturesWallet(symbol="ETHUSDT", state_namespace="t2")
    w.open_position("ETHUSDT", _setup(), mark_price=100)
    w.partial_close("ETHUSDT", mark_price=105, pct=0.5, reason="T1")
    w.close_position("ETHUSDT", mark_price=106, reason="T2")

    replay = w.replay_portfolio_state()
    assert len(replay.open_positions) == 0
    assert replay.realized_pnl_total != Decimal("0")


def test_invariant_filled_qty_leq_order_qty(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    w = EnhancedFuturesWallet(symbol="SOLUSDT", state_namespace="t3")
    w.open_position("SOLUSDT", _setup(), mark_price=100)
    order = next(iter(w.orders.values()))
    assert order.filled_quantity <= order.quantity


def test_replay_out_of_order_guard(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    w = EnhancedFuturesWallet(symbol="XRPUSDT", state_namespace="t4")
    w.open_position("XRPUSDT", _setup(), mark_price=100)

    events = w.events_file.read_text(encoding="utf-8").splitlines()
    if len(events) >= 2:
        w.events_file.write_text("\n".join(reversed(events)) + "\n", encoding="utf-8")

    try:
        w.replay_portfolio_state()
        assert False, "Expected out-of-order event detection"
    except ValueError as exc:
        assert "Out-of-order" in str(exc)
