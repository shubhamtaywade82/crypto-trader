"""
crypto_trader.selection.selector — score every strategy per symbol, pick champion.

For each symbol: fetch klines, take an out-of-sample TEST slice (last
``test_frac``), run each candidate strategy's faithful backtest on it, compute
NET metrics (fees modelled), gate, and choose the champion = the passing
candidate with the highest net expectancy. Symbols where nothing passes get
``strategy_mode: "none"`` (the bot won't trade them).

Fixed-rule strategies are not parameter-fit here, so test-slice performance is a
fair out-of-sample read. Liquidity-sweep is swept over a small TP grid (its only
real knob) and the best TP is recorded per symbol.

This is OFFLINE. It writes ``strategy_champions.json``; the live bot only reads it.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Dict, List

from ..analytics import metrics as M
from ..backtest.smc_cli import fetch_klines
from ..backtest.engine import run_backtest as run_mr
from ..backtest.st2_engine import run_backtest_st2
from ..backtest.smc_engine import run_backtest_smc
from ..backtest.lsw_engine import run_backtest_lsw
from ..backtest.mta_engine import run_backtest_mta
from ..backtest.smc5c_engine import run_backtest_smc5c
from .champions import CHAMPIONS_PATH

logger = logging.getLogger("crypto_trader.selection.selector")

# Acceptance gates (net, out-of-sample). A champion must clear ALL.
GATES = {"min_trades": 20, "min_profit_factor": 1.1, "min_expectancy_r": 0.0}


def _tail(df, frac):
    cut = int(len(df) * (1.0 - frac))
    return df.iloc[cut:].reset_index(drop=True)


def _metrics(res):
    m = M.compute_metrics(res.trades)
    return {
        "oos_trades": m["total_trades"],
        "oos_win_rate": round(m["win_rate"], 4),
        "oos_expectancy_r": round(m["expectancy_r"], 4),
        "oos_pf": round(m["profit_factor"], 4),
    }


def _candidates(df15, df1h, df4h, df5m, df1d, symbol, taker_fee, leverage):
    """Yield (strategy_mode, params, metrics) for every candidate."""
    out = []
    # mean_reversion (15m)
    try:
        r = run_mr(df15, symbol=symbol, timeframe="15m",
                   taker_fee=taker_fee, leverage=leverage)
        out.append(("mean_reversion", {}, _metrics(r)))
    except Exception as e:
        logger.warning("[selector] %s mean_reversion failed: %s", symbol, e)
    # supertrend2 (1h + 4h)
    try:
        r = run_backtest_st2(df1h, df4h, symbol=symbol, timeframe="1h",
                             htf_timeframe="4h", taker_fee=taker_fee, leverage=leverage)
        out.append(("supertrend2", {}, _metrics(r)))
    except Exception as e:
        logger.warning("[selector] %s supertrend2 failed: %s", symbol, e)
    # smc (15m + 4h)
    try:
        r = run_backtest_smc(df15, df4h, symbol=symbol, timeframe="15m",
                             htf_timeframe="4h", taker_fee=taker_fee, leverage=leverage)
        out.append(("smc", {}, _metrics(r)))
    except Exception as e:
        logger.warning("[selector] %s smc failed: %s", symbol, e)
    # liquidity_sweep (15m + 4h), small TP grid (its only real knob)
    for tp_r in (2.0, 2.5, 3.0):
        try:
            r = run_backtest_lsw(df15, df4h, symbol=symbol, timeframe="15m",
                                 htf_timeframe="4h", tp_r=tp_r,
                                 taker_fee=taker_fee, leverage=leverage)
            out.append(("liquidity_sweep", {"lsw_tp_r": tp_r}, _metrics(r)))
        except Exception as e:
            logger.warning("[selector] %s liquidity_sweep tp=%s failed: %s", symbol, tp_r, e)
    # mtf_alignment (5m exec + 1h/4h/1d macro), TP grid
    if df5m is not None:
        macro = {"1h": df1h, "4h": df4h, "1d": df1d}
        for tp_r in (2.5, 3.0):
            try:
                r = run_backtest_mta(df5m, macro, symbol=symbol, timeframe="5m",
                                     tp_r=tp_r, taker_fee=taker_fee, leverage=leverage)
                out.append(("mtf_alignment", {"mta_tp_r": tp_r}, _metrics(r)))
            except Exception as e:
                logger.warning("[selector] %s mtf_alignment tp=%s failed: %s", symbol, tp_r, e)
    # smc_5c (5m exec + 15m/1h/4h HTF), fixed 2:1 from the ML report
    if df5m is not None:
        htf5c = {"15m": df15, "1h": df1h, "4h": df4h}
        try:
            r = run_backtest_smc5c(df5m, htf5c, symbol=symbol, timeframe="5m",
                                   taker_fee=taker_fee, leverage=leverage)
            out.append(("smc_5c", {}, _metrics(r)))
        except Exception as e:
            logger.warning("[selector] %s smc_5c failed: %s", symbol, e)
    return out


def select_symbol(symbol: str, days: int, test_frac: float,
                  taker_fee: float, leverage: int) -> Dict:
    df15 = fetch_klines(symbol, "15m", int(days * 96))
    df1h = fetch_klines(symbol, "1h", int(days * 24))
    df4h = fetch_klines(symbol, "4h", int(days * 6))
    df5m = fetch_klines(symbol, "5m", int(days * 288))
    df1d = fetch_klines(symbol, "1d", max(int(days), 60))
    df15, df1h, df4h = _tail(df15, test_frac), _tail(df1h, test_frac), _tail(df4h, test_frac)
    df5m = _tail(df5m, test_frac)
    # 1d not tailed — keep full history so EMA50(1d) is warm at the OOS boundary.

    cands = _candidates(df15, df1h, df4h, df5m, df1d, symbol, taker_fee, leverage)
    passing = [
        c for c in cands
        if c[2]["oos_trades"] >= GATES["min_trades"]
        and c[2]["oos_pf"] >= GATES["min_profit_factor"]
        and c[2]["oos_expectancy_r"] > GATES["min_expectancy_r"]
    ]
    passing.sort(key=lambda c: c[2]["oos_expectancy_r"], reverse=True)
    return {"symbol": symbol, "candidates": cands, "winner": (passing[0] if passing else None)}


def run(symbols: List[str], days: int = 150, test_frac: float = 0.35,
        taker_fee: float = 0.00059, leverage: int = 1,
        ts_ms: int = 0) -> Dict:
    """Score all symbols and build the champions payload (does NOT write)."""
    champions: Dict[str, Dict] = {}
    detail: Dict[str, list] = {}
    for sym in symbols:
        res = select_symbol(sym, days, test_frac, taker_fee, leverage)
        detail[sym] = res["candidates"]
        w = res["winner"]
        if w is None:
            champions[sym] = {"strategy_mode": "none", "reason": "no candidate passed gates"}
        else:
            mode, params, met = w
            champions[sym] = {"strategy_mode": mode, "params": params, **met}
    return {"generated_at": ts_ms, "lookback_days": days,
            "test_frac": test_frac, "gates": GATES, "champions": champions, "_detail": detail}


def write_champions(payload: Dict, path=None) -> str:
    p = path or CHAMPIONS_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    # Drop the verbose per-candidate detail from the persisted file.
    persisted = {k: v for k, v in payload.items() if k != "_detail"}
    with open(p, "w") as f:
        json.dump(persisted, f, indent=2)
    return str(p)
