"""
crypto_trader.execution.signal_bus — decoupled signal → execution pipeline
===========================================================================
Strategies stay pure: they emit a :class:`Signal` to the ``execution:signals``
Redis stream and never touch the exchange. The :class:`SignalConsumer` drains
the stream through a consumer group and routes each signal through a
Chain-of-Responsibility pipeline:

  PoisonPillHandler → ParseHandler → DuplicateHandler
      → RiskGateHandler → AdapterHandler → ExecuteHandler

Each handler either stops the chain (ack + log / DLQ) or forwards to the next.
Adding a new step means writing a new :class:`~crypto_trader.patterns.Handler`
subclass and inserting it in ``SignalConsumer._build_pipeline()``.

Safety guarantees:
* idempotency lock (no double-fills on retry/replay),
* risk gate run *before* the adapter (velocity / drawdown veto),
* poison-pill → dead-letter routing (no crash loops),
* boot-time PEL recovery (orphaned in-flight signals handled first),
* ``XACK`` only after the adapter succeeds (at-least-once).
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Callable, Dict, Optional, Protocol

from ..infra.redis_streams import RedisStreamBus
from ..patterns.chain import Chain, Handler, HandlerResult

logger = logging.getLogger("crypto_trader.execution.signal_bus")


@dataclass
class Signal:
    strategy_id: str
    symbol: str
    side: str                      # "LONG" | "SHORT" | "BUY" | "SELL"
    quantity: float
    mode: str = "paper"            # "paper" | "live"
    order_type: str = "market"
    price: float = 0.0
    timestamp: int = field(default_factory=lambda: int(time.time() * 1000))
    signal_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict:
        return asdict(self)

    @classmethod
    def from_payload(cls, d: dict) -> "Signal":
        known = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        return cls(**known)


class SignalAdapter(Protocol):
    def execute(self, signal: Signal) -> None: ...


class SignalPublisher:
    """Strategy-side producer."""

    def __init__(self, bus: RedisStreamBus, stream: str = "execution:signals"):
        self.bus = bus
        self.stream = stream

    def emit(self, signal: Signal) -> str:
        msg_id = self.bus.publish(self.stream, signal.to_payload())
        logger.debug("emitted signal %s (%s %s) -> %s", signal.signal_id, signal.side, signal.symbol, msg_id)
        return msg_id


# ─────────────────────────────────────────────────────────────────────────────
# Pipeline context + handler chain
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class _SignalCtx:
    """Mutable context threaded through every handler in the pipeline."""
    msg_id: str
    raw_data: dict
    stream: str
    group: str
    bus: Any                          # RedisStreamBus
    dlq_stream: str
    signal: Optional["Signal"] = None
    adapter: Optional["SignalAdapter"] = None


class _PoisonPillHandler(Handler[_SignalCtx]):
    def __init__(self, max_deliveries: int) -> None:
        super().__init__()
        self.max_deliveries = max_deliveries

    def handle(self, ctx: _SignalCtx) -> HandlerResult:
        count = ctx.bus.delivery_count(ctx.stream, ctx.group, ctx.msg_id)
        if count > self.max_deliveries:
            ctx.bus.to_dlq(ctx.dlq_stream, ctx.raw_data,
                           reason=f"max_deliveries>{self.max_deliveries}")
            ctx.bus.ack(ctx.stream, ctx.group, ctx.msg_id)
            logger.error("signal %s exceeded delivery limit -> DLQ", ctx.msg_id)
            return HandlerResult.stop("poison pill")
        return self.forward(ctx)


class _ParseHandler(Handler[_SignalCtx]):
    def handle(self, ctx: _SignalCtx) -> HandlerResult:
        try:
            ctx.signal = Signal.from_payload(ctx.raw_data)
        except Exception as exc:
            ctx.bus.to_dlq(ctx.dlq_stream, ctx.raw_data, reason=f"malformed: {exc}")
            ctx.bus.ack(ctx.stream, ctx.group, ctx.msg_id)
            return HandlerResult.stop(f"malformed: {exc}")
        return self.forward(ctx)


class _DuplicateHandler(Handler[_SignalCtx]):
    def handle(self, ctx: _SignalCtx) -> HandlerResult:
        sig = ctx.signal
        idem_key = f"idem:{sig.strategy_id}:{sig.timestamp}"
        if ctx.bus.is_processed(idem_key):
            logger.info("duplicate signal %s suppressed", sig.signal_id)
            ctx.bus.ack(ctx.stream, ctx.group, ctx.msg_id)
            return HandlerResult.stop("duplicate")
        return self.forward(ctx)


class _RiskGateHandler(Handler[_SignalCtx]):
    def __init__(self, risk_gate: Optional[Callable[["Signal"], bool]]) -> None:
        super().__init__()
        self._gate = risk_gate

    def handle(self, ctx: _SignalCtx) -> HandlerResult:
        if self._gate is not None and not self._gate(ctx.signal):
            logger.warning(
                "risk gate vetoed signal %s (%s %s)",
                ctx.signal.signal_id, ctx.signal.side, ctx.signal.symbol,
            )
            ctx.bus.ack(ctx.stream, ctx.group, ctx.msg_id)
            return HandlerResult.stop("risk gate veto")
        return self.forward(ctx)


class _AdapterHandler(Handler[_SignalCtx]):
    def __init__(self, adapters: Dict[str, "SignalAdapter"], dlq_stream: str) -> None:
        super().__init__()
        self._adapters = adapters

    def handle(self, ctx: _SignalCtx) -> HandlerResult:
        adapter = self._adapters.get(ctx.signal.mode)
        if adapter is None:
            ctx.bus.to_dlq(ctx.dlq_stream, ctx.raw_data,
                           reason=f"unknown mode '{ctx.signal.mode}'")
            ctx.bus.ack(ctx.stream, ctx.group, ctx.msg_id)
            return HandlerResult.stop(f"unknown mode '{ctx.signal.mode}'")
        ctx.adapter = adapter
        return self.forward(ctx)


class _ExecuteHandler(Handler[_SignalCtx]):
    def __init__(
        self,
        idempotency_ttl: int,
        record_open_fn: Optional[Callable[[], None]],
    ) -> None:
        super().__init__()
        self._ttl = idempotency_ttl
        self._record_open = record_open_fn

    def handle(self, ctx: _SignalCtx) -> HandlerResult:
        sig = ctx.signal
        try:
            ctx.adapter.execute(sig)
        except Exception as exc:
            logger.error(
                "adapter failed for signal %s (will retry): %s", sig.signal_id, exc
            )
            return HandlerResult.stop(f"adapter error: {exc}")

        if self._record_open is not None:
            try:
                self._record_open()
            except Exception as exc:
                logger.error("failed to record open for signal %s: %s", sig.signal_id, exc)

        idem_key = f"idem:{sig.strategy_id}:{sig.timestamp}"
        ctx.bus.mark_processed(idem_key, self._ttl)
        ctx.bus.ack(ctx.stream, ctx.group, ctx.msg_id)
        logger.debug("signal %s executed and acked", sig.signal_id)
        return HandlerResult.proceed()


class SignalConsumer:
    """Execution-side consumer group worker."""

    def __init__(
        self,
        bus: RedisStreamBus,
        adapters: Dict[str, SignalAdapter],
        *,
        stream: str = "execution:signals",
        group: str = "execution_engine",
        consumer: str = "worker-1",
        dlq_stream: Optional[str] = None,
        max_deliveries: int = 3,
        idempotency_ttl_seconds: int = 300,
        risk_gate: Optional[Callable[[Signal], bool]] = None,
        record_open_fn: Optional[Callable[[], None]] = None,
    ):
        self.bus = bus
        self.adapters = adapters
        self.stream = stream
        self.group = group
        self.consumer = consumer
        self.dlq_stream = dlq_stream or f"{stream}:dlq"
        self.max_deliveries = max_deliveries
        self.idempotency_ttl = idempotency_ttl_seconds
        self.risk_gate = risk_gate
        self.record_open_fn = record_open_fn
        self._running = False
        self._pipeline = self._build_pipeline()

    def setup(self) -> None:
        self.bus.ensure_group(self.stream, self.group)

    # ── recovery + main loop ──────────────────────────────────────────────────
    def recover_pending(self) -> int:
        """Handle this consumer's orphaned in-flight messages (PEL) on boot."""
        handled = 0
        for msg_id, data in self.bus.read_pending(self.stream, self.group, self.consumer):
            self._handle(msg_id, data)
            handled += 1
        if handled:
            logger.warning("recovered %d pending signal(s) from PEL", handled)
        return handled

    def run_forever(self, *, block_ms: int = 5000, max_batches: Optional[int] = None) -> None:
        self.setup()
        self._running = True
        batches = 0
        while self._running:
            # Retry un-acked backlog first (drives the poison-pill counter), then
            # take new messages — so a crash mid-execution is always recovered.
            for msg_id, data in self.bus.read_pending(self.stream, self.group, self.consumer):
                self._handle(msg_id, data)
            for msg_id, data in self.bus.read_new(self.stream, self.group, self.consumer, block_ms=block_ms):
                self._handle(msg_id, data)
            batches += 1
            if max_batches is not None and batches >= max_batches:
                break

    def stop(self) -> None:
        self._running = False

    # ── Pipeline construction ─────────────────────────────────────────────────

    def _build_pipeline(self) -> Chain[_SignalCtx]:
        """Build the handler chain. Called once at construction time.

        To add a new processing step, write a new :class:`Handler` subclass
        and insert it at the appropriate position here.
        """
        return Chain(
            _PoisonPillHandler(self.max_deliveries),
            _ParseHandler(),
            _DuplicateHandler(),
            _RiskGateHandler(self.risk_gate),
            _AdapterHandler(self.adapters, self.dlq_stream),
            _ExecuteHandler(self.idempotency_ttl, self.record_open_fn),
        )

    # ── Per-message dispatch ──────────────────────────────────────────────────

    def _handle(self, msg_id: str, data: dict) -> None:
        ctx = _SignalCtx(
            msg_id=msg_id,
            raw_data=data,
            stream=self.stream,
            group=self.group,
            bus=self.bus,
            dlq_stream=self.dlq_stream,
        )
        self._pipeline.run(ctx)


