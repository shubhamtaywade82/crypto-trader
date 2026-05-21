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

    def _signal_tick(self):
        # Base logic from REST engine
        df4h = self.feed.get_klines(self.symbol, "4h", 200)
        regime, regime_score = self._classify(df4h)
        setup = self._setup(regime, regime_score)
        if setup and self.risk.can_trade() and self.wallet.position is None:
            final_score = self._final_score(setup, regime, regime_score)
            if final_score >= 0.75:
                self._execute_entry(setup, regime, regime_score, final_score)

    def _execute_entry(self, setup, regime, regime_score, final_score):
        intended = self.feed.get_mark_price(self.symbol)
        mark = self._entry_price()
        side = self._side(setup)
        self.wallet.open_position(side, mark, margin=50.0)
        self.risk.record_open()
        snap = self.ws.snapshot()
        spread_pct = ((snap.ask - snap.bid) / mark * 100) if (snap.ask and snap.bid and mark) else None
        slip_pct = abs(mark - intended) / intended * 100 if intended else None
        self.journal.append({
            "symbol": self.symbol, "regime": regime, "regime_score": regime_score,
            "final_score": final_score, "entry_price": mark, "intended_price": intended,
            "spread_pct": spread_pct, "slippage_pct": slip_pct,
            "bid": snap.bid, "ask": snap.ask, "mark_price": snap.mark_price,
            "ltp": snap.last_trade_price, "funding_rate": snap.funding_rate,
        })

    def _check_exits(self):
        pos = self.wallet.position
        if not pos:
            return
        snap = self.ws.snapshot()
        prices = list(snap.wick_buffer)
        if not prices:
            return
        px = prices[-1]
        sl = pos.entry_price * (0.993 if pos.side.name == "LONG" else 1.007)
        tp = pos.entry_price * (1.01 if pos.side.name == "LONG" else 0.99)
        exit_reason = None
        if pos.side.name == "LONG":
            if len(prices) == 5 and all(p <= sl for p in prices):
                exit_reason = "stop_loss"
            elif px >= tp:
                exit_reason = "take_profit"
        else:
            if len(prices) == 5 and all(p >= sl for p in prices):
                exit_reason = "stop_loss"
            elif px <= tp:
                exit_reason = "take_profit"
        if exit_reason:
            realized_pnl = self.wallet.close_position(px)
            self.risk.record_close(realized_pnl)
            logger.info(
                "[%s] position closed reason=%s price=%.4f pnl=%.4f",
                self.symbol, exit_reason, px, realized_pnl,
            )

    def run_once(self):
        self._signal_tick()
        self._check_exits()

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

# guide-compatible alias
WebSocketTradingEngine = TradingEngineWS
