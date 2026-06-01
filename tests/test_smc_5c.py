"""PlaybookSMC5C — 5-condition both-side entry, fixed 2:1, engine-interface port."""
import pandas as pd

from crypto_trader.strategies.smc_5c import (
    PlaybookSMC5C, trend_dir, bos_dir, fvg_recent,
)
from crypto_trader.config import TradingConfig
from crypto_trader.wallet import PositionSide


def _frame(closes, vol=1000):
    rows, prev = [], closes[0]
    for c in closes[1:]:
        rows.append(dict(open=prev, high=max(prev, c) + 0.1, low=min(prev, c) - 0.1,
                         close=c, volume=vol, quote_volume=vol)); prev = c
    df = pd.DataFrame(rows)
    df["open_time"] = pd.to_datetime(range(len(df)), unit="m", utc=True)
    df["close_time"] = df["open_time"]
    return df


def _bull_setup():
    df4 = _frame([100 + i * 0.3 for i in range(260)])         # 4H uptrend EMA50>EMA200
    oneh = [120 + i * 0.05 for i in range(40)]; oneh[-1] = oneh[-2] + 2.0   # 1H BOS up
    df1 = _frame(oneh)
    df15 = _frame([121 + i * 0.05 for i in range(40)])        # 15m + injected bull FVG
    df15.loc[df15.index[-1], "low"] = df15["high"].iloc[-3] + 0.5
    df15.loc[df15.index[-1], "high"] = df15["low"].iloc[-1] + 0.3
    de = _frame([122 + i * 0.05 for i in range(260)])         # 5m entry + ATR/vol spike
    de.loc[de.index[-1], "high"] = de["close"].iloc[-1] + 3.0
    de.loc[de.index[-1], "low"] = de["close"].iloc[-1] - 3.0
    de.loc[de.index[-1], "volume"] = 100000
    return de, {"15m": df15, "1h": df1, "4h": df4}


def test_helpers():
    up = _frame([100 + i * 0.3 for i in range(260)])
    assert trend_dir(up["close"], 50, 200) == 1
    dn = _frame([200 - i * 0.3 for i in range(260)])
    assert trend_dir(dn["close"], 50, 200) == -1
    oneh = [120 + i * 0.05 for i in range(40)]; oneh[-1] = oneh[-2] + 2.0
    assert bos_dir(_frame(oneh), 5) == 1


def test_long_signal_fixed_2to1():
    de, htf = _bull_setup()
    pb = PlaybookSMC5C(entry_timeframe="5m")
    s = pb.evaluate(de, htf)
    assert s is not None and s["side"] == PositionSide.LONG
    rr = (s["tp_price"] - s["entry_price"]) / (s["entry_price"] - s["sl_price"])
    assert abs(rr - 2.0) < 1e-6
    assert s["strategy"] == "smc_5c"


def test_trend_gate_blocks_when_no_4h_trend():
    de, htf = _bull_setup()
    htf["4h"] = _frame([100] * 260)   # flat 4H → EMA50==EMA200 → undecided
    pb = PlaybookSMC5C()
    assert pb.evaluate(de, htf) is None
    assert pb.last_eval and pb.last_eval.get("stage") == "trend"


def test_insufficient_bars_returns_none():
    pb = PlaybookSMC5C()
    short = _frame([100 + i for i in range(10)])
    assert pb.evaluate(short, {}) is None


def test_config_from_env(monkeypatch):
    monkeypatch.setenv("STRATEGY_MODE", "smc_5c")
    monkeypatch.setenv("SMC5C_TP_PCT", "0.012")
    monkeypatch.setenv("SMC5C_SL_PCT", "0.006")
    cfg = TradingConfig.from_env()
    assert cfg.strategy_mode == "smc_5c"
    assert cfg.smc5c_tp_pct == 0.012 and cfg.smc5c_sl_pct == 0.006
