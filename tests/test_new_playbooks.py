"""Tests for the new strategies + bias-aware selection.

Covers:
  - PlaybookVolExhaust (Strategy A): 3-SD band + RSI extreme + volume + reversal
  - PlaybookSweepMSS  (Strategy B): liquidity sweep + MSS + OB/FVG confluence
  - WebSocketTradingEngine._bias_factor: selection-only LLM-bias tiebreak
"""
import pandas as pd
from datetime import datetime, timedelta

from crypto_trader.playbooks import PlaybookVolExhaust, PlaybookSweepMSS
from crypto_trader.wallet import PositionSide

_BT = datetime(2026, 5, 1)


def _mk(rows):
    return pd.DataFrame([
        {
            "open_time": _BT + timedelta(minutes=15 * i),
            "close_time": _BT + timedelta(minutes=15 * (i + 1)),
            "open": o, "high": h, "low": l, "close": c,
            "volume": qv / c, "quote_volume": qv,
        }
        for i, (o, h, l, c, qv) in enumerate(rows)
    ])


# ── Strategy A: Volatility Exhaustion ──

def test_volexhaust_long_fires():
    # 38 flat candles then a deep bullish pinbar that closes below the 3-SD band,
    # crushing RSI and spiking volume.
    rows = [(100, 100.05, 99.95, 100, 100_000) for _ in range(38)]
    rows.append((86, 87.1, 80, 87, 800_000))
    setup = PlaybookVolExhaust().evaluate(_mk(rows), adx_1h=10)
    assert setup is not None
    assert setup["side"] == PositionSide.LONG
    assert setup["sl_price"] < setup["entry_price"] < setup["tp_price"]
    assert setup["score"] >= 0.60


def test_volexhaust_short_fires():
    rows = [(100, 100.05, 99.95, 100, 100_000) for _ in range(38)]
    rows.append((114, 120, 112.9, 113, 800_000))
    setup = PlaybookVolExhaust().evaluate(_mk(rows), adx_1h=10)
    assert setup is not None
    assert setup["side"] == PositionSide.SHORT
    assert setup["tp_price"] < setup["entry_price"] < setup["sl_price"]


def test_volexhaust_blocked_by_high_adx():
    rows = [(100, 100.05, 99.95, 100, 100_000) for _ in range(38)]
    rows.append((86, 87.1, 80, 87, 800_000))
    assert PlaybookVolExhaust().evaluate(_mk(rows), adx_1h=99) is None


def test_volexhaust_no_signal_on_flat_market():
    rows = [(100, 100.5, 99.5, 100, 100_000) for _ in range(45)]
    assert PlaybookVolExhaust().evaluate(_mk(rows), adx_1h=10) is None


# ── Strategy B: Liquidity Sweep + MSS ──

def _flat_1h(n=40):
    return pd.DataFrame([
        {
            "open_time": _BT + timedelta(hours=i), "close_time": _BT + timedelta(hours=i + 1),
            "open": 100, "high": 101, "low": 99, "close": 100.0,
            "volume": 1000, "quote_volume": 100_000,
        }
        for i in range(n)
    ])


def _long_structures():
    s1h = {
        "recent_sweep": {"type": "BULLISH", "candles_ago": 1, "swept_price": 98.6},
        "active_swing_highs": [{"price": 104.0}, {"price": 107.0}],
        "active_swing_lows": [{"price": 96.0}],
        "unmitigated_order_blocks": [{"type": "BULLISH", "low": 99.0, "high": 100.5}],
        "unmitigated_fvgs": [],
    }
    sltf = {"recent_bos": {"type": "BULLISH", "candles_ago": 3},
            "unmitigated_order_blocks": [], "unmitigated_fvgs": []}
    return s1h, sltf


def test_sweep_long_fires():
    df = _flat_1h()
    s1h, sltf = _long_structures()
    setup = PlaybookSweepMSS().evaluate(df, df, s1h, sltf)
    assert setup is not None
    assert setup["side"] == PositionSide.LONG
    assert setup["sl_price"] < setup["entry_price"]
    assert setup["tp_levels"][0]["price"] > setup["entry_price"]


