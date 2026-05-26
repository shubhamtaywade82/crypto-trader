"""
crypto_trader.execution.run_consumer — Redis Streams execution worker
======================================================================
Standalone consumer that drains the ``execution:signals`` stream and routes each
signal to the appropriate wallet (paper or live).

Supports idempotency, poison-pill dead-lettering, and boot-time PEL recovery.

It reuses :class:`LiveTradingSystem` for the gated wallet, execution,
routing, and boot-time PEL recovery.
"""
from __future__ import annotations

import logging
from ..config import load_config
from ..infra.redis_streams import RedisStreamBus
from .signal_bus import SignalConsumer, WalletSignalAdapter, build_risk_gate

logger = logging.getLogger("crypto_trader.execution")


def build_consumer() -> SignalConsumer:
    cfg = load_config()
    
    # We use the same system logic as engine_live to ensure consistent validation/wallet logic
    from ..engine_live import LiveTradingSystem
    system = LiveTradingSystem(cfg)
    system.startup_self_test()           # gated build: wallet, execution, reconcile
    if cfg.is_live:
        system.wallet.attach_execution_engine(system.execution_engine, live=True)

    if cfg.projection_enabled and cfg.database_url:
        try:
            from ..storage.projection import PostgresProjection
            system.projection = PostgresProjection(cfg.database_url, mode=cfg.mode.value)
            logger.info("Postgres projection (read-model) enabled in consumer")
            try:
                system.projection.align_with_wallet(system.wallet)
            except Exception as ex:
                logger.error("Failed to align projection with wallet on boot in consumer: %s", ex)
        except Exception as e:
            logger.error("projection init failed: %s", e)
            system.projection = None

    if getattr(system, "event_store", None) is not None or getattr(system, "projection", None) is not None or cfg.redis_enabled:
        prev = system.wallet.event_hook
        bus = RedisStreamBus.from_url(cfg.redis_url) if cfg.redis_enabled else None
        
        def _sink(ev):
            # Ensure mode is included for the UI/Projection to filter correctly
            if isinstance(ev, dict) and "payload" in ev and isinstance(ev["payload"], dict):
                if "mode" not in ev["payload"]:
                    ev["payload"]["mode"] = cfg.mode.value
                    
            if getattr(system, "event_store", None) is not None:
                try: system.event_store.append(ev)
                except Exception as e: logger.error("event store append failed: %s", e)
            if getattr(system, "projection", None) is not None:
                try: system.projection.apply(ev)
                except Exception as e: logger.error("projection apply failed: %s", e)
            if bus:
                try: bus.publish("events:broadcast", ev)
                except Exception as e: logger.error("redis event broadcast failed: %s", e)
            if prev: prev(ev)
        system.wallet.event_hook = _sink

    adapter = WalletSignalAdapter(system.wallet)
    adapters = {"paper": adapter, "live": adapter}  # wallet decides paper vs live
    bus = RedisStreamBus.from_url(cfg.redis_url)
    
    # Ensure stream and group exist
    bus.ensure_group(cfg.signal_stream, cfg.consumer_group)
    
    return SignalConsumer(
        bus, adapters,
        stream=cfg.signal_stream,
        group=cfg.consumer_group,
        max_deliveries=cfg.signal_max_deliveries,
        idempotency_ttl_seconds=cfg.idempotency_ttl_seconds,
        risk_gate=build_risk_gate(system.risk),
        record_open_fn=system.risk.record_open,
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
