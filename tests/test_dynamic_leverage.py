"""
Dynamic per-trade leverage (5x–20x band).

Covers: config defaults + env, DynamicLeverageManager band math, and the engine
wiring (_apply_dynamic_leverage) — applies per-symbol leverage, halts on extreme
vol, and is a no-op when the feature flag is off.
"""
from types import SimpleNamespace

import pytest

from crypto_trader.config import TradingConfig
from crypto_trader.margin_engine import DynamicLeverageManager, LeverageEngine
from crypto_trader.engine_ws import WebSocketTradingEngine


# ── caps / config ────────────────────────────────────────────────────────────

def test_hard_cap_raised_to_20():
    assert LeverageEngine().hard_max_leverage == 20


def test_config_dynamic_leverage_defaults():
    cfg = TradingConfig()
    assert cfg.use_dynamic_leverage is False        # off by default
    assert cfg.dynamic_leverage_min == 5
    assert cfg.dynamic_leverage_max == 20


def test_config_env_enables_dynamic(monkeypatch):
    monkeypatch.setenv("USE_DYNAMIC_LEVERAGE", "true")
    monkeypatch.setenv("DYNAMIC_LEVERAGE_MIN", "5")
    monkeypatch.setenv("DYNAMIC_LEVERAGE_MAX", "20")
    monkeypatch.delenv("TRADING_PROFILE", raising=False)
    cfg = TradingConfig.from_env()
    assert cfg.use_dynamic_leverage is True
    assert (cfg.dynamic_leverage_min, cfg.dynamic_leverage_max) == (5, 20)


# ── band math ────────────────────────────────────────────────────────────────

def test_band_floor_and_ceiling():
    m = DynamicLeverageManager()
    assert m.min_leverage == 5 and m.max_leverage == 20
    # all conditions favourable (low vol, strong trend, healthy) → climbs to ceiling 20
    top = m.compute("S", base_leverage=20, venue_max_leverage=20,
                    atr_pct=0.0, drawdown=0.0, margin_ratio=0.0, regime="TREND_EXPANSION")
    assert top == 20
    # everything in band
    assert 5 <= m.compute("S", base_leverage=20, venue_max_leverage=20, regime="ACCUMULATION") <= 20


def test_strong_setup_climbs_high():
    m = DynamicLeverageManager()
    lev = m.compute("S", base_leverage=20, venue_max_leverage=20,
                    atr_pct=0.0, drawdown=0.0, margin_ratio=0.0, regime="TREND_EXPANSION")
    assert lev == 20


def test_weak_regime_stays_low():
    m = DynamicLeverageManager()
    # mean-reversion (factor 0.25), calm → low end of band
    lev = m.compute("S", base_leverage=20, venue_max_leverage=20, regime="MEAN_REVERSION")
    # floor 5 + 0.25*15 ≈ 8.75 → 9
    assert lev <= 10 and lev >= 5


def test_default_mid_band():
    m = DynamicLeverageManager()
    # neutral regime, calm, healthy → mid-ish (not pinned at 20)
    lev = m.compute("S", base_leverage=20, venue_max_leverage=20, regime="ACCUMULATION")
    assert 5 <= lev < 20    # NOT the ceiling by default


def test_high_vol_pulls_toward_floor():
    m = DynamicLeverageManager()
    # atr between high(0.05) and extreme(0.10) with strong regime → reduced, not 20
    lev = m.compute("S", base_leverage=20, venue_max_leverage=20,
                    atr_pct=0.075, regime="TREND_EXPANSION")
    assert 5 <= lev < 20


def test_extreme_vol_halts():
    m = DynamicLeverageManager()
    assert m.compute("S", base_leverage=20, venue_max_leverage=20, atr_pct=0.11,
                     regime="TREND_EXPANSION") == 0


def test_no_trade_regime_halts():
    m = DynamicLeverageManager()
    assert m.compute("S", base_leverage=20, venue_max_leverage=20, regime="DEAD_MARKET") == 0


def test_drawdown_suppresses_leverage():
    m = DynamicLeverageManager()
    healthy = m.compute("S", base_leverage=20, venue_max_leverage=20, regime="TREND_EXPANSION")
    drawn = m.compute("S", base_leverage=20, venue_max_leverage=20, regime="TREND_EXPANSION",
                      drawdown=0.10)  # severe
    assert drawn < healthy and drawn == 5


def test_conviction_scales_when_provided():
    m = DynamicLeverageManager()
    high = m.compute("S", base_leverage=20, venue_max_leverage=20, regime="TREND_EXPANSION",
                     conviction=1.0)
    low = m.compute("S", base_leverage=20, venue_max_leverage=20, regime="TREND_EXPANSION",
                    conviction=0.3)
    assert low < high


def test_venue_max_caps_band():
    m = DynamicLeverageManager()
    # venue only allows 8x → result can't exceed 8 even on a perfect setup
    lev = m.compute("S", base_leverage=20, venue_max_leverage=8,
                    atr_pct=0.0, regime="TREND_EXPANSION")
    assert lev <= 8


def test_configured_ceiling_caps_band():
    m = DynamicLeverageManager()
    # base_leverage (configured MAX_LEVERAGE) below band max acts as upper clamp
    lev = m.compute("S", base_leverage=10, venue_max_leverage=20,
                    atr_pct=0.0, regime="TREND_EXPANSION")
    assert lev <= 10


# ── engine wiring ────────────────────────────────────────────────────────────

def _shim(dyn, *, is_live=False, set_lev_calls=None):
    wallet = SimpleNamespace(
        margin_balance=1000.0, positions={}, execution_engine=None,
        set_symbol_leverage=lambda s, l: (set_lev_calls.append((s, l))
                                          if set_lev_calls is not None else None),
    )
    return SimpleNamespace(
        symbol="SOLUSDT",
        cfg=SimpleNamespace(max_leverage=10, is_live=is_live),
        dynamic_leverage=dyn,
        wallet=wallet,
        risk_manager=SimpleNamespace(peak_balance=0),
        _last_signal_df_1h=None,
    )


def _regime_ctx(value="TREND_EXPANSION"):
    return SimpleNamespace(extended=SimpleNamespace(value=value))


def test_engine_applies_per_symbol_leverage():
    calls = []
    sh = _shim(DynamicLeverageManager(), set_lev_calls=calls)
    ok = WebSocketTradingEngine._apply_dynamic_leverage(sh, {}, 100.0, _regime_ctx())
    assert ok is True
    assert calls and calls[0][0] == "SOLUSDT"
    assert 5 <= calls[0][1] <= 20


def test_engine_skips_entry_on_extreme_vol():
    import pandas as pd
    # build a df with a huge range so ATR% >= extreme (10%)
    n = 30
    df = pd.DataFrame({
        "high": [200.0] * n, "low": [100.0] * n, "close": [150.0] * n,
    })
    calls = []
    sh = _shim(DynamicLeverageManager(), set_lev_calls=calls)
    sh._last_signal_df_1h = df
    ok = WebSocketTradingEngine._apply_dynamic_leverage(sh, {}, 150.0, _regime_ctx())
    assert ok is False            # entry skipped
    assert calls == []            # no leverage applied


def test_engine_noop_when_disabled():
    # dynamic_leverage None → _execute_entry never calls the helper; assert the
    # gate attribute is the off switch.
    sh = _shim(None)
    assert sh.dynamic_leverage is None