def test_sweep_requires_mss_in_same_direction():
    df = _flat_1h()
    s1h, _ = _long_structures()
    opposing = {"recent_bos": {"type": "BEARISH", "candles_ago": 3},
                "unmitigated_order_blocks": [], "unmitigated_fvgs": []}
    assert PlaybookSweepMSS().evaluate(df, df, s1h, opposing) is None


def test_sweep_rejects_stale_sweep():
    df = _flat_1h()
    s1h, sltf = _long_structures()
    s1h["recent_sweep"]["candles_ago"] = 99
    assert PlaybookSweepMSS().evaluate(df, df, s1h, sltf) is None


def test_sweep_needs_structure():
    df = _flat_1h()
    _, sltf = _long_structures()
    assert PlaybookSweepMSS().evaluate(df, df, None, sltf) is None


# ── Selection: bias-aware tiebreak ──

def test_bias_factor():
    from crypto_trader.strategies.router import StrategyRouter as E

    class _Advice:
        def __init__(self, rec):
            self.recommended_bias = rec

    assert E._bias_factor(PositionSide.LONG, None) == 1.0
    assert E._bias_factor(PositionSide.LONG, _Advice("any")) == 1.0
    assert E._bias_factor(PositionSide.LONG, _Advice("long_only")) == 1.0
    assert E._bias_factor(PositionSide.SHORT, _Advice("long_only")) < 1.0
    assert E._bias_factor(PositionSide.LONG, _Advice("short_only")) < 1.0
    assert E._bias_factor(PositionSide.LONG, _Advice("none")) < 1.0


# ── Part 4: maker-limit entry (paper simulation) ──

def test_maker_limit_paper_fills_at_signal_price_with_maker_fee():
    """A maker-limit entry in paper mode fills exactly at the signal price (no
    spread/collar penalty) and pays the maker fee, unlike a market entry."""
    import uuid
    from decimal import Decimal
    from crypto_trader.wallet import DATA_DIR, EnhancedFuturesWallet, Playbook, PositionSide

    def fresh(sym):
        ns = f"test_{uuid.uuid4().hex}"
        return EnhancedFuturesWallet(symbol=sym, state_namespace=ns)

    setup = {
        "entry_price": "100", "side": PositionSide.LONG, "playbook": Playbook.INTRADAY,
        "sl_price": "90", "tp_price": "110", "entry_order_style": "maker_limit",
    }
    w = fresh("BTCUSDT")
    pos = w.open_position("BTCUSDT", setup, mark_price=100, custom_quantity=1.0)
    assert pos is not None
    # Maker-limit paper fill is exactly the signal price, no spread degradation.
    assert Decimal(str(pos.entry_price)) == Decimal("100")
    # Fee charged at the maker rate on notional.
    fill = w.fills[-1]
    expected = (Decimal("100") * Decimal("1")) * w.maker_fee_rate
    assert abs(Decimal(str(fill.fee)) - expected) < Decimal("1e-9")


# ── Regression: EnhancedPosition liquidation price (CRITICAL bug fix) ──

def test_liquidation_price_computed_and_no_attribute_error():
    """Regression for: 'EnhancedPosition' object has no attribute 'liquidation_price'.
    The wallet exposes a liquidation_price(pos) helper; long liq < entry, short > entry."""
    import uuid
    from decimal import Decimal
    from crypto_trader.wallet import EnhancedFuturesWallet, Playbook, PositionSide

    def fresh(sym):
        return EnhancedFuturesWallet(symbol=sym, state_namespace=f"test_{uuid.uuid4().hex}")

    w = fresh("BTCUSDT")
    lp = w.open_position("BTCUSDT", {
        "entry_price": "100", "side": PositionSide.LONG, "playbook": Playbook.INTRADAY,
        "sl_price": "90", "tp_price": "110",
    }, mark_price=100, custom_quantity=1.0)
    assert lp is not None
    liq_long = w.liquidation_price(lp)
    assert liq_long > 0 and liq_long < Decimal("100")

    w2 = fresh("ETHUSDT")
    sp = w2.open_position("ETHUSDT", {
        "entry_price": "100", "side": PositionSide.SHORT, "playbook": Playbook.INTRADAY,
        "sl_price": "110", "tp_price": "90",
    }, mark_price=100, custom_quantity=1.0)
    assert sp is not None
    liq_short = w2.liquidation_price(sp)
    assert liq_short > Decimal("100")
