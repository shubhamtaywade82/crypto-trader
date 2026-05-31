"""Tests for StopLossCalculator and TakeProfitCalculator."""

import numpy as np
import pandas as pd
import pytest

from crypto_trader.position_management.sl_tp_calculator import (
    StopLossCalculator,
    TakeProfitCalculator,
)


def _make_df(n: int = 100, trend: str = "up", base: float = 100.0) -> pd.DataFrame:
    """Generate synthetic OHLCV data."""
    np.random.seed(42)
    step = 0.5 if trend == "up" else -0.5
    closes = [base + i * step + np.random.normal(0, 0.3) for i in range(n)]
    highs = [c + abs(np.random.normal(0, 0.5)) for c in closes]
    lows = [c - abs(np.random.normal(0, 0.5)) for c in closes]
    opens = [closes[i - 1] if i > 0 else closes[0] for i in range(n)]
    volumes = [abs(np.random.normal(1000, 200)) for _ in range(n)]
    ts = list(range(1_600_000_000_000, 1_600_000_000_000 + n * 3_600_000, 3_600_000))
    return pd.DataFrame({
        "open": opens, "high": highs, "low": lows, "close": closes,
        "volume": volumes, "open_time": ts, "close_time": ts,
    })


class TestStopLossCalculator:
    def test_atr_sl_below_entry_for_long(self):
        df = _make_df()
        entry = float(df["close"].iloc[-1])
        calc = StopLossCalculator(df=df, symbol="BTCUSDT", side="LONG", entry_price=entry)
        result = calc.calculate(style="atr")
        assert result.price < entry, "ATR SL for LONG must be below entry"
        assert result.atr_used > 0

    def test_atr_sl_above_entry_for_short(self):
        df = _make_df(trend="down")
        entry = float(df["close"].iloc[-1])
        calc = StopLossCalculator(df=df, symbol="BTCUSDT", side="SHORT", entry_price=entry)
        result = calc.calculate(style="atr")
        assert result.price > entry, "ATR SL for SHORT must be above entry"

    def test_swing_sl_below_entry_for_long(self):
        df = _make_df()
        entry = float(df["close"].iloc[-1])
        calc = StopLossCalculator(df=df, symbol="BTCUSDT", side="LONG", entry_price=entry)
        result = calc.calculate(style="swing_low")
        assert result.price < entry

    def test_pct_sl_uses_configured_percentage(self):
        df = _make_df()
        entry = 100.0
        pct = 0.01
        calc = StopLossCalculator(df=df, symbol="X", side="LONG", entry_price=entry, pct_default=pct)
        result = calc.calculate(style="pct_from_entry")
        assert abs(result.price - (entry * (1 - pct))) < 0.001

    def test_unknown_style_falls_back_gracefully(self):
        df = _make_df()
        entry = float(df["close"].iloc[-1])
        calc = StopLossCalculator(df=df, symbol="X", side="LONG", entry_price=entry)
        result = calc.calculate(style="nonexistent_style")
        assert result.price < entry

    def test_insufficient_data_returns_pct_fallback(self):
        df = _make_df(n=3)
        entry = 100.0
        calc = StopLossCalculator(df=df, symbol="X", side="LONG", entry_price=entry)
        result = calc.calculate(style="swing_low")
        assert result.price < entry


class TestTakeProfitCalculator:
    def test_fixed_rr_tp_above_entry_for_long(self):
        df = _make_df()
        entry = 100.0
        sl = 97.0
        calc = TakeProfitCalculator(df=df, symbol="X", side="LONG", entry_price=entry, stop_loss=sl)
        result = calc.calculate(style="fixed_rr", rr_ratios=[2.0])
        assert len(result.levels) == 1
        assert result.levels[0] > entry, "TP for LONG must be above entry"
        assert abs(result.levels[0] - 106.0) < 0.01  # entry + 2 * (100-97)

    def test_fixed_rr_tp_below_entry_for_short(self):
        df = _make_df(trend="down")
        entry = 100.0
        sl = 103.0
        calc = TakeProfitCalculator(df=df, symbol="X", side="SHORT", entry_price=entry, stop_loss=sl)
        result = calc.calculate(style="fixed_rr", rr_ratios=[2.0])
        assert result.levels[0] < entry

    def test_multi_level_returns_multiple_targets(self):
        df = _make_df()
        entry = 100.0
        sl = 97.0
        calc = TakeProfitCalculator(df=df, symbol="X", side="LONG", entry_price=entry, stop_loss=sl)
        result = calc.calculate(style="multi_level", rr_ratios=[1.5, 2.5, 4.0])
        assert len(result.levels) == 3
        # Levels should be ascending for LONG
        assert result.levels[0] < result.levels[1] < result.levels[2]

    def test_atr_expansion_tp(self):
        df = _make_df()
        entry = float(df["close"].iloc[-1])
        sl = entry * 0.98
        calc = TakeProfitCalculator(df=df, symbol="X", side="LONG", entry_price=entry, stop_loss=sl)
        result = calc.calculate(style="atr_expansion")
        assert result.levels[0] > entry

    def test_liquidity_target_finds_swing_high(self):
        df = _make_df(n=50, trend="up")
        entry = float(df["close"].iloc[25])  # midpoint
        sl = entry * 0.98
        calc = TakeProfitCalculator(df=df, symbol="X", side="LONG", entry_price=entry, stop_loss=sl)
        result = calc.calculate(style="liquidity_target")
        assert len(result.levels) >= 1
        assert result.levels[0] > entry
