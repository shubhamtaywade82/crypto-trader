from __future__ import annotations
import argparse, logging, time
from .data_feed import BinanceDataFeed
from .regime import classify_regime
from .playbooks import evaluate_setup
from .wallet import Wallet, PositionSide
from .risk import RiskManager, LLMCircuitBreaker
from .llm_advisor import OllamaAdvisor
from .journal import TradeJournal

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

    def run_once(self):
        df4h = self.feed.get_klines(self.symbol, "4h", 200)
        regime, regime_score = classify_regime(df4h)
        setup = evaluate_setup(regime, regime_score)
        llm_weight = 0.0 if regime_score >= 0.85 else 0.2
        llm_conf = 0.5
        if self.use_llm and llm_weight > 0 and self.cb.enabled:
            advice = self.advisor.advise(f"{self.symbol} {regime}")
            age = time.time() - advice.timestamp
            if age <= 20 and advice.latency_ms <= 3000:
                llm_conf = advice.confidence
        if setup and self.risk.can_trade():
            final_score = setup["score"] * (1-llm_weight) + llm_conf * llm_weight
            if final_score >= 0.75 and self.wallet.position is None:
                mark = self.feed.get_mark_price(self.symbol)
                side = PositionSide.LONG if setup["action"] == "LONG" else PositionSide.SHORT
                self.wallet.open_position(side, mark, margin=50.0)
                self.risk.record_open()
                self.journal.append({"symbol": self.symbol, "regime": regime, "regime_score": regime_score, "final_score": final_score})


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
