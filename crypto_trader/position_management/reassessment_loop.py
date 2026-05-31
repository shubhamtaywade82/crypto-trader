"""
crypto_trader.position_management.reassessment_loop
====================================================
Background evaluation loop — runs continuously, triggering reassessments
on every open position based on configured triggers.

Trigger types:
  tick        → price changed by ≥ tick_threshold_pct (1%)
  candle      → candle close event dispatched from the engine
  fill        → order fill detected (partial or full)
  shock       → sudden price move > shock_threshold_pct (3%)
  scheduled   → periodic evaluation regardless of price change

Debounce: same position is not re-assessed more often than min_interval_s
unless a shock or fill trigger overrides the debounce.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Callable, Dict, List, Optional

from .ai_assessor import AIAssessor
from .audit_logger import AuditLogger
from .executor import PositionExecutor
from .market_context_engine import MarketContextEngine
from .policy_guard import PolicyGuard
from .state_engine import PositionStateEngine

logger = logging.getLogger("crypto_trader.position_management.reassessment_loop")


class ReassessmentLoop:
    """
    Orchestrates the full management pipeline for every open position.

    Pipeline per position:
      StateEngine   → builds PositionManagementInput
      MarketContextEngine → enriches with live market context
      AIAssessor    → recommends action (with scorer fallback)
      PolicyGuard   → validates / overrides
      Executor      → applies safe action
      AuditLogger   → stores full record

    Thread-safety: each position is evaluated sequentially; the loop runs in a
    background daemon thread.  The main engine thread can trigger immediate
    reassessments via force_reassess(symbol).
    """

    def __init__(
        self,
        state_engine: PositionStateEngine,
        market_ctx_engine: MarketContextEngine,
        ai_assessor: AIAssessor,
        policy_guard: PolicyGuard,
        executor: PositionExecutor,
        audit_logger: AuditLogger,
        tick_interval_s: float = 1.0,
        scheduled_interval_s: float = 60.0,
        min_reassess_interval_s: float = 5.0,
        tick_threshold_pct: float = 0.01,
        shock_threshold_pct: float = 0.03,
        on_action_taken: Optional[Callable[[str, str, str], None]] = None,
    ):
        self.state_engine = state_engine
        self.mce = market_ctx_engine
        self.assessor = ai_assessor
        self.guard = policy_guard
        self.executor = executor
        self.audit = audit_logger

        self.tick_interval_s = tick_interval_s
        self.scheduled_interval_s = scheduled_interval_s
        self.min_reassess_interval_s = min_reassess_interval_s
        self.tick_threshold_pct = tick_threshold_pct
        self.shock_threshold_pct = shock_threshold_pct
        self.on_action_taken = on_action_taken

        # symbol → last evaluated timestamp
        self._last_eval: Dict[str, float] = {}
        # symbol → last known price
        self._last_prices: Dict[str, float] = {}
        # Forced reassessment queue
        self._force_queue: List[tuple] = []   # [(symbol, trigger)]
        self._force_lock = threading.Lock()

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._scheduled_last = 0.0

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def start(self):
        self._running = True
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="pm-reassess"
        )
        self._thread.start()
        logger.info("[ReassessmentLoop] Started")

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("[ReassessmentLoop] Stopped")

    # ── External triggers ─────────────────────────────────────────────────

    def on_tick(self, symbol: str, ltp: float):
        """Call on each LTP update from the WebSocket feed."""
        last = self._last_prices.get(symbol, ltp)
        self._last_prices[symbol] = ltp
        change_pct = abs(ltp - last) / last if last else 0.0
        if change_pct >= self.shock_threshold_pct:
            self.force_reassess(symbol, trigger="shock")
        elif change_pct >= self.tick_threshold_pct:
            self.force_reassess(symbol, trigger="tick")

    def on_candle_close(self, symbol: str):
        """Call when a candle closes for the given symbol."""
        self.force_reassess(symbol, trigger="candle")

    def on_fill(self, symbol: str):
        """Call when an order fill is detected."""
        self.force_reassess(symbol, trigger="fill")

    def force_reassess(self, symbol: str, trigger: str = "manual"):
        """Queue an immediate reassessment for this symbol."""
        with self._force_lock:
            self._force_queue.append((symbol, trigger))

    # ── Main loop ─────────────────────────────────────────────────────────

    def _loop(self):
        while self._running:
            try:
                # Process forced reassessments first
                forced = []
                with self._force_lock:
                    forced, self._force_queue = self._force_queue, []

                for symbol, trigger in forced:
                    self._evaluate_symbol(symbol, trigger, bypass_debounce=True)

                # Scheduled sweep of all positions
                now = time.time()
                if now - self._scheduled_last >= self.scheduled_interval_s:
                    self._scheduled_last = now
                    self._evaluate_all(trigger="scheduled")

            except Exception as exc:
                logger.error("[ReassessmentLoop] Loop error: %s", exc, exc_info=True)

            time.sleep(self.tick_interval_s)

    def _evaluate_all(self, trigger: str):
        """Evaluate all open positions (debounced)."""
        wallet = self.state_engine.wallet
        with wallet.lock:
            symbols = [s for s, p in wallet.positions.items() if p.status == "OPEN"]
        for symbol in symbols:
            self._evaluate_symbol(symbol, trigger=trigger, bypass_debounce=False)

    def _evaluate_symbol(
        self, symbol: str, trigger: str, bypass_debounce: bool = False
    ):
        now = time.time()
        last = self._last_eval.get(symbol, 0.0)
        if not bypass_debounce and (now - last) < self.min_reassess_interval_s:
            return

        ltp = self._last_prices.get(symbol)
        if ltp is None:
            return

        self._last_eval[symbol] = now
        cycle_id = str(uuid.uuid4())
        t0 = time.time()

        try:
            # 1. Build market context
            from ..data_feed import BinanceDataFeed
            df = None
            if hasattr(self.mce, "data_feed") and self.mce.data_feed:
                try:
                    df = self.mce.data_feed.get_klines(symbol, interval="1h", limit=200)
                except Exception:
                    pass
            mctx = self.mce.build(symbol=symbol, df=df, current_price=ltp)

            # 2. Build state input
            inp = self.state_engine.build_snapshot(symbol, ltp, market_context=mctx)
            if inp is None:
                return

            # 3. AI assessment
            decision = self.assessor.assess(inp)

            # 4. Policy guard
            policy = self.guard.validate(inp, decision)

            # 5. Execute
            outcome = self.executor.apply(inp, policy)

            # 6. Audit
            latency_ms = int((time.time() - t0) * 1000)
            self.audit.log(
                inp=inp,
                ai_decision=decision,
                policy_decision=policy,
                execution_outcome=outcome,
                trigger=trigger,
                cycle_id=cycle_id,
                latency_ms=latency_ms,
            )

            if policy.approved and outcome == "ok":
                logger.info(
                    "[ReassessmentLoop] [%s] %s → action=%s (score=%.0f conf=%.2f) in %dms",
                    trigger, symbol, policy.action, decision.score, decision.confidence, latency_ms,
                )
                if self.on_action_taken:
                    self.on_action_taken(symbol, policy.action, outcome)
            else:
                logger.debug(
                    "[ReassessmentLoop] [%s] %s → %s / %s (%s)",
                    trigger, symbol, policy.action, outcome,
                    policy.rejection_reason or "no_rejection",
                )

        except Exception as exc:
            logger.error(
                "[ReassessmentLoop] Evaluation error for %s (cycle=%s): %s",
                symbol, cycle_id, exc, exc_info=True,
            )
