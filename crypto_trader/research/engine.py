"""
crypto_trader.research.engine — Capital-aware strategy research engine
======================================================================
Fetches live Binance klines, runs all strategies through existing backtest
engines, applies a capital simulation layer (₹10k / 10x), and ranks results.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import pandas as pd

from ..data_feed import BinanceDataFeed
from ..backtest.engine import run_backtest as _run_mr
from ..backtest.st2_engine import run_backtest_st2
from ..backtest.smc_engine import run_backtest_smc
from ..backtest.smc5c_engine import run_backtest_smc5c
from ..backtest.lsw_engine import run_backtest_lsw
from ..backtest.mta_engine import run_backtest_mta
from ..backtest.regime_switch_engine import run_backtest_regime_switch
from ..analytics.metrics import compute_metrics
from ..journal import TradeOutcomeRecord

logger = logging.getLogger("crypto_trader.research")

INR_PER_USDT = 84.5


# ── Strategy runner definitions ──────────────────────────────────────────

StrategyConfig = Tuple[str, bool, Optional[str], dict]
""" (timeframe, needs_htf, htf_timeframe_or_none, params) """


def _st2_runner(df, htf_df, **kw):
    return run_backtest_st2(df, htf_df, **kw)


def _smc_runner(df, htf_df, **kw):
    return run_backtest_smc(df, htf_df, **kw)


def _smc5c_runner(df, htf_frames, **kw):
    return run_backtest_smc5c(df, htf_frames, **kw)


def _lsw_runner(df, htf_df, **kw):
    return run_backtest_lsw(df, htf_df, **kw)


def _mta_runner(df, htf_frames, **kw):
    return run_backtest_mta(df, htf_frames, **kw)


def _rs_runner(df, htf_df, **kw):
    allowed = kw.pop("allowed_regimes", ["TREND_EXPANSION", "BREAKOUT_ENVIRONMENT"])
    return run_backtest_regime_switch(df, htf_df, allowed_regimes=allowed, **kw)


STRATEGIES = {
    "mean_reversion": {
        "fn": _run_mr, "tf": "15m", "htf": None,
        "params": {"sma_period": 20, "entry_band": 0.015, "stop_loss_pct": 0.008},
    },
    "supertrend2": {
        "fn": _st2_runner, "tf": "1h", "htf": "4h",
        "params": {"atr_period": 10, "factor": 3.0, "entry_mode": "flip",
                    "tp1_pct": 1.0, "use_tp": True, "sl_mode": "supertrend"},
    },
    "smc_pipeline": {
        "fn": _smc_runner, "tf": "15m", "htf": "4h",
        "params": {"swing_window": 3, "min_score": 70.0, "sweep_lookback": 12,
                    "structure_lookback": 12, "tp_rr": 2.0, "entry_mode": "signal"},
    },
    "smc_5c": {
        "fn": _smc5c_runner, "tf": "5m", "htf": "15m,1h,4h",
        "params": {"ema_fast": 50, "ema_slow": 200, "bos_lookback": 5,
                    "fvg_window": 10, "atr_period": 14, "vol_window": 200},
    },
    "liquidity_sweep": {
        "fn": _lsw_runner, "tf": "15m", "htf": "4h",
        "params": {"swing_window": 3, "sweep_lookback": 3, "tp_r": 2.5,
                    "sl_buffer_atr": 0.25, "min_stop_frac": 0.002},
    },
    "mtf_alignment": {
        "fn": _mta_runner, "tf": "5m", "htf": "1h,4h,1d",
        "params": {"ema_len": 50, "min_macro_frames": 2, "break_lookback": 5,
                    "swing_lookback": 6, "tp_r": 3.0},
    },
    "regime_switch": {
        "fn": _rs_runner, "tf": "1h", "htf": "4h",
        "params": {"atr_period": 10, "factor": 3.0, "entry_mode": "flip",
                    "tp1_pct": 1.0, "use_tp": True, "sl_mode": "supertrend"},
    },
}


# ── Data classes ─────────────────────────────────────────────────────────


@dataclass
class RankedStrategy:
    symbol: str
    strategy: str
    trades: int = 0
    win_rate: float = 0.0
    profit_factor: Optional[float] = None
    expectancy_r: float = 0.0
    avg_win_r: float = 0.0
    avg_loss_r: float = 0.0
    total_return_pct: float = 0.0
    final_capital_inr: float = 0.0
    avg_holding_hours: float = 0.0
    max_drawdown_pct: float = 0.0
    return_over_drawdown: float = 0.0

    @property
    def rank_score(self) -> float:
        pf = self.profit_factor or 0.0
        exp = max(self.expectancy_r, 0.0)
        wr = self.win_rate / 100.0
        return round(pf * 0.4 + exp * 10 * 0.3 + wr * 0.3, 4)


# ── Capital simulator ────────────────────────────────────────────────────


class CapitalSimulator:
    """Scale raw 1-unit backtest trade outcomes to a capital/leverage scenario."""

    def __init__(
        self,
        initial_capital_inr: float = 10_000.0,
        leverage: int = 10,
        risk_per_trade_pct: float = 0.01,
        inr_per_usdt: float = INR_PER_USDT,
        taker_fee: float = 0.0005,
    ):
        self.initial_capital_inr = initial_capital_inr
        self.leverage = leverage
        self.risk_per_trade_pct = risk_per_trade_pct
        self.inr_per_usdt = inr_per_usdt
        self.taker_fee = taker_fee

    def simulate(self, trades: List[TradeOutcomeRecord], symbol: str) -> RankedStrategy:
        balance = self.initial_capital_inr
        peak = balance
        max_dd = 0.0

        for t in trades:
            if t.entry_price is None or t.entry_price <= 0 or t.realized_pnl is None:
                continue

            usdt_buying_power = (balance / self.inr_per_usdt) * self.leverage
            notional_1u = t.entry_price * max(abs(t.quantity or 0.0), 1.0)
            scale = usdt_buying_power / notional_1u
            scaled_pnl = float(t.realized_pnl) * scale
            round_trip_fee = usdt_buying_power * self.taker_fee * 2
            net_pnl = scaled_pnl - round_trip_fee
            balance += net_pnl * self.inr_per_usdt
            peak = max(peak, balance)
            dd = (peak - balance) / peak if peak > 0 else 0.0
            max_dd = max(max_dd, dd)

        total_return_pct = ((balance / self.initial_capital_inr) - 1.0) * 100
        valid_trades = [t for t in trades if t.realized_pnl is not None]
        total_holding = sum(t.holding_time_s for t in valid_trades if t.holding_time_s)
        avg_holding_hours = total_holding / 3600 / max(len(valid_trades), 1)
        rord = round(total_return_pct / (max_dd * 100 + 0.001), 2)

        m = compute_metrics(trades)
        return RankedStrategy(
            symbol=symbol,
            strategy="",
            trades=m["total_trades"],
            win_rate=round(m["win_rate"] * 100, 2),
            profit_factor=m["profit_factor"],
            expectancy_r=m["expectancy_r"],
            avg_win_r=m["avg_win_r"],
            avg_loss_r=m["avg_loss_r"],
            total_return_pct=round(total_return_pct, 2),
            final_capital_inr=round(balance, 2),
            avg_holding_hours=round(avg_holding_hours, 2),
            max_drawdown_pct=round(max_dd * 100, 2),
            return_over_drawdown=rord,
        )


# ── Research engine ──────────────────────────────────────────────────────


class ResearchEngine:
    """Fetch data, run all strategies, rank by capital-aware metrics.

    Usage:
        engine = ResearchEngine(symbols=["SOLUSDT", "ETHUSDT"])
        results = engine.run()
        engine.print_ranking(results)
    """

    def __init__(
        self,
        symbols: Optional[List[str]] = None,
        bars: int = 3000,
        initial_capital_inr: float = 10_000.0,
        leverage: int = 10,
        risk_per_trade_pct: float = 0.01,
        taker_fee: float = 0.0005,
        inr_per_usdt: float = INR_PER_USDT,
        feed: Optional[BinanceDataFeed] = None,
    ):
        self.symbols = symbols or ["SOLUSDT", "ETHUSDT"]
        self.bars = bars
        self.feed = feed or BinanceDataFeed()
        self.simulator = CapitalSimulator(
            initial_capital_inr=initial_capital_inr,
            leverage=leverage,
            risk_per_trade_pct=risk_per_trade_pct,
            taker_fee=taker_fee,
            inr_per_usdt=inr_per_usdt,
        )

    def run(self) -> List[RankedStrategy]:
        results: List[RankedStrategy] = []
        for symbol in self.symbols:
            logger.info("Researching %s ...", symbol)
            for sname, sdef in STRATEGIES.items():
                try:
                    bt_result = self._run_single(symbol, sname, sdef)
                    if bt_result is None or len(bt_result.trades or []) < 3:
                        continue
                    ranked = self.simulator.simulate(bt_result.trades, symbol)
                    ranked.strategy = sname
                    if ranked.trades >= 5:
                        results.append(ranked)
                        logger.info(
                            "  %s/%-18s %3d trds PF=%-5s E[r]=%-7s ret=%-+7s%%",
                            symbol, sname, ranked.trades,
                            f"{ranked.profit_factor:.2f}" if ranked.profit_factor else "∞",
                            f"{ranked.expectancy_r:.3f}",
                            f"{ranked.total_return_pct:.1f}",
                        )
                except Exception as e:
                    logger.debug("%s/%s failed: %s", symbol, sname, e)
        results.sort(key=lambda r: r.rank_score, reverse=True)
        return results

    def _fetch_data(self, symbol: str, timeframe: str, limit: int) -> Optional[pd.DataFrame]:
        try:
            df = self.feed.get_klines(symbol, timeframe, limit=limit)
            if df is not None and len(df) > 50:
                return df
        except Exception:
            pass
        try:
            df = self.feed.get_klines(symbol, timeframe, limit=limit)
            if df is not None and len(df) > 50:
                return df
        except Exception as e:
            logger.debug("No data %s %s: %s", symbol, timeframe, e)
        return None

    def _run_single(self, symbol: str, sname: str, sdef: dict):
        tf = sdef["tf"]
        df = self._fetch_data(symbol, tf, self.bars)
        if df is None:
            return None

        htf_val = sdef.get("htf")
        multi_htf = htf_val and "," in htf_val
        htf_dfs = None

        if htf_val and not multi_htf:
            htf_dfs = self._fetch_data(symbol, htf_val, self.bars)
            if htf_dfs is None:
                return None
        elif multi_htf:
            tfs = [t.strip() for t in htf_val.split(",")]
            htf_dfs = []
            for mtf in tfs:
                h = self._fetch_data(symbol, mtf, self.bars)
                if h is not None:
                    htf_dfs.append(h)
            if not htf_dfs:
                return None

        params = dict(sdef["params"])
        kwargs = dict(symbol=symbol, timeframe=tf,
                      taker_fee=self.simulator.taker_fee,
                      leverage=self.simulator.leverage)

        if sname == "mean_reversion":
            return _run_mr(df, **params, **kwargs)
        elif sname == "smc_5c":
            return run_backtest_smc5c(df, htf_dfs or [], **params, **kwargs)
        elif sname == "mtf_alignment":
            return run_backtest_mta(df, htf_dfs or [], **params, **kwargs)
        elif sname == "regime_switch":
            allowed = params.pop("allowed_regimes", ["TREND_EXPANSION", "BREAKOUT_ENVIRONMENT"])
            return run_backtest_regime_switch(df, htf_dfs, allowed_regimes=allowed,
                                              htf_timeframe=htf_val, **params, **kwargs)
        elif htf_dfs is not None:
            fn = sdef["fn"]
            return fn(df, htf_dfs, htf_timeframe=htf_val, **params, **kwargs)
        else:
            return None

    @staticmethod
    def print_ranking(results: List[RankedStrategy], top_n: int = 25):
        try:
            from tabulate import tabulate
        except ImportError:
            _simple_print(results, top_n)
            return
        headers = [
            "Rank", "Symbol", "Strategy", "Trades", "Win%", "PF",
            "E[r]", "Return%", "Final(INR)", "DD%", "RoDD", "AvgHr",
        ]
        rows = []
        for i, r in enumerate(results[:top_n], 1):
            rows.append([
                i, r.symbol, r.strategy[:14], r.trades, r.win_rate,
                f"{r.profit_factor:.2f}" if r.profit_factor else "∞",
                f"{r.expectancy_r:.3f}", f"{r.total_return_pct:+.1f}",
                f"₹{r.final_capital_inr:,.0f}",
                f"{r.max_drawdown_pct:.1f}", f"{r.return_over_drawdown:.2f}",
                f"{r.avg_holding_hours:.1f}",
            ])
        print(tabulate(rows, headers=headers, tablefmt="psql"))

    @staticmethod
    def print_summary(results: List[RankedStrategy]):
        try:
            from tabulate import tabulate
        except ImportError:
            return
        by_strategy: Dict[str, List[RankedStrategy]] = {}
        for r in results:
            by_strategy.setdefault(r.strategy, []).append(r)
        headers = ["Strategy", "Symbols", "Avg PF", "Avg E[r]", "Avg Win%",
                    "Avg Ret%", "Avg DD%"]
        rows = []
        for sname, rs in sorted(by_strategy.items()):
            pfs = [r.profit_factor for r in rs if r.profit_factor is not None]
            avg_pf = sum(pfs) / len(pfs) if pfs else None
            avg_exp = _mean(r.expectancy_r for r in rs)
            avg_wr = _mean(r.win_rate for r in rs)
            avg_ret = _mean(r.total_return_pct for r in rs)
            avg_dd = _mean(r.max_drawdown_pct for r in rs)
            rows.append([
                sname[:16], len(rs),
                f"{avg_pf:.2f}" if avg_pf else "∞",
                f"{avg_exp:.3f}", f"{avg_wr:.1f}%",
                f"{avg_ret:+.1f}%", f"{avg_dd:.1f}%",
            ])
        print("\n── Strategy Summary ──")
        print(tabulate(rows, headers=headers, tablefmt="psql"))


def _mean(values) -> float:
    vals = list(values)
    return sum(vals) / len(vals) if vals else 0.0


def _simple_print(results, top_n=25):
    print(f"\n{'Rank':<5} {'Symbol':<10} {'Strategy':<18} {'Trades':>6} "
          f"{'Win%':>6} {'PF':>6} {'E[r]':>7} {'Return%':>8} {'DD%':>6}")
    print("-" * 75)
    for i, r in enumerate(results[:top_n], 1):
        pf = f"{r.profit_factor:.2f}" if r.profit_factor else "∞"
        print(f"{i:<5} {r.symbol:<10} {r.strategy[:17]:<18} "
              f"{r.trades:>6} {r.win_rate:>5.1f}% {pf:>6} "
              f"{r.expectancy_r:>7.3f} {r.total_return_pct:>+7.1f}% "
              f"{r.max_drawdown_pct:>5.1f}%")
