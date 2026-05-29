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
    """Per-regime breakdown in CURRENCY (trades/pnl/win_rate/expectancy-in-currency).
    Consumer: summary() (the /metrics dashboard payload). NOT interchangeable with
    per_regime_expectancy_r below, which reports R-multiples for compute_metrics."""
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
        if r.mfe is not None and abs(r.mfe) > 1e-10 and r.realized_pnl is not None and r.entry_price:
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


def _r_multiples(records: Sequence[TradeOutcomeRecord]) -> List[float]:
    """PnL-in-R values for trades that carry a pnl_r (others are excluded)."""
    return [float(r.pnl_r) for r in records if r.pnl_r is not None]


def avg_win_r(records: Sequence[TradeOutcomeRecord]) -> float:
    """Mean pnl_r over winning trades (realized_pnl > 0). 0.0 when none."""
    wins = [
        float(r.pnl_r)
        for r in records
        if r.pnl_r is not None and r.realized_pnl is not None and r.realized_pnl > 0
    ]
    return round(mean(wins), 4) if wins else 0.0


def avg_loss_r(records: Sequence[TradeOutcomeRecord]) -> float:
    """Mean pnl_r over losing trades (realized_pnl < 0); negative. 0.0 when none."""
    losses = [
        float(r.pnl_r)
        for r in records
        if r.pnl_r is not None and r.realized_pnl is not None and r.realized_pnl < 0
    ]
    return round(mean(losses), 4) if losses else 0.0


def expectancy_r(records: Sequence[TradeOutcomeRecord]) -> float:
    """Expectancy expressed in R: win_rate*avg_win_r + (1-win_rate)*avg_loss_r."""
    wr = win_rate(records)
    return round(wr * avg_win_r(records) + (1.0 - wr) * avg_loss_r(records), 4)


def max_drawdown(records: Sequence[TradeOutcomeRecord]) -> float:
    """Max peak-to-trough drop of the CUMULATIVE realized-PnL curve. >= 0.

    Trades are ordered by ``closed_at`` (falling back to input order when that's
    missing) to reconstruct the equity path."""
    ordered = sorted(
        (r for r in records if r.realized_pnl is not None),
        key=lambda r: (r.closed_at is None, r.closed_at or 0),
    )
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for r in ordered:
        cum += float(r.realized_pnl)
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
    return round(max_dd, 4)


def avg_holding_time_s(records: Sequence[TradeOutcomeRecord]) -> float:
    """Mean holding_time_s across trades. 0.0 when none."""
    times = [float(r.holding_time_s) for r in records if r.holding_time_s is not None]
    return round(mean(times), 4) if times else 0.0


def per_regime_expectancy_r(records: Sequence[TradeOutcomeRecord]) -> Dict[str, Dict]:
    """Per-regime breakdown keyed by regime -> {win_rate, expectancy_r} in R (NOT
    currency). Consumer: compute_metrics() (the required spec metric set). NOT
    interchangeable with per_regime_pnl above, which reports currency expectancy
    for summary()."""
    groups: Dict[str, List[TradeOutcomeRecord]] = {}
    for r in records:
        groups.setdefault(r.regime or "unknown", []).append(r)
    return {
        regime: {
            "win_rate": win_rate(rs),
            "expectancy_r": expectancy_r(rs),
        }
        for regime, rs in groups.items()
    }


def compute_metrics(records: Sequence[TradeOutcomeRecord]) -> Dict:
    """Required spec metric set over an iterable of TradeOutcomeRecord.

    Returns a dict with EXACTLY these keys (zero-trade -> safe zeros/None):
      total_trades, win_rate, avg_win_r, avg_loss_r, expectancy_r,
      profit_factor, max_drawdown, avg_holding_time_s, per_regime.

    profit_factor sentinel: ``None`` when there are no losing trades
    (gross loss == 0) — the ratio is undefined; callers should treat it as
    "no losses recorded". R-based means exclude trades lacking pnl_r and never
    crash on them.
    """
    records = list(records)
    return {
        "total_trades": len(records),
        "win_rate": win_rate(records),
        "avg_win_r": avg_win_r(records),
        "avg_loss_r": avg_loss_r(records),
        "expectancy_r": expectancy_r(records),
        "profit_factor": profit_factor(records),
        "max_drawdown": max_drawdown(records),
        "avg_holding_time_s": avg_holding_time_s(records),
        "per_regime": per_regime_expectancy_r(records),
    }


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
