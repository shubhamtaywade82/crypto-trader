"""
crypto_trader.engine — Trading Engine Orchestrator
====================================================
Wires all modules together:
    DataFeed → RegimeAnalyzer → Playbooks → LLM Fusion → Risk → Wallet → Journal
"""

import os
import time
import uuid
import logging
import argparse
from decimal import Decimal
from datetime import datetime, timezone

import pandas as pd

from .data_feed import BinanceDataFeed
from .wallet import EnhancedFuturesWallet, PositionSide
from .risk import RiskManager, LLMCircuitBreaker, AdaptiveThresholdManager
from .playbooks import PlaybookA, PlaybookB
from .regime import MarketRegimeAnalyzer, compute_ema
from .structure import MarketStructureAnalyzer
from .llm_advisor import OllamaAdvisor, build_advisor
from .journal import TradeJournal
from .market_state import OpenInterestTracker
from .config import load_config

logger = logging.getLogger("crypto_trader.engine")

# ── Configuration ──
DEFAULT_SYMBOL = "SOLUSDT"
LEVERAGE = 10
EQUITY_UTILIZATION = 0.50
CATASTROPHIC_SL_PCT = -0.50

TF_4H, TF_1H, TF_15M, TF_5M = "4h", "1h", "15m", "5m"
LIMIT_4H, LIMIT_1H, LIMIT_15M, LIMIT_5M = 200, 150, 150, 150
FUNDING_EXTREME = 0.0005


