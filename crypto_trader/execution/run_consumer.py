"""
crypto_trader.execution.run_consumer — Redis Streams execution worker
======================================================================
Standalone consumer that drains the ``execution:signals`` stream and routes each
signal to the wallet/execution engine. This is the "Execution Orchestrator" of
the decoupled pipeline: strategies publish signals (producer); this worker
executes them (consumer group) with idempotency, a risk gate, poison-pill DLQ
routing, and boot-time PEL recovery.

It reuses :class:`LiveTradingSystem` for the gated wallet/execution/risk build
(so the safe_mode gate, reconciliation and config validation all still apply),
then attaches a :class:`SignalConsumer` on top.

Run:  python -m crypto_trader.execution.run_consumer
Requires:  EXECUTION_BUS=redis and REDIS_URL set.
"""
from __future__ import annotations

import logging

from ..config import load_config
from ..infra.redis_streams import RedisStreamBus
from .signal_bus import SignalConsumer, WalletSignalAdapter, build_risk_gate

logger = logging.getLogger("crypto_trader.execution.run_consumer")


def build_consumer(cfg=None, *, consumer_name: str = "worker-1") -> SignalConsumer:
    cfg = cfg or load_config()
    if not cfg.redis_enabled:
        raise RuntimeError("EXECUTION_BUS=redis and REDIS_URL are required to run the consumer")

    from ..engine_live import LiveTradingSystem
    system = LiveTradingSystem(cfg)
    system.startup_self_test()           # gated build: wallet, execution, reconcile
    if cfg.is_live:
        system.wallet.attach_execution_engine(system.execution_engine, live=True)

    adapter = WalletSignalAdapter(system.wallet)
    adapters = {"paper": adapter, "live": adapter}  # wallet decides paper vs live
    bus = RedisStreamBus.from_url(cfg.redis_url)
    return SignalConsumer(
        bus, adapters,
        stream=cfg.signal_stream,
        group=cfg.consumer_group,
        consumer=consumer_name,
        max_deliveries=cfg.signal_max_deliveries,
        idempotency_ttl_seconds=cfg.idempotency_ttl_seconds,
        risk_gate=build_risk_gate(system.risk),
    )


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    consumer = build_consumer()
    logger.info("Execution consumer started on stream=%s group=%s", consumer.stream, consumer.group)
    try:
        consumer.run_forever()
    except KeyboardInterrupt:
        consumer.stop()
        logger.info("Execution consumer stopped")


if __name__ == "__main__":
    main()
