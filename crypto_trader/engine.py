"""
crypto_trader.engine — Trading Engine Orchestrator
====================================================
Wires all modules together:
    DataFeed → RegimeAnalyzer → Playbooks → LLM Fusion → Risk → Wallet → Journal
"""

import time
import uuid
import logging
import argparse
from datetime import datetime, timezone

import pandas as pd

from .data_feed import BinanceDataFeed
from .wallet import EnhancedFuturesWallet, PositionSide
from .risk import RiskManager, LLMCircuitBreaker
from .playbooks import PlaybookA, PlaybookB
from .regime import MarketRegimeAnalyzer, compute_ema
from .llm_advisor import OllamaAdvisor
from .journal import TradeJournal

logger = logging.getLogger("crypto_trader.engine")

# ── Configuration ──
DEFAULT_SYMBOL = "SOLUSDT"
LEVERAGE = 10
EQUITY_UTILIZATION = 0.50
CATASTROPHIC_SL_PCT = -0.50

TF_4H, TF_1H = "4h", "1h"
LIMIT_4H, LIMIT_1H = 200, 150


class TradingEngine:
    """Production trading engine with LLM-enhanced decision making."""

    def __init__(
        self,
        symbol: str = DEFAULT_SYMBOL,
        initial_balance: float = 1_000.0,
        leverage: int = LEVERAGE,
        testnet: bool = False,
        use_llm: bool = True,
        llm_host: str = "http://localhost:11434",
        llm_model: str = "qwen3.5:4b",
        log_responses: bool = False,
    ):
        self.symbol = symbol.upper()
        self.use_llm = use_llm

        # Modules
        base_url = "https://demo-fapi.binance.com" if testnet else "https://fapi.binance.com"
        self.data_feed = BinanceDataFeed(base_url=base_url, log_responses=log_responses)
        self.wallet = EnhancedFuturesWallet(
            symbol=self.symbol,
            initial_balance=initial_balance,
            leverage=leverage,
            equity_utilization=EQUITY_UTILIZATION,
            catastrophic_sl_pct=CATASTROPHIC_SL_PCT,
        )
        self.risk_manager = RiskManager()
        self.regime_analyzer = MarketRegimeAnalyzer()
        self.playbook_a = PlaybookA()
        self.playbook_b = PlaybookB()
        self.journal = TradeJournal()

        # LLM
        self.advisor = None
        self._llm_thread = None
        if use_llm:
            self.advisor = OllamaAdvisor(host=llm_host, model=llm_model)
            if self.advisor.is_ready():
                logger.info(f"[LLM] Connected to {llm_model}")
            else:
                logger.warning("[LLM] Ollama unavailable. Technical-only mode.")

    def run_once(self) -> dict:
        """Single trading tick."""
        logger.info(f"--- Tick: {self.symbol} ---")

        # 1. Fetch data
        try:
            df_4h = self.data_feed.get_klines(self.symbol, TF_4H, limit=LIMIT_4H)
            df_1h = self.data_feed.get_klines(self.symbol, TF_1H, limit=LIMIT_1H)
            mark_price = self.data_feed.get_mark_price(self.symbol)
            funding_rate = self.data_feed.get_funding_rate(self.symbol)
            oi_data = self.data_feed.get_open_interest(self.symbol)
            taker_ratio = self.data_feed.get_taker_ratio(self.symbol)
        except Exception as e:
            logger.error(f"Data fetch failed: {e}")
            return self.wallet.get_summary()

        # 2. Regime analysis
        regime, regime_score, df_4h = self.regime_analyzer.analyze(df_4h)
        logger.info(f"Regime: {regime.value} (score={regime_score:.2f})")

        # OI delta: would require storing historical OI to compute a proper delta.
        # Using 0.0 for now — the raw oi value is still passed to the LLM prompt.
        oi_delta = 0.0

        # 4. Start LLM async (non-blocking)
        if self.use_llm and self.advisor:
            open_positions = [p.to_dict() for p in self.wallet.positions.values() if p.status == "OPEN"]
            if self._llm_thread is None or not self._llm_thread.is_alive():
                self._llm_thread = self.advisor.get_advice_async(
                    symbol=self.symbol,
                    df_1h=df_1h,
                    df_4h=df_4h,
                    regime=regime.value,
                    regime_score=regime_score,
                    mark_price=mark_price,
                    funding_rate=funding_rate,
                    oi_delta=oi_delta,
                    taker_ratio=taker_ratio,
                    open_positions=open_positions,
                )

        # 5. Risk check
        can_trade, risk_reason = self.risk_manager.can_trade()
        if not can_trade:
            logger.info(f"[RISK] {risk_reason}")

        # 6. Indicators for trailing stops
        df_1h["ema9"] = compute_ema(df_1h["close"], 9)
        ema9_val = df_1h["ema9"].iloc[-1]
        ema9_1h = float(ema9_val) if pd.notna(ema9_val) else None
        candle_close_time = int(df_1h["close_time"].iloc[-1].timestamp() * 1000)

        # 7. Update positions (using candle time, not wall clock)
        self.wallet.update_positions(mark_price, candle_close_time, ema9_1h)

        # 8. Evaluate entry
        if not self.wallet.get_open_position() and can_trade:
            setup = None

            # Try Playbook A
            setup = self.playbook_a.evaluate(df_1h, regime)
            if setup:
                logger.info(f"[PLAYBOOK A] Signal: {setup['side'].value} | score={setup['score']:.2f}")

            # Try Playbook B
            if not setup:
                setup = self.playbook_b.evaluate(df_1h, df_4h, regime)
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
                from .llm_advisor import FINAL_SCORE_THRESHOLD
                if final_score >= FINAL_SCORE_THRESHOLD:
                    # Adjust margin by final score (higher score = full size, lower = reduced)
                    base_margin = self.wallet.available_balance * self.wallet.equity_utilization
                    adjusted_margin = base_margin * min(final_score / FINAL_SCORE_THRESHOLD, 1.0)

                    trade_id = str(uuid.uuid4())[:8]
                    pos = self.wallet.open_position(setup, mark_price, custom_margin=adjusted_margin)

                    if pos:
                        self.risk_manager.record_open()
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
                            margin=adjusted_margin,
                            notional=pos.notional,
                            entry_price=mark_price,
                            funding_rate=funding_rate,
                            oi_delta=oi_delta,
                            taker_ratio=taker_ratio,
                        )
                        logger.info(f"[EXECUTED] Trade {trade_id} | margin={adjusted_margin:.2f} | score={final_score:.2f}")
                else:
                    logger.info(f"[BLOCKED] Final score {final_score:.2f} < {FINAL_SCORE_THRESHOLD}")

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
    parser.add_argument("--testnet", action="store_true")
    parser.add_argument("--no-llm", action="store_true")
    parser.add_argument("--llm-host", default="http://localhost:11434")
    parser.add_argument("--llm-model", default="qwen3.5:4b")
    parser.add_argument("--log-responses", action="store_true", help="Log Binance API request/response JSON")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--tick", type=int, default=300)
    args = parser.parse_args()

    # Configure logging ONLY here
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    engine = TradingEngine(
        symbol=args.symbol,
        initial_balance=args.balance,
        leverage=args.leverage,
        testnet=args.testnet,
        use_llm=not args.no_llm,
        llm_host=args.llm_host,
        llm_model=args.llm_model,
        log_responses=args.log_responses,
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
