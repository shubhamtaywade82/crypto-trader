"""SMC structure: CHoCH detection + order-block impulse anchoring."""
import pandas as pd

from crypto_trader.structure import MarketStructureAnalyzer


def _candle(o, c, wick=0.3, vol=1000):
    return dict(open=o, high=max(o, c) + wick, low=min(o, c) - wick,
                close=c, volume=vol, quote_volume=vol)


def _df(closes):
    rows, prev = [], closes[0]
    for c in closes[1:]:
        rows.append(_candle(prev, c))
        prev = c
    df = pd.DataFrame(rows)
    df["open_time"] = range(len(df))
    return df


def test_choch_detected_after_opposite_bos():
    # Rally that breaks a swing high (bullish BOS, bias→+1), then a decline that
    # breaks a swing low — that counter-trend break must be tagged CHoCH.
    closes = [100, 102, 104, 106, 108, 110, 109, 107, 105, 103, 101, 100,
              101, 103, 106, 109, 112, 114, 113, 111, 110, 109, 108, 109,
              110, 109, 107, 105, 103, 101, 99, 97]
    r = MarketStructureAnalyzer(_df(closes), swing_window=3).analyze()

    # A CHoCH must have been recorded, and it must be a bearish reversal.
    assert r["recent_choch"] is not None
    assert r["recent_choch"]["event"] == "CHOCH"
    assert r["recent_choch"]["type"] == "BEARISH"
    # Bias ends bearish after the down-breaks.
    assert r["trend_bias"] == -1
    # Every structural break carries an explicit BOS/CHoCH tag.
    assert r["recent_bos"]["event"] in ("BOS", "CHOCH")


def test_first_break_is_bos_not_choch():
    # A single clean bullish break from a neutral start is a BOS (continuation),
    # never a CHoCH (which requires a prior opposite bias).
    closes = [100, 99, 98, 97, 98, 99, 100, 101, 102, 103, 105, 107, 109, 111]
    r = MarketStructureAnalyzer(_df(closes), swing_window=3).analyze()
    if r["recent_bos"]:
        assert r["recent_bos"]["event"] == "BOS"
    assert r["recent_choch"] is None


def test_order_block_anchored_to_impulse_not_swing():
    # Build: swing high forms early, a long pullback, then ONE down candle
    # immediately before the impulse that breaks the swing high. The bullish OB
    # must anchor to that impulse-origin down candle (index AFTER the swing
    # point), proving the fix — the old code anchored back at the swing index.
    # Construct explicit candles to control bull/bear and the impulse origin.
    # (≥20 bars required for the analyzer to engage, so prepend rising filler.)
    lead = [_candle(90 + i, 91 + i) for i in range(8)]              # 0..7 filler
    core = [
        _candle(100, 101), _candle(101, 102), _candle(102, 103),   # 8,9,10
        _candle(103, 105), _candle(105, 104), _candle(104, 103),   # 11 swing high ~105, 12,13 down
        _candle(103, 102), _candle(102, 101), _candle(101, 100),   # 14,15,16
        _candle(100, 101), _candle(101, 102), _candle(102, 101),   # 17,18, 19 DOWN: impulse origin
        _candle(101, 103), _candle(103, 106), _candle(106, 108),   # 20,21 impulse up — 21 closes >105 => BOS
    ]
    df = pd.DataFrame(lead + core)
    df["open_time"] = range(len(df))
    r = MarketStructureAnalyzer(df, swing_window=3).analyze()

    bull_obs = [o for o in r["unmitigated_order_blocks"] if o["type"] == "BULLISH"]
    assert bull_obs, "expected a bullish order block from the break"
    ob = bull_obs[-1]
    # OB candle must be a DOWN candle (bearish origin of the up-impulse) ...
    assert ob["close"] < ob["open"]
    # ... and anchored near the impulse (idx ~19), NOT back at the swing high
    # (~idx 11). The old swing-anchored code would have produced an index ≤ 11.
    assert ob["index"] >= 15