class TradingEngine:
    """Production trading engine with LLM-enhanced decision making."""

    def __init__(
        self,
        symbol: str = DEFAULT_SYMBOL,
        initial_balance: float = 1_000.0,
        leverage: int = LEVERAGE,
        use_llm: bool = True,
        llm_host: str = None,
        llm_model: str = None,
        log_responses: bool = False,
        log_rest: bool = False,
        log_ws: bool = False,
        log_llm: bool = False,
    ):
        self.symbol = symbol.upper()
        self.use_llm = use_llm

        rest_logged = log_responses or log_rest
        ws_logged = log_responses or log_ws
        llm_logged = log_responses or log_llm

        # Modules
        self.data_feed = BinanceDataFeed(log_responses=rest_logged)
        self.wallet = EnhancedFuturesWallet(
            symbol=self.symbol,
            initial_balance=initial_balance,
            leverage=leverage,
            equity_utilization=EQUITY_UTILIZATION,
            catastrophic_sl_pct=CATASTROPHIC_SL_PCT,
        )
        # Config-driven risk limits (was: bare RiskManager() using constructor
        # defaults, which silently ignored the configured/spec values).
        self.cfg = load_config()
        self.risk_manager = RiskManager(
            max_daily_trades=self.cfg.max_daily_trades,
            max_consecutive_losses=self.cfg.max_consecutive_losses,
            max_drawdown_pct=self.cfg.max_drawdown_pct,
            max_daily_drawdown_pct=self.cfg.max_daily_drawdown_pct,
            cooldown_after_loss_minutes=self.cfg.cooldown_after_loss_minutes,
            max_orders_per_minute=self.cfg.max_orders_per_minute,
            max_margin_ratio=self.cfg.max_margin_ratio,
            max_margin_utilization=self.cfg.max_margin_utilization,
        )
        from .llm_advisor import FINAL_SCORE_THRESHOLD
        self.adaptive_threshold = AdaptiveThresholdManager(
            base_threshold=getattr(self.cfg, "final_score_threshold", FINAL_SCORE_THRESHOLD),
            min_threshold=getattr(self.cfg, "adaptive_min_threshold", 0.50),
            decay_per_hour=getattr(self.cfg, "adaptive_decay_per_hour", 0.01),
            target_trades_per_day=getattr(self.cfg, "adaptive_target_trades_per_day", 1.0),
        )
        self.regime_analyzer = MarketRegimeAnalyzer()
        self.playbook_a = PlaybookA()
        self.playbook_b = PlaybookB()
        self.journal = TradeJournal()
        self.oi_tracker = OpenInterestTracker()

        # LLM
        self.advisor = None
        self._llm_thread = None
        self.advisor = build_advisor(use_llm, llm_host=llm_host, llm_model=llm_model, llm_logged=llm_logged)

    def run_once(self) -> dict:
        """Single trading tick."""
        logger.info(f"--- Tick: {self.symbol} ---")

        # 1. Fetch data
        try:
            df_4h = self.data_feed.get_klines(self.symbol, TF_4H, limit=LIMIT_4H)
            df_1h = self.data_feed.get_klines(self.symbol, TF_1H, limit=LIMIT_1H)
            df_15m = self.data_feed.get_klines(self.symbol, TF_15M, limit=LIMIT_15M)
            df_5m = self.data_feed.get_klines(self.symbol, TF_5M, limit=LIMIT_5M)
            mark_price = self.data_feed.get_mark_price(self.symbol)
            funding_rate = self.data_feed.get_funding_rate(self.symbol)
            oi_data = self.data_feed.get_open_interest(self.symbol)
            taker_ratio = self.data_feed.get_taker_ratio(self.symbol)
        except Exception as e:
            logger.error(f"Data fetch failed: {e}")
            return self.wallet.get_summary()

        if mark_price <= 0:
            logger.warning(f"Invalid mark price {mark_price} for {self.symbol}. Skipping tick.")
            return self.wallet.get_summary()

        # 2. Regime analysis
        regime, regime_score, df_4h = self.regime_analyzer.analyze(df_4h)
        logger.info(f"Regime: {regime.value} (score={regime_score:.2f})")

        # 3. Market structure analysis (SMC signals for playbook gating)
        structure = None
        try:
            msa = MarketStructureAnalyzer(df_1h, swing_window=3)
            structure = msa.analyze()
            recent_bos = structure.get("recent_bos")
            logger.debug(
                f"[STRUCTURE] BOS={recent_bos['type'] if recent_bos else 'None'} | "
                f"OBs={len(structure.get('unmitigated_order_blocks', []))} | "
                f"FVGs={len(structure.get('unmitigated_fvgs', []))}"
            )
        except Exception as e:
            logger.warning(f"[STRUCTURE] MarketStructureAnalyzer failed: {e}")
            structure = None

        oi_value = float(oi_data.get("openInterest", 0.0)) if isinstance(oi_data, dict) else 0.0
        oi_metrics = self.oi_tracker.update(oi_value)
        oi_delta = oi_metrics.oi_delta_15m

        # 4. Start LLM async (non-blocking)
        if self.use_llm and self.advisor:
            open_positions = [p.to_dict() for p in self.wallet.positions.values() if p.status == "OPEN"]
            if self._llm_thread is None or not self._llm_thread.is_alive():
                self._llm_thread = self.advisor.get_advice_async(
                    symbol=self.symbol,
                    df_1h=df_1h,
                    df_4h=df_4h,
                    df_15m=df_15m,
                    df_5m=df_5m,
                    regime=regime.value,
                    regime_score=regime_score,
                    mark_price=mark_price,
                    funding_rate=funding_rate,
                    oi_delta=oi_delta,
                    taker_ratio=taker_ratio,
                    open_positions=open_positions,
                )

        # 5. Risk check — update peak AFTER the gate so a fresh session doesn't
        # advance the high-water mark before we verify daily drawdown limits.
        can_trade, risk_reason = self.risk_manager.can_trade(current_balance=self.wallet.margin_balance)
        if can_trade:
            self.risk_manager.update_peak_balance(self.wallet.margin_balance)
        if not can_trade:
            logger.info(f"[RISK] {risk_reason}")

        # 6. Indicators for trailing stops
        df_1h["ema9"] = compute_ema(df_1h["close"], 9)
        ema9_val = df_1h["ema9"].iloc[-1]
        ema9_1h = float(ema9_val) if pd.notna(ema9_val) else None
        candle_close_time = int(df_1h["close_time"].iloc[-1].timestamp() * 1000)

        # 7. Update positions (using candle time, not wall clock)
        advice = self.advisor.get_last_advice() if (self.use_llm and self.advisor) else None
        current_llm_advice = advice.to_dict() if advice else None
        self.wallet.update_positions(
            self.symbol,
            mark_price,
            candle_close_time,
            ema9_1h,
            current_llm_advice=current_llm_advice,
            current_regime=regime.value if regime else None,
        )

        # 8. Evaluate entry
        if not self.wallet.get_open_position(self.symbol) and can_trade:
            setup = None

            # Try Playbook A
            setup = self.playbook_a.evaluate(df_1h, regime, structure=structure)
            if setup:
                logger.info(f"[PLAYBOOK A] Signal: {setup['side'].value} | score={setup['score']:.2f}")

            # Try Playbook B
            if not setup:
                setup = self.playbook_b.evaluate(df_1h, df_4h, regime, structure=structure)
                if setup:
                    logger.info(f"[PLAYBOOK B] Signal: {setup['side'].value} | score={setup['score']:.2f}")

            # 9. LLM Fusion
            if setup:
                tech_score = setup["score"]
                advice = self.advisor.get_last_advice() if self.advisor else None

                if self.advisor and advice:
                    final_score, llm_weight, explanation = self.advisor.compute_final_score(
                        technical_score=tech_score,
                        regime=regime.value,
                        regime_score=regime_score,
                        advice=advice,
                    )
                    logger.info(f"[FUSION] {explanation} | tech={tech_score:.2f} | final={final_score:.2f}")
                else:
                    final_score = tech_score
                    llm_weight = 0.0
                    explanation = "LLM unavailable"

                # Execute if final score passes threshold
                dynamic_threshold = self.adaptive_threshold.get_threshold()
                if final_score >= dynamic_threshold:
                    if (
                        (setup["side"] == PositionSide.LONG and funding_rate > FUNDING_EXTREME)
                        or (setup["side"] == PositionSide.SHORT and funding_rate < -FUNDING_EXTREME)
                    ):
                        logger.info(
                            f"[BLOCKED] Funding extreme for {setup['side'].value}: {funding_rate:+.4%} "
                            f"(threshold={FUNDING_EXTREME:.2%})"
                        )
                        return self.wallet.get_summary()
                    # Risk-based sizing from stop distance (ATR-ready because stop comes from setup).
                    stop_distance = abs(mark_price - setup["sl_price"])
                    risk_pct = Decimal(str(self.cfg.risk_per_trade_pct)) if self.cfg else Decimal("0.01")
                    risk_budget = self.wallet.margin_balance * risk_pct
                    size_multiplier = min(final_score / dynamic_threshold, 1.0)
                    risk_budget *= Decimal(str(size_multiplier))
                    stop_distance_dec = Decimal(str(stop_distance))
                    quantity = (risk_budget / stop_distance_dec) if stop_distance_dec > 0 else Decimal("0")
                    trade_id = str(uuid.uuid4())[:8]
                    pos = self.wallet.open_position(
                        self.symbol,
                        setup,
                        mark_price,
                        custom_margin=None,
                        custom_quantity=quantity,
                    )

                    if pos:
                        self.risk_manager.record_open()
                        self.adaptive_threshold.record_trade()
                        self.journal.log_open(
                            trade_id=trade_id,
                            symbol=self.symbol,
                            regime=regime.value,
                            regime_score=regime_score,
                            setup=setup,
                            technical_score=tech_score,
                            llm_advice=advice.to_dict() if advice else None,
                            llm_weight=llm_weight,
                            final_score=final_score,
                            margin=pos.margin_used,
                            notional=pos.notional,
                            entry_price=mark_price,
                            funding_rate=funding_rate,
                            oi_delta=oi_delta,
                            taker_ratio=taker_ratio,
                        )
                        logger.info(
                            f"[EXECUTED] Trade {trade_id} | margin={pos.margin_used:.2f} | "
                            f"score={final_score:.2f} (threshold={dynamic_threshold:.2f})"
                        )
                else:
                    logger.info(f"[BLOCKED] Final score {final_score:.2f} < {dynamic_threshold:.2f}")

        # 10. Summary
        summary = self.wallet.get_summary()
        summary["regime"] = regime.value
        summary["regime_score"] = regime_score
        if self.advisor:
            advice = self.advisor.get_last_advice()
            summary["llm"] = advice.to_dict() if advice else {"status": "unavailable"}
        return summary

    def run_loop(self, interval_seconds: int = 300, max_iterations: int = None):
        iteration = 0
        try:
            while True:
                self.run_once()
                self.wallet.print_summary()
                iteration += 1
                if max_iterations and iteration >= max_iterations:
                    break
                time.sleep(interval_seconds)
        except KeyboardInterrupt:
            logger.info("Engine stopped by user")


