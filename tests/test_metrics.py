"""Phase 1 — metrics aggregator. Values checked against hand computation."""
import math

import pytest

from crypto_trader.analytics import metrics
from crypto_trader.journal import TradeOutcomeRecord


def _rec(pnl, regime="trend", mfe=None, mae=None, pnl_r=None, qty=1.0, entry=100.0):
    return TradeOutcomeRecord(
        trade_id=f"t{pnl}", symbol="SOLUSDT", side="long",
        opened_at=0, closed_at=1000, entry_price=entry, exit_price=entry + pnl,
        quantity=qty, realized_pnl=pnl, holding_time_s=60.0, exit_reason="tp",
        regime=regime, mfe=mfe, mae=mae, pnl_r=pnl_r,
    )


def _rec_at(pnl, t, regime="trend", pnl_r=None):
    """Like _rec but with an explicit closed_at so drawdown ordering is testable."""
    return TradeOutcomeRecord(
        trade_id=f"t{pnl}@{t}", symbol="SOLUSDT", side="long",
        opened_at=0, closed_at=t, entry_price=100.0, exit_price=100.0 + pnl,
        quantity=1.0, realized_pnl=pnl, holding_time_s=60.0, exit_reason="tp",
        regime=regime, pnl_r=pnl_r,
    )


def test_win_rate():
    recs = [_rec(10), _rec(-5), _rec(20), _rec(-2)]  # 2 wins / 4
    assert metrics.win_rate(recs) == 0.5


def test_win_rate_empty():
    assert metrics.win_rate([]) == 0.0


def test_profit_factor():
    recs = [_rec(30), _rec(-10), _rec(-5)]  # gross win 30 / gross loss 15 = 2.0
    assert metrics.profit_factor(recs) == 2.0


def test_profit_factor_no_losses_is_none():
    assert metrics.profit_factor([_rec(10), _rec(5)]) is None


def test_expectancy_currency():
    recs = [_rec(10), _rec(-4), _rec(6)]  # mean = 4.0
    assert metrics.expectancy(recs)["currency"] == 4.0


def test_expectancy_r_multiple():
    recs = [_rec(10, pnl_r=2.0), _rec(-4, pnl_r=-1.0)]  # mean r = 0.5
    assert metrics.expectancy(recs)["r"] == 0.5


def test_sharpe_basic():
    pnls = [10.0, -5.0, 20.0, -2.0]
    recs = [_rec(p) for p in pnls]
    from statistics import mean, pstdev
    expected = (mean(pnls) / pstdev(pnls)) * math.sqrt(365.0)
    assert metrics.sharpe(recs) == pytest.approx(round(expected, 4))


def test_sharpe_too_few_trades():
    assert metrics.sharpe([_rec(10)]) is None


def test_sharpe_zero_variance():
    assert metrics.sharpe([_rec(10), _rec(10)]) is None


def test_per_regime_pnl_groups():
    recs = [_rec(10, regime="trend"), _rec(-5, regime="trend"),
            _rec(8, regime="chop")]
    out = metrics.per_regime_pnl(recs)
    assert out["trend"]["trades"] == 2
    assert out["trend"]["pnl"] == 5.0
    assert out["trend"]["win_rate"] == 0.5
    assert out["chop"]["trades"] == 1
    assert out["chop"]["pnl"] == 8.0


def test_mfe_mae_percentiles():
    recs = [_rec(10, mfe=0.01, mae=-0.005), _rec(-5, mfe=0.02, mae=-0.01)]
    dist = metrics.mfe_mae_distribution(recs)
    assert dist["mfe"]["p50"] == pytest.approx(0.015, abs=1e-6)
    assert dist["mae"]["p50"] == pytest.approx(-0.0075, abs=1e-6)


def test_equity_curve_sorted():
    snaps = [{"ts": 30, "wallet_balance": 1030.0},
             {"ts": 10, "wallet_balance": 1000.0},
             {"ts": 20, "wallet_balance": 980.0}]
    curve = metrics.equity_curve(snaps)
    assert [p["ts"] for p in curve] == [10, 20, 30]
    assert curve[0]["equity"] == 1000.0


def test_equity_curve_empty():
    assert metrics.equity_curve(None) == []


def test_summary_shape():
    recs = [_rec(10), _rec(-5)]
    snaps = [{"ts": 1, "wallet_balance": 1000.0}]
    s = metrics.summary(recs, snapshots=snaps,
                        llm_attribution={"total_trades": 2}, window_days=30)
    assert s["window_days"] == 30
    assert set(s.keys()) == {"window_days", "trades", "equity_curve",
                             "per_regime", "mfe_mae", "llm_attribution"}
    assert s["trades"]["total"] == 2
    assert s["trades"]["total_pnl"] == 5.0
    assert s["equity_curve"][0]["equity"] == 1000.0
    assert s["llm_attribution"]["total_trades"] == 2


