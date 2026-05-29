"""
crypto_trader.analytics.metrics — performance aggregation.

Pure functions over a list of TradeOutcomeRecord (+ optional equity snapshots).
No live dependencies, no I/O beyond what callers pass in — keeps it trivially
testable and safe to import from the API service.

The aggregator answers the observability questions the bot was previously blind
to: win-rate, equity curve, per-regime PnL, MFE/MAE capture, Sharpe, profit
factor, expectancy. LLM attribution is delegated to the existing
TradeJournal.analyze_llm_contribution.
"""
from __future__ import annotations

import math
from statistics import mean, pstdev
from typing import Dict, List, Optional, Sequence

from ..journal import TradeOutcomeRecord


def _pnls(records: Sequence[TradeOutcomeRecord]) -> List[float]:
    return [float(r.realized_pnl) for r in records if r.realized_pnl is not None]


def win_rate(records: Sequence[TradeOutcomeRecord]) -> float:
    pnls = _pnls(records)
    if not pnls:
        return 0.0
    return round(sum(1 for p in pnls if p > 0) / len(pnls), 4)


def profit_factor(records: Sequence[TradeOutcomeRecord]) -> Optional[float]:
    """gross_win / gross_loss. None when there are no losses (undefined)."""
    pnls = _pnls(records)
    gross_win = sum(p for p in pnls if p > 0)
    gross_loss = -sum(p for p in pnls if p < 0)
    if gross_loss == 0:
        return None
    return round(gross_win / gross_loss, 4)


def expectancy(records: Sequence[TradeOutcomeRecord]) -> Dict[str, float]:
    """Per-trade expected value in currency and (when available) R-multiples."""
    pnls = _pnls(records)
    out: Dict[str, float] = {"currency": 0.0}
    if pnls:
        out["currency"] = round(mean(pnls), 4)
    r_multiples = [float(r.pnl_r) for r in records if r.pnl_r is not None]
    if r_multiples:
        out["r"] = round(mean(r_multiples), 4)
    return out


def sharpe(records: Sequence[TradeOutcomeRecord], periods_per_year: float = 365.0) -> Optional[float]:
    """Annualized Sharpe of the per-trade return series (treats each trade as a
    period). Returns None when fewer than 2 trades or zero variance."""
    pnls = _pnls(records)
    if len(pnls) < 2:
        return None
    sd = pstdev(pnls)
    if sd == 0:
        return None
    return round((mean(pnls) / sd) * math.sqrt(periods_per_year), 4)


def per_regime_pnl(records: Sequence[TradeOutcomeRecord]) -> Dict[str, Dict]:
    groups: Dict[str, List[TradeOutcomeRecord]] = {}
    for r in records:
        groups.setdefault(r.regime or "unknown", []).append(r)
    return {
        regime: {
            "trades": len(rs),
            "pnl": round(sum(_pnls(rs)), 4),
            "win_rate": win_rate(rs),
            "expectancy": expectancy(rs).get("currency", 0.0),
        }
        for regime, rs in groups.items()
    }


def _percentiles(values: List[float], ps=(25, 50, 75, 90)) -> Dict[str, float]:
    if not values:
        return {f"p{p}": 0.0 for p in ps}
    s = sorted(values)
    out = {}
    for p in ps:
        # linear-interpolation percentile
        k = (len(s) - 1) * (p / 100.0)
        lo = math.floor(k)
        hi = math.ceil(k)
        if lo == hi:
            out[f"p{p}"] = round(s[int(k)], 6)
        else:
            out[f"p{p}"] = round(s[lo] * (hi - k) + s[hi] * (k - lo), 6)
    return out


def mfe_mae_distribution(records: Sequence[TradeOutcomeRecord]) -> Dict[str, Dict]:
    mfes = [float(r.mfe) for r in records if r.mfe is not None]
    maes = [float(r.mae) for r in records if r.mae is not None]
    # capture ratio = realized move vs best move seen (how much of MFE was kept)
    captures = []
    for r in records:
        if r.mfe and r.mfe != 0 and r.realized_pnl is not None and r.entry_price:
            realized_pct = (r.realized_pnl / (abs(r.quantity) * r.entry_price)) if r.quantity else 0.0
            captures.append(realized_pct / r.mfe)
    return {
        "mfe": _percentiles(mfes),
        "mae": _percentiles(maes),
        "capture_ratio": _percentiles(captures),
    }


def equity_curve(snapshots: Optional[Sequence[dict]]) -> List[Dict]:
    """Build (ts, equity) points from wallet snapshot rows. Each row is expected
    to carry 'ts' and 'wallet_balance' (+ optional 'realized_pnl_total')."""
    if not snapshots:
        return []
    curve = []
    for s in snapshots:
        ts = s.get("ts")
        equity = s.get("wallet_balance")
        if ts is None or equity is None:
            continue
        curve.append({"ts": int(ts), "equity": round(float(equity), 4)})
    curve.sort(key=lambda p: p["ts"])
    return curve


def equity_curve_from_records(
    records: Sequence[TradeOutcomeRecord], initial_balance: float = 0.0
) -> List[Dict]:
    """Cumulative-equity curve from closed trades (ts = closed_at, equity =
    initial_balance + running realized PnL). More granular than the rotated
    snapshot tail and always available."""
    ordered = sorted(
        (r for r in records if r.closed_at is not None and r.realized_pnl is not None),
        key=lambda r: r.closed_at,
    )
    equity = float(initial_balance)
    curve = []
    for r in ordered:
        equity += float(r.realized_pnl)
        curve.append({"ts": int(r.closed_at), "equity": round(equity, 4)})
    return curve


def summary(
    records: Sequence[TradeOutcomeRecord],
    snapshots: Optional[Sequence[dict]] = None,
    llm_attribution: Optional[dict] = None,
    window_days: Optional[int] = None,
    initial_balance: float = 0.0,
) -> Dict:
    """Assemble the full metrics payload (the /metrics contract).

    Equity curve: prefer DB snapshots when supplied, otherwise derive from the
    closed-trade outcomes (cumulative realized PnL)."""
    records = list(records)
    curve = equity_curve(snapshots) if snapshots else equity_curve_from_records(records, initial_balance)
    return {
        "window_days": window_days,
        "trades": {
            "total": len(records),
            "win_rate": win_rate(records),
            "profit_factor": profit_factor(records),
            "expectancy": expectancy(records),
            "sharpe": sharpe(records),
            "total_pnl": round(sum(_pnls(records)), 4),
        },
        "equity_curve": curve,
        "per_regime": per_regime_pnl(records),
        "mfe_mae": mfe_mae_distribution(records),
        "llm_attribution": llm_attribution or {},
    }
