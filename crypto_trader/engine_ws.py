from __future__ import annotations

import argparse
import logging
import time

from .data_feed import BinanceDataFeed
from .engine import TradingEngine
from .websocket import BinanceFuturesWebSocket

logger = logging.getLogger(__name__)


class TradingEngineWS(TradingEngine):
    """Hybrid engine: REST for signal generation + WS for execution/monitoring."""

    def __init__(self, symbol: str = "SOLUSDT", leverage: int = 10, use_llm: bool = True, testnet: bool = False):
        super().__init__(symbol=symbol, leverage=leverage, use_llm=use_llm)
        if testnet:
            self.feed = BinanceDataFeed(base_url="https://demo-fapi.binance.com")
        self.ws = BinanceFuturesWebSocket(symbol=symbol)
        self.ws.start()

    def _entry_price(self) -> float:
        mid = self.ws.mid_price()
        if mid is not None:
            return mid
        mark = self.ws.snapshot().mark_price
        if mark is not None:
            return mark
        return self.feed.get_mark_price(self.symbol)

    def run_once(self):
        # Base logic from REST engine
        df4h = self.feed.get_klines(self.symbol, "4h", 200)
        regime, regime_score = self._classify(df4h)
        setup = self._setup(regime, regime_score)
        if setup and self.risk.can_trade() and self.wallet.position is None:
            final_score = self._final_score(setup, regime, regime_score)
            if final_score >= 0.75:
                mark = self._entry_price()
                side = self._side(setup)
                self.wallet.open_position(side, mark, margin=50.0)
                self.risk.record_open()
                snap = self.ws.snapshot()
                self.journal.append({
                    "symbol": self.symbol,
                    "regime": regime,
                    "regime_score": regime_score,
                    "final_score": final_score,
                    "entry_price": mark,
                    "bid": snap.bid,
                    "ask": snap.ask,
                    "mark_price": snap.mark_price,
                    "ltp": snap.last_trade_price,
                    "funding_rate": snap.funding_rate,
                })

    # helpers reuse v1 logic cleanly
    def _classify(self, df4h):
        from .regime import classify_regime
        return classify_regime(df4h)

    def _setup(self, regime, regime_score):
        from .playbooks import evaluate_setup
        return evaluate_setup(regime, regime_score)

    def _final_score(self, setup, regime, regime_score):
        llm_weight = 0.0 if regime_score >= 0.85 else 0.2
        llm_conf = 0.5
        if self.use_llm and llm_weight > 0 and self.cb.enabled:
            advice = self.advisor.advise(f"{self.symbol} {regime}")
            if (time.time() - advice.timestamp) <= 20 and advice.latency_ms <= 3000:
                llm_conf = advice.confidence
        return setup["score"] * (1 - llm_weight) + llm_conf * llm_weight

    def _side(self, setup):
        from .wallet import PositionSide
        return PositionSide.LONG if setup["action"] == "LONG" else PositionSide.SHORT


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--symbol", default="SOLUSDT")
    p.add_argument("--loop", action="store_true")
    p.add_argument("--tick", type=int, default=300)
    p.add_argument("--no-llm", action="store_true")
    p.add_argument("--leverage", type=int, default=10)
    p.add_argument("--testnet", action="store_true")
    args = p.parse_args()

    eng = TradingEngineWS(symbol=args.symbol, leverage=args.leverage, use_llm=not args.no_llm, testnet=args.testnet)
    if args.loop:
        while True:
            eng.run_once()
            time.sleep(args.tick)
    else:
        eng.run_once()


if __name__ == "__main__":
    main()
