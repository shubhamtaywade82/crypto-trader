"""PlaybookMTFAlignment — macro-aligned breakout+engulf entry, wide-R target."""
import pandas as pd

from crypto_trader.strategies.mtf_alignment import PlaybookMTFAlignment
from crypto_trader.config import TradingConfig
from crypto_trader.wallet import PositionSide


def _frame(closes, wick=0.1):
    rows, prev = [], closes[0]
    for c in closes[1:]:
        rows.append(dict(open=prev, high=max(prev, c) + wick, low=min(prev, c) - wick,
                         close=c, volume=1000, quote_volume=1000))
        prev = c
    df = pd.DataFrame(rows)
    df["open_time"] = pd.to_datetime(range(len(df)), unit="m", utc=True)
    df["close_time"] = df["open_time"]
    return df


def _macro_up():
    up = [100 + i * 0.5 for i in range(80)]          # EMA50 ~127, price ~139
    return {"1h": _frame(up), "4h": _frame(up), "1d": _frame(up)}


def _entry_bull_setup():
    # entry TF in the SAME price band as the macro frames, above their EMA50,
    # ending with a red bar then a bullish-engulfing breakout.
    base = [136 + i * 0.05 for i in range(40)]
    rows, prev = [], base[0]
    for c in base[1:]:
        rows.append(dict(open=prev, high=max(prev, c) + 0.05, low=min(prev, c) - 0.05,
                         close=c, volume=1000, quote_volume=1000)); prev = c
    rows.append(dict(open=prev, high=prev + 0.05, low=prev - 0.6, close=prev - 0.5,
                     volume=1000, quote_volume=1000))                       # red
    hi_prev = max(r["high"] for r in rows[-5:])
    rows.append(dict(open=prev - 0.6, high=hi_prev + 1.5, low=prev - 0.7, close=hi_prev + 1.2,
                     volume=2000, quote_volume=2000))                       # bull engulf breakout
    df = pd.DataFrame(rows)
    df["open_time"] = pd.to_datetime(range(len(df)), unit="m", utc=True)
    df["close_time"] = df["open_time"]
    return df


def test_long_signal_with_3R_target():
    pb = PlaybookMTFAlignment(tp_r=3.0, min_macro_frames=2)
    s = pb.evaluate(_entry_bull_setup(), _macro_up())
    assert s is not None and s["side"] == PositionSide.LONG
    rr = (s["tp_price"] - s["entry_price"]) / (s["entry_price"] - s["sl_price"])
    assert abs(rr - 3.0) < 1e-6
    assert s["strategy"] == "mtf_alignment"


def test_tp_r_configurable():
    s = PlaybookMTFAlignment(tp_r=2.0).evaluate(_entry_bull_setup(), _macro_up())
    rr = (s["tp_price"] - s["entry_price"]) / (s["entry_price"] - s["sl_price"])
    assert abs(rr - 2.0) < 1e-6


def test_no_macro_alignment_blocks(monkeypatch):
    # Macro frames disagree (1h up, 4h/1d flat-low) → no aligned bias → no trade.
    up = [100 + i * 0.5 for i in range(80)]
    down = [200 - i * 0.1 for i in range(80)]   # well above entry price → 'short' side per frame
    htf = {"1h": _frame(up), "4h": _frame(down), "1d": _frame(down)}
    pb = PlaybookMTFAlignment(min_macro_frames=3)
    assert pb.evaluate(_entry_bull_setup(), htf) is None
    assert pb.last_eval and pb.last_eval.get("stage") == "macro_bias"


def test_insufficient_macro_frames_blocks():
    pb = PlaybookMTFAlignment(min_macro_frames=3)
    # only 1 macro frame provided → below min → no trade
    assert pb.evaluate(_entry_bull_setup(), {"1h": _macro_up()["1h"]}) is None


def test_config_from_env(monkeypatch):
    monkeypatch.setenv("STRATEGY_MODE", "mtf_alignment")
    monkeypatch.setenv("MTA_TP_R", "3.0")
    monkeypatch.setenv("MTA_MACRO_TFS", "1h,4h,1d")
    cfg = TradingConfig.from_env()
    assert cfg.strategy_mode == "mtf_alignment"
    assert cfg.mta_tp_r == 3.0
    assert cfg.mta_macro_tfs == ["1h", "4h", "1d"]