# ─────────────────────────────────────────────────────────────────────────────
# compute_metrics — the spec's required metric set (FIX A)
# ─────────────────────────────────────────────────────────────────────────────


def test_compute_metrics_keys():
    m = metrics.compute_metrics([_rec(10, pnl_r=2.0), _rec(-5, pnl_r=-1.0)])
    assert set(m.keys()) == {
        "total_trades", "win_rate", "avg_win_r", "avg_loss_r", "expectancy_r",
        "profit_factor", "max_drawdown", "avg_holding_time_s", "per_regime",
    }


def test_compute_metrics_zero_trades_safe():
    m = metrics.compute_metrics([])
    assert m["total_trades"] == 0
    assert m["win_rate"] == 0.0
    assert m["avg_win_r"] == 0.0
    assert m["avg_loss_r"] == 0.0
    assert m["expectancy_r"] == 0.0
    assert m["profit_factor"] is None        # no losses sentinel
    assert m["max_drawdown"] == 0.0
    assert m["avg_holding_time_s"] == 0.0
    assert m["per_regime"] == {}


def test_compute_metrics_known_r_values():
    # 3 wins @ R = 2,1,3 -> avg_win_r = 2.0 ; 1 loss @ R = -1 -> avg_loss_r = -1.0
    recs = [
        _rec(10, pnl_r=2.0), _rec(5, pnl_r=1.0), _rec(30, pnl_r=3.0),
        _rec(-4, pnl_r=-1.0),
    ]
    m = metrics.compute_metrics(recs)
    assert m["total_trades"] == 4
    assert m["win_rate"] == 0.75
    assert m["avg_win_r"] == 2.0
    assert m["avg_loss_r"] == -1.0
    # expectancy_r = 0.75*2.0 + 0.25*-1.0 = 1.5 - 0.25 = 1.25
    assert m["expectancy_r"] == 1.25


def test_compute_metrics_no_loss_profit_factor_sentinel():
    m = metrics.compute_metrics([_rec(10, pnl_r=2.0), _rec(5, pnl_r=1.0)])
    assert m["profit_factor"] is None        # no losses -> undefined
    assert m["avg_loss_r"] == 0.0            # safe, no crash


def test_compute_metrics_max_drawdown_known_curve():
    # pnl sequence: +10, -4, +6, -20, +5  (ordered by closed_at)
    # cumulative:   10,  6, 12, -8, -3
    # peak so far:  10, 10, 12, 12, 12  -> drawdowns: 0,4,0,20,15 -> max = 20
    recs = [
        _rec_at(10, t=1), _rec_at(-4, t=2), _rec_at(6, t=3),
        _rec_at(-20, t=4), _rec_at(5, t=5),
    ]
    assert metrics.max_drawdown(recs) == 20.0
    # order-independent: shuffled input must give the same answer
    shuffled = [recs[3], recs[0], recs[4], recs[1], recs[2]]
    assert metrics.max_drawdown(shuffled) == 20.0


def test_compute_metrics_per_regime_expectancy_r():
    recs = [
        _rec(10, regime="trend", pnl_r=2.0),
        _rec(-5, regime="trend", pnl_r=-1.0),
        _rec(8, regime="chop", pnl_r=1.5),
    ]
    m = metrics.compute_metrics(recs)
    per = m["per_regime"]
    # trend: win_rate 0.5; expectancy_r = 0.5*2.0 + 0.5*-1.0 = 0.5
    assert per["trend"]["win_rate"] == 0.5
    assert per["trend"]["expectancy_r"] == 0.5
    # chop: single win, win_rate 1.0; expectancy_r = 1.0*1.5 = 1.5
    assert per["chop"]["win_rate"] == 1.0
    assert per["chop"]["expectancy_r"] == 1.5
    # per-regime exposes ONLY win_rate + expectancy_r
    assert set(per["trend"].keys()) == {"win_rate", "expectancy_r"}


def test_compute_metrics_excludes_records_missing_pnl_r():
    # Two wins, only ONE carries pnl_r -> R means use only that one.
    recs = [_rec(10, pnl_r=2.0), _rec(20)]  # second lacks pnl_r
    m = metrics.compute_metrics(recs)
    assert m["avg_win_r"] == 2.0             # excluded the pnl_r-less win
    assert m["win_rate"] == 1.0              # win_rate still counts both
    # no crash; expectancy_r uses avg_win_r=2.0, avg_loss_r=0.0
    assert m["expectancy_r"] == 2.0


def test_avg_holding_time_s():
    recs = [_rec(10), _rec(-5)]  # _rec sets holding_time_s=60.0
    assert metrics.avg_holding_time_s(recs) == 60.0
