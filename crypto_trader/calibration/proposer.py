"""
crypto_trader.calibration.proposer — bounded parameter proposals from trade history.

Proposals are grounded ONLY in fields actually stored on TradeOutcomeRecord
(per-regime expectancy, win-rate, R-multiples, MFE/MAE). Each proposal is
clamped to a [min, max] range and a max per-run step so a single calibration
can never make a large, unreviewed jump. Every proposal carries its rationale,
the train-fold metric it was derived from, and the sample size — so a human can
judge it before approval.

Levers (all in the Phase-2 SAFE/FLAT_ONLY whitelist):
  - risk_per_trade_pct          half-Kelly from win-rate + win/loss R ratio
  - regime_size_multiplier_high scaled by best-regime expectancy_r sign
  - regime_size_multiplier_low  scaled by worst-regime expectancy_r sign
  - st2_sl_atr_mult             widened/tightened from MAE vs realized
  - mr_entry_band               widened when MR win-rate is poor
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Dict, List, Optional, Sequence, Tuple

from ..analytics import metrics
from ..journal import TradeOutcomeRecord

# param -> (min, max, max_step_per_run)
DEFAULT_BOUNDS: Dict[str, Tuple[float, float, float]] = {
    "risk_per_trade_pct": (0.0025, 0.02, 0.0025),
    "regime_size_multiplier_high": (1.0, 1.5, 0.1),
    "regime_size_multiplier_low": (0.25, 1.0, 0.1),
    "st2_sl_atr_mult": (1.5, 4.0, 0.25),
    "mr_entry_band": (0.005, 0.03, 0.0025),
}

# don't propose anything off a tiny sample
MIN_TRADES = 20
MIN_REGIME_TRADES = 10


@dataclass
class Proposal:
    param: str
    current: float
    proposed: float
    rationale: str
    n_trades: int
    train_metric: float

    def to_dict(self) -> dict:
        return asdict(self)


def _clamp_step(current: float, target: float, bounds: Tuple[float, float, float]) -> float:
    """Clamp target to [min,max] and to within max_step of current; round sane."""
    lo, hi, step = bounds
    target = max(lo, min(hi, target))
    if target > current + step:
        target = current + step
    elif target < current - step:
        target = current - step
    target = max(lo, min(hi, target))
    return round(target, 6)


def _propose_risk_per_trade(records, current, bounds) -> Optional[Proposal]:
    if len(records) < MIN_TRADES:
        return None
    wr = metrics.win_rate(records)
    awr = metrics.avg_win_r(records)
    alr = abs(metrics.avg_loss_r(records))
    if awr <= 0 or alr <= 0:
        return None
    # Kelly fraction f* = W - (1-W)/R, R = avg_win/avg_loss. Use HALF-Kelly.
    R = awr / alr
    kelly = wr - (1.0 - wr) / R
    target_frac = max(0.0, kelly) * 0.5
    # interpret kelly fraction as a nudge relative to current sizing
    target = current * (1.0 + (target_frac - 0.5)) if target_frac > 0 else current * 0.5
    proposed = _clamp_step(current, target, bounds)
    if proposed == current:
        return None
    return Proposal(
        param="risk_per_trade_pct", current=current, proposed=proposed,
        rationale=f"half-Kelly={target_frac:.3f} (win_rate={wr:.2f}, R={R:.2f})",
        n_trades=len(records), train_metric=round(metrics.expectancy_r(records), 4),
    )


def _propose_regime_multipliers(records, current_high, current_low, bounds_high, bounds_low):
    per = metrics.per_regime_expectancy_r(records)
    counts = {}
    for r in records:
        reg = r.regime or "unknown"
        counts[reg] = counts.get(reg, 0) + 1

    out: List[Proposal] = []
    eligible = {r: d for r, d in per.items() if counts.get(r, 0) >= MIN_REGIME_TRADES}
    if not eligible:
        return out
    best = max(eligible.items(), key=lambda kv: kv[1]["expectancy_r"])
    worst = min(eligible.items(), key=lambda kv: kv[1]["expectancy_r"])
    # best regime strongly positive → push high multiplier up
    if best[1]["expectancy_r"] > 0.1:
        proposed = _clamp_step(current_high, current_high + bounds_high[2], bounds_high)
        if proposed != current_high:
            out.append(Proposal(
                "regime_size_multiplier_high", current_high, proposed,
                f"best regime '{best[0]}' expectancy_r={best[1]['expectancy_r']:.2f}",
                counts[best[0]], round(best[1]["expectancy_r"], 4),
            ))
    # worst regime negative → push low multiplier down
    if worst[1]["expectancy_r"] < 0:
        proposed = _clamp_step(current_low, current_low - bounds_low[2], bounds_low)
        if proposed != current_low:
            out.append(Proposal(
                "regime_size_multiplier_low", current_low, proposed,
                f"worst regime '{worst[0]}' expectancy_r={worst[1]['expectancy_r']:.2f}",
                counts[worst[0]], round(worst[1]["expectancy_r"], 4),
            ))
    return out


def _propose_sl_atr_mult(records, current, bounds) -> Optional[Proposal]:
    st2 = [r for r in records if (r.entry_reason or "").startswith("supertrend")]
    if len(st2) < MIN_TRADES:
        return None
    maes = [abs(float(r.mae)) for r in st2 if r.mae is not None]
    if not maes:
        return None
    # if losers routinely run deep (high p75 MAE) widen the stop; if shallow, tighten
    p75 = metrics.mfe_mae_distribution(st2)["mae"]["p75"]
    if abs(p75) > 0.02:       # adverse excursion > 2% routinely → widen
        target = current + bounds[2]
    elif abs(p75) < 0.005:    # stops barely tested → tighten
        target = current - bounds[2]
    else:
        return None
    proposed = _clamp_step(current, target, bounds)
    if proposed == current:
        return None
    return Proposal("st2_sl_atr_mult", current, proposed,
                    f"ST2 MAE p75={p75:.4f}", len(st2),
                    round(metrics.expectancy_r(st2), 4))


def _propose_mr_band(records, current, bounds) -> Optional[Proposal]:
    mr = [r for r in records if (r.entry_reason or "") == "mean_reversion"]
    if len(mr) < MIN_TRADES:
        return None
    wr = metrics.win_rate(mr)
    if wr < 0.4:  # poor MR win-rate → demand deeper deviation before entering
        proposed = _clamp_step(current, current + bounds[2], bounds)
        if proposed != current:
            return Proposal("mr_entry_band", current, proposed,
                            f"MR win_rate={wr:.2f} < 0.40 → widen band",
                            len(mr), round(metrics.expectancy_r(mr), 4))
    return None


def propose(
    records: Sequence[TradeOutcomeRecord],
    current_params: Dict[str, float],
    bounds: Optional[Dict[str, Tuple[float, float, float]]] = None,
) -> List[Proposal]:
    """Generate bounded proposals from closed-trade outcomes.

    current_params: the champion values (from config) for each lever.
    Returns [] when the sample is too small or no lever warrants a change."""
    records = list(records)
    b = bounds or DEFAULT_BOUNDS
    proposals: List[Proposal] = []

    p = _propose_risk_per_trade(records, current_params.get("risk_per_trade_pct", 0.005),
                                b["risk_per_trade_pct"])
    if p:
        proposals.append(p)

    proposals.extend(_propose_regime_multipliers(
        records,
        current_params.get("regime_size_multiplier_high", 1.25),
        current_params.get("regime_size_multiplier_low", 0.5),
        b["regime_size_multiplier_high"], b["regime_size_multiplier_low"],
    ))

    p = _propose_sl_atr_mult(records, current_params.get("st2_sl_atr_mult", 2.5),
                             b["st2_sl_atr_mult"])
    if p:
        proposals.append(p)

    p = _propose_mr_band(records, current_params.get("mr_entry_band", 0.015),
                         b["mr_entry_band"])
    if p:
        proposals.append(p)

    return proposals