class WalletSignalAdapter:
    """Executes a :class:`Signal` against an ``EnhancedFuturesWallet`` (paper or
    live, depending on whether an execution engine is attached). Bridges the
    transport layer to the existing position lifecycle.

    The signal carries the full entry setup in ``metadata`` (sl_price, and
    either tp_levels for scale-outs or a single tp_price, plus time_stop_hours)
    so the booked position keeps the same risk structure the strategy computed.
    An optional ``event_bus`` lets the adapter emit ``TradeOpenedEvent`` after a
    confirmed open so downstream notifiers (Telegram) still fire on the
    decoupled path.
    """

    def __init__(self, wallet, *, event_bus=None,
                 default_sl_pct: float = 0.007, default_tp_pct: float = 0.010):
        self.wallet = wallet
        self.event_bus = event_bus
        self.default_sl_pct = default_sl_pct
        self.default_tp_pct = default_tp_pct

    def execute(self, signal: Signal) -> None:
        from ..wallet import PositionSide, Playbook
        side = PositionSide.LONG if str(signal.side).upper() in ("LONG", "BUY") else PositionSide.SHORT
        meta = signal.metadata or {}
        entry = float(signal.price) or float(meta.get("entry_price", 0) or 0)
        if entry <= 0:
            raise ValueError(f"signal {signal.signal_id} has no usable price")

        sl = meta.get("sl_price")
        if sl is None:
            sl = entry * (1 - self.default_sl_pct) if side == PositionSide.LONG else entry * (1 + self.default_sl_pct)

        setup = {
            "entry_price": entry, "side": side, "playbook": Playbook.INTRADAY,
            "sl_price": float(sl),
        }
        if meta.get("time_stop_hours") is not None:
            setup["time_stop_hours"] = meta["time_stop_hours"]
        # Prefer multi-level scale-out targets when the strategy supplied them.
        tp_levels = meta.get("tp_levels")
        if tp_levels:
            setup["tp_levels"] = [
                {"price": float(t["price"]), "pct": float(t.get("pct", 1.0)),
                 "hit": False, "label": t.get("label", "TP")}
                for t in tp_levels if t.get("price")
            ]
        else:
            tp = meta.get("tp_price")
            if tp is None:
                tp = entry * (1 + self.default_tp_pct) if side == PositionSide.LONG else entry * (1 - self.default_tp_pct)
            setup["tp_price"] = float(tp)

        # Entry order style. For live maker-limit, acquire the passive fill
        # lock-free, then book it; a skip-fallback miss aborts. Paper maker-limit
        # is simulated inside open_position via the setup flag.
        style = str(meta.get("entry_order_style", "market")).lower()
        setup["entry_order_style"] = style
        qty = float(signal.quantity)
        external_fill = None
        if style == "maker_limit" and getattr(self.wallet, "live_execution", False):
            external_fill = self.wallet.acquire_live_entry_fill(
                signal.symbol, side, qty, entry,
                timeout_s=float(meta.get("maker_limit_timeout_s", 8.0)),
                offset_bps=float(meta.get("maker_limit_offset_bps", 1.0)),
                fallback=str(meta.get("maker_limit_fallback", "market")),
            )
            if external_fill is None:
                return  # unfilled, fallback=skip
            fq = external_fill.get("filled_quantity")
            if fq is not None:
                qty = float(fq)

        if external_fill is not None:
            pos = self.wallet.open_position(
                signal.symbol, setup, entry, custom_quantity=qty, external_fill=external_fill,
            )
        else:
            pos = self.wallet.open_position(signal.symbol, setup, entry, custom_quantity=qty)

        if pos and self.event_bus is not None:
            from ..events import TradeOpenedEvent
            tp_for_event = setup.get("tp_price")
            if tp_for_event is None and setup.get("tp_levels"):
                tp_for_event = setup["tp_levels"][0]["price"]
            self.event_bus.publish(TradeOpenedEvent(
                symbol=signal.symbol, side=side.value,
                leverage=int(getattr(self.wallet, "leverage", 1) or 1),
                entry_price=float(pos.entry_price), stop_loss=float(sl),
                take_profit=float(tp_for_event or 0),
                margin=float(pos.margin_used), notional=float(pos.notional),
                regime=str(meta.get("regime", "")),
                tech_score=float(meta.get("tech_score", 0) or 0),
                llm_decision=str(meta.get("llm_decision", "")),
                funding=float(meta.get("funding_rate", 0) or 0),
                oi_delta=float(meta.get("oi_delta", 0) or 0),
                liquidation_price=float(
                    self.wallet.liquidation_price(pos)
                    if hasattr(self.wallet, "liquidation_price") else 0
                ),
                wallet_balance=float(self.wallet.wallet_balance),
            ))


def build_risk_gate(risk_manager) -> Callable[["Signal"], bool]:
    """A signal-level risk gate: enforces the kill switch + daily/velocity caps
    before execution. Does NOT record the open (that is done after execution)."""
    def gate(signal: "Signal") -> bool:
        ok, reason = risk_manager.can_trade()
        if not ok:
            logger.warning("risk gate blocked %s: %s", signal.symbol, reason)
            return False
        return True
    return gate
