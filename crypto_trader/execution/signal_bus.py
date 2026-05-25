"""
crypto_trader.execution.signal_bus — decoupled signal → execution pipeline
===========================================================================
Strategies stay pure: they emit a :class:`Signal` to the ``execution:signals``
Redis stream and never touch the exchange. The :class:`SignalConsumer` drains
the stream through a consumer group and routes each signal to a mode-specific
adapter (``paper`` / ``live``), with the safety machinery the review called for:

* idempotency lock (no double-fills on retry/replay),
* a risk gate run *before* the adapter (velocity / drawdown veto),
* poison-pill → dead-letter routing (no crash loops),
* boot-time PEL recovery (orphaned in-flight signals handled first),
* ``XACK`` only after the adapter succeeds (at-least-once).

This layer is transport-only; the actual order placement lives behind the
``SignalAdapter`` Protocol (the live adapter calls the wallet/execution engine).
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Callable, Dict, Optional, Protocol

from ..infra.redis_streams import RedisStreamBus

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
        self._running = False

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

    # ── per-message handling ──────────────────────────────────────────────────
    def _handle(self, msg_id: str, data: dict) -> None:
        # Poison-pill guard: stop re-delivering a message that keeps failing.
        if self.bus.delivery_count(self.stream, self.group, msg_id) > self.max_deliveries:
            self.bus.to_dlq(self.dlq_stream, data, reason=f"max_deliveries>{self.max_deliveries}")
            self.bus.ack(self.stream, self.group, msg_id)
            logger.error("signal %s exceeded delivery limit -> DLQ", msg_id)
            return
        try:
            signal = Signal.from_payload(data)
        except Exception as e:  # malformed payload is a poison pill
            self.bus.to_dlq(self.dlq_stream, data, reason=f"malformed: {e}")
            self.bus.ack(self.stream, self.group, msg_id)
            return

        # Idempotency: skip a signal that already COMPLETED (marker set on success).
        # The marker is intentionally not a pre-claim, so a failed attempt retries.
        idem_key = f"idem:{signal.strategy_id}:{signal.timestamp}"
        if self.bus.is_processed(idem_key):
            logger.info("duplicate signal %s suppressed (already executed)", signal.signal_id)
            self.bus.ack(self.stream, self.group, msg_id)
            return

        # Risk gate runs BEFORE the adapter; a veto is terminal (acked, not retried).
        if self.risk_gate is not None and not self.risk_gate(signal):
            logger.warning("risk gate vetoed signal %s (%s %s)", signal.signal_id, signal.side, signal.symbol)
            self.bus.ack(self.stream, self.group, msg_id)
            return

        adapter = self.adapters.get(signal.mode)
        if adapter is None:
            self.bus.to_dlq(self.dlq_stream, data, reason=f"unknown mode '{signal.mode}'")
            self.bus.ack(self.stream, self.group, msg_id)
            return

        # Execute. On failure we DO NOT ack -> message stays in the PEL and is
        # retried on the next pass, eventually DLQ'd by the poison-pill guard.
        try:
            adapter.execute(signal)
        except Exception as e:
            logger.error("adapter failed for signal %s (will retry): %s", signal.signal_id, e)
            return
        self.bus.mark_processed(idem_key, self.idempotency_ttl)
        self.bus.ack(self.stream, self.group, msg_id)
        logger.debug("signal %s executed and acked", signal.signal_id)


class WalletSignalAdapter:
    """Executes a :class:`Signal` against an ``EnhancedFuturesWallet`` (paper or
    live, depending on whether an execution engine is attached). Bridges the
    transport layer to the existing position lifecycle."""

    def __init__(self, wallet, *, default_sl_pct: float = 0.007, default_tp_pct: float = 0.010):
        self.wallet = wallet
        self.default_sl_pct = default_sl_pct
        self.default_tp_pct = default_tp_pct

    def execute(self, signal: Signal) -> None:
        from ..wallet import PositionSide, Playbook
        side = PositionSide.LONG if str(signal.side).upper() in ("LONG", "BUY") else PositionSide.SHORT
        entry = float(signal.price) or float(signal.metadata.get("entry_price", 0) or 0)
        if entry <= 0:
            raise ValueError(f"signal {signal.signal_id} has no usable price")
        sl = signal.metadata.get("sl_price")
        tp = signal.metadata.get("tp_price")
        if sl is None:
            sl = entry * (1 - self.default_sl_pct) if side == PositionSide.LONG else entry * (1 + self.default_sl_pct)
        if tp is None:
            tp = entry * (1 + self.default_tp_pct) if side == PositionSide.LONG else entry * (1 - self.default_tp_pct)
        setup = {
            "entry_price": entry, "side": side, "playbook": Playbook.INTRADAY,
            "sl_price": float(sl), "tp_price": float(tp),
        }
        self.wallet.open_position(signal.symbol, setup, entry, custom_quantity=float(signal.quantity))


def build_risk_gate(risk_manager) -> Callable[["Signal"], bool]:
    """A signal-level risk gate: enforces the kill switch + daily/velocity caps
    before execution. Records the open so the velocity window advances."""
    def gate(signal: "Signal") -> bool:
        ok, reason = risk_manager.can_trade()
        if not ok:
            logger.warning("risk gate blocked %s: %s", signal.symbol, reason)
            return False
        risk_manager.record_open()
        return True
    return gate