def main():
    parser = argparse.ArgumentParser(description="Crypto Trader v4")
    parser.add_argument("--symbol", default=DEFAULT_SYMBOL)
    parser.add_argument("--balance", type=float, default=1_000.0)
    parser.add_argument("--leverage", type=int, default=LEVERAGE)
    parser.add_argument("--no-llm", action="store_true")
    parser.add_argument("--llm-host", default=None, help="Ollama host URL (defaults to OLLAMA_HOST env var)")
    parser.add_argument("--llm-model", default=None, help="Ollama model name (defaults to OLLAMA_MODEL env var)")
    parser.add_argument("--log-responses", action="store_true", help="Log all REST API, WS, and LLM JSON outputs (master toggle)")
    parser.add_argument("--log-rest", action="store_true", help="Log Binance REST API request/response JSON")
    parser.add_argument("--log-ws", action="store_true", help="Log Binance WebSocket message JSON")
    parser.add_argument("--log-llm", action="store_true", help="Log Ollama LLM prompt and response JSON")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--tick", type=int, default=300)
    args = parser.parse_args()

    # Configure logging ONLY here
    from .logger_config import configure_colored_logging
    configure_colored_logging(level=logging.INFO)

    engine = TradingEngine(
        symbol=args.symbol,
        initial_balance=args.balance,
        leverage=args.leverage,
        use_llm=not args.no_llm,
        llm_host=args.llm_host,
        llm_model=args.llm_model,
        log_responses=args.log_responses,
        log_rest=args.log_rest,
        log_ws=args.log_ws,
        log_llm=args.log_llm,
    )

    if args.loop:
        logger.info("Starting live loop... Ctrl+C to stop.")
        engine.run_loop(interval_seconds=args.tick)
    else:
        summary = engine.run_once()
        engine.wallet.print_summary()
        print("\nSummary:")
        import json
        print(json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    main()
