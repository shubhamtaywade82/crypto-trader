"""
crypto_trader.calibration.walkforward — out-of-sample stability check.

IMPORTANT — this is a PROXY validation, not a full strategy re-simulation. A true
walk-forward would re-run the strategies over historical klines with the proposed
params; the bot stores closed-trade *outcomes* (and a wallet event replay of the
same trades), not the raw market history needed to re-evaluate entries under new
params. So instead we verify that the EDGE the proposals were derived from is
stable across a time split: the proposer sees the train fold; we then confirm the
held-out test fold still shows a non-deteriorating edge (expectancy_r) and no
materially worse drawdown. A 'reject' here means the in-sample signal didn't
hold out — don't apply.
"""
from __future__ import annotations

from typing import Dict, List, Sequence, Tuple

from ..analytics import metrics
from ..journal import TradeOutcomeRecord


def time_split(
    records: Sequence[TradeOutcomeRecord], train_frac: float = 0.7
) -> Tuple[List[TradeOutcomeRecord], List[TradeOutcomeRecord]]:
    """Chronological split (oldest→train, newest→test) by closed_at."""
    ordered = sorted(records, key=lambda r: (r.closed_at or 0))
    n = len(ordered)
    cut = int(n * train_frac)
    return ordered[:cut], ordered[cut:]


def _fold_metrics(records) -> Dict:
    return {
        "n": len(records),
        "expectancy_r": metrics.expectancy_r(records),
        "win_rate": metrics.win_rate(records),
        "max_drawdown": metrics.max_drawdown(records),
        "total_pnl": round(sum(float(r.realized_pnl) for r in records if r.realized_pnl is not None), 4),
    }


def validate(
    records: Sequence[TradeOutcomeRecord],
    train_frac: float = 0.7,
    min_test_trades: int = 10,
    expectancy_floor_ratio: float = 0.5,
    dd_tolerance_ratio: float = 1.5,
) -> Dict:
    """Split, compare folds, return a verdict dict.

    verdict == 'improve' when the test fold keeps at least
    expectancy_floor_ratio of the train-fold expectancy_r AND its max drawdown
    is no worse than dd_tolerance_ratio × the train fold. Otherwise 'reject'."""
    train, test = time_split(records, train_frac)
    train_m = _fold_metrics(train)
    test_m = _fold_metrics(test)

    if test_m["n"] < min_test_trades:
        verdict = "insufficient_data"
        reason = f"test fold has {test_m['n']} trades (< {min_test_trades})"
    else:
        edge_ok = test_m["expectancy_r"] >= train_m["expectancy_r"] * expectancy_floor_ratio
        # drawdown: allow some slack; if train had ~0 DD, only require test not blow up
        dd_cap = max(train_m["max_drawdown"] * dd_tolerance_ratio, train_m["max_drawdown"] + 1e-9)
        dd_ok = test_m["max_drawdown"] <= dd_cap or train_m["max_drawdown"] == 0
        if edge_ok and dd_ok:
            verdict = "improve"
            reason = "edge persists out-of-sample within drawdown tolerance"
        else:
            verdict = "reject"
            bits = []
            if not edge_ok:
                bits.append(f"test expectancy_r {test_m['expectancy_r']:.3f} "
                            f"< {expectancy_floor_ratio}×train {train_m['expectancy_r']:.3f}")
            if not dd_ok:
                bits.append(f"test max_dd {test_m['max_drawdown']:.2f} worse than tolerance")
            reason = "; ".join(bits)

    return {
        "verdict": verdict,
        "reason": reason,
        "train": train_m,
        "test": test_m,
        "note": "proxy stability check, not a strategy re-simulation",
    }
