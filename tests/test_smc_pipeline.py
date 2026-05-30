"""PlaybookSMC — gates, weighted scoring, and sane SL/TP."""
import pandas as pd

from crypto_trader.strategies.smc_pipeline import PlaybookSMC
from crypto_trader.wallet import PositionSide


def _candle(o, c, hi=None, lo=None, vol=1000):
    return dict(
        open=o,
        high=hi if hi is not None else max(o, c) + 0.2,
        low=lo if lo is not None else min(o, c) - 0.2,
        close=c, volume=vol, quote_volume=vol,
    )


def _htf_uptrend(n=60, start=100.0, step=1.0):
    rows, p = [], start
    for _ in range(n):
        rows.append(_candle(p, p + step)); p += step
    df = pd.DataFrame(rows); df["open_time"] = range(len(df))
    return df


def _bullish_setup_df():
    """Lead uptrend → swing low ~96.8 + minor swing high ~104 → SSL sweep →
    displacement up that breaks the high (BOS) and leaves an FVG, big volume."""
    lead = list(range(84, 100))                       # rising filler
    mid = [100, 99, 98, 97, 98, 99, 100, 102, 104, 103, 102, 101, 100, 99, 98]
    rows, prev = [], lead[0]
    for c in lead[1:] + mid:
        rows.append(_candle(prev, c)); prev = c
    rows.append(_candle(98, 99, lo=95.0, vol=2000))    # sweep wick below 96.8, close back above
    rows.append(_candle(99, 103, vol=4500))            # displacement, big vol
    rows.append(_candle(103.2, 106.5, lo=101.0, vol=6000))  # close>104 → BOS, FVG, big vol
    df = pd.DataFrame(rows); df["open_time"] = range(len(df))
    return df


def test_bullish_pipeline_fires_with_sane_risk():
    pb = PlaybookSMC(min_score=70.0)
    setup = pb.evaluate(
        _bullish_setup_df(), _htf_uptrend(),
        oi_delta=None, oi_available=False,
        rvol=1.6, liquidation_intensity=0.0,
    )
    assert setup is not None
    assert setup["side"] == PositionSide.LONG
    assert setup["confidence_score"] >= 70.0
    # every scored factor present in the breakdown
    for k in ("htf", "sweep", "structure", "ob", "fvg", "vol", "rvol", "liq"):
        assert k in setup["factors"]
    # OI was unavailable → excluded from the breakdown (renormalised, not 0-scored)
    assert "oi" not in setup["factors"]
    # SL below entry, TP above, and risk capped to a tradeable band (<= ~ATR mult,
    # not the full sweep-wick distance).
    entry, sl, tp = setup["entry_price"], setup["sl_price"], setup["tp_price"]
    assert sl < entry < tp
    risk_pct = (entry - sl) / entry
    assert risk_pct < 0.05, f"stop too wide: {risk_pct:.3%}"
    rr = (tp - entry) / (entry - sl)
    assert 1.0 <= rr <= pb.tp_rr + 1e-6


def test_htf_gate_blocks_when_no_trend():
    pb = PlaybookSMC(min_score=70.0)
    flat = pd.DataFrame([_candle(100, 100) for _ in range(60)])
    flat["open_time"] = range(60)
    assert pb.evaluate(_bullish_setup_df(), flat, oi_available=False, rvol=1.6) is None
    assert pb.last_eval and pb.last_eval.get("stage") == "htf_bias"


def test_liquidation_cascade_gate_rejects():
    pb = PlaybookSMC(min_score=70.0, liq_cascade_threshold=0.5)
    setup = pb.evaluate(
        _bullish_setup_df(), _htf_uptrend(),
        oi_available=False, rvol=1.6, liquidation_intensity=0.9,
    )
    assert setup is None
    assert pb.last_eval and pb.last_eval.get("stage") == "liquidation"


def test_insufficient_bars_returns_none():
    pb = PlaybookSMC()
    short = pd.DataFrame([_candle(100, 101) for _ in range(10)])
    short["open_time"] = range(10)
    assert pb.evaluate(short, _htf_uptrend(), oi_available=False) is None


def test_oi_factor_included_when_available():
    pb = PlaybookSMC(min_score=70.0)
    setup = pb.evaluate(
        _bullish_setup_df(), _htf_uptrend(),
        oi_delta=5.0, oi_available=True,   # 5% > 3% target → full OI factor
        rvol=1.6, liquidation_intensity=0.0,
    )
    assert setup is not None
    assert "oi" in setup["factors"]
    assert setup["factors"]["oi"] == 1.0
