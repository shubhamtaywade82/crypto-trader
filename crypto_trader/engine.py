from __future__ import annotations
import argparse, logging, time
from .data_feed import BinanceDataFeed
from .regime import classify_regime
from .playbooks import evaluate_setup
from .wallet import Wallet, PositionSide
from .risk import RiskManager, LLMCircuitBreaker
from .llm_advisor import OllamaAdvisor
from .journal import TradeJournal
from .websocket_feed import RealtimeTicker

logger = logging.getLogger(__name__)

class TradingEngine:
    def __init__(self, symbol: str = "SOLUSDT", leverage: int = 10, use_llm: bool = True):
        self.symbol = symbol
        self.feed = BinanceDataFeed()
        self.wallet = Wallet(symbol=symbol, leverage=leverage)
        self.risk = RiskManager()
        self.journal = TradeJournal()
        self.use_llm = use_llm
        self.cb = LLMCircuitBreaker()
        self.advisor = OllamaAdvisor(timeout=15) if use_llm else None
        self.ticker = RealtimeTicker(symbol=symbol)
        self.ticker.start()

    def run_once(self):
        try:
            self._tick()
        except Exception:
            logger.exception("Unhandled error in run_once — continuing loop")

    def _tick(self):
        logger.info("[%s] === tick start ===", self.symbol)
        df4h = self.feed.get_klines(self.symbol, "4h", 200)
        regime, regime_score = classify_regime(df4h)
        logger.info("[%s] regime=%s score=%.4f", self.symbol, regime, regime_score)

        setup = evaluate_setup(regime, regime_score)
        logger.info("[%s] setup=%s", self.symbol, setup)

        # Log current position state
        pos = self.wallet.position
        if pos:
            mark = self.ticker.last_price or self.feed.get_mark_price(self.symbol)
            pos.update_pnl(mark)
            logger.info(
                "[%s] open position side=%s entry=%.4f qty=%.4f unrealized_pnl=%.4f balance=%.2f",
                self.symbol, pos.side.value, pos.entry_price, pos.remaining_quantity,
                pos.unrealized_pnl, self.wallet.balance,
            )
        else:
            logger.info("[%s] no open position | balance=%.2f", self.symbol, self.wallet.balance)

        if not setup:
            logger.info("[%s] no setup — skip", self.symbol)
            return

        if not self.risk.can_trade():
            logger.warning(
                "[%s] risk gate blocked — daily=%d consec_losses=%d",
                self.symbol, self.risk.daily_count, self.risk.consec_losses,
            )
            return

        llm_weight = 0.0 if regime_score >= 0.85 else 0.2
        llm_conf = 0.5
        if self.use_llm and llm_weight > 0 and self.cb.enabled:
            try:
                advice = self.advisor.advise(f"{self.symbol} {regime}")
                age = time.time() - advice.timestamp
                if age <= 20 and advice.latency_ms <= 3000:
                    llm_conf = advice.confidence
                    self.cb.record_success()
                else:
                    logger.warning("[%s] LLM advice stale (age=%.1fs latency=%.0fms) — using default conf",
                                   self.symbol, age, advice.latency_ms)
            except Exception as exc:
                logger.warning("[%s] LLM advisor error: %s", self.symbol, exc)
                self.cb.record_failure()

        final_score = setup["score"] * (1 - llm_weight) + llm_conf * llm_weight
        logger.info(
            "[%s] final_score=%.4f (setup=%.4f llm_weight=%.2f llm_conf=%.4f)",
            self.symbol, final_score, setup["score"], llm_weight, llm_conf,
        )

        if final_score >= 0.75 and self.wallet.position is None:
            mark = self.ticker.last_price if self.ticker.last_price is not None else self.feed.get_mark_price(self.symbol)
            side = PositionSide.LONG if setup["action"] == "LONG" else PositionSide.SHORT
            self.wallet.open_position(side, mark, margin=50.0)
            self.risk.record_open()
            self.journal.append({
                "symbol": self.symbol, "regime": regime, "regime_score": regime_score,
                "final_score": final_score, "entry_price": mark, "side": side.value,
            })
            logger.info(
                "[%s] *** POSITION OPENED side=%s entry=%.4f final_score=%.4f ***",
                self.symbol, side.value, mark, final_score,
            )
        elif final_score < 0.75:
            logger.info("[%s] final_score %.4f < 0.75 threshold — no entry", self.symbol, final_score)
        elif self.wallet.position is not None:
            logger.info("[%s] position already open — skip entry", self.symbol)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--symbol", default="SOLUSDT")
    p.add_argument("--loop", action="store_true")
    p.add_argument("--tick", type=int, default=300)
    p.add_argument("--no-llm", action="store_true")
    p.add_argument("--leverage", type=int, default=10)
    args = p.parse_args()
    eng = TradingEngine(symbol=args.symbol, leverage=args.leverage, use_llm=not args.no_llm)
    if args.loop:
        while True:
            eng.run_once(); time.sleep(args.tick)
    else:
        eng.run_once()

if __name__ == "__main__":
    main()
