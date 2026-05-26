"""
crypto_trader.infra.event_routing — shared event sink factory
=============================================================
Centralises the wallet.event_hook wiring pattern so event_store,
projection, and ui_publisher are always plumbed the same way.
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

logger = logging.getLogger("crypto_trader.infra.event_routing")


def build_event_sink(
    *,
    event_store=None,
    projection=None,
    ui_publisher=None,
    mode: str = "",
    prev_hook: Optional[Callable] = None,
) -> Optional[Callable]:
    """Return a wallet event_hook that fans events to store/projection/ui.

    Returns None if nothing is wired (no-op; caller can skip assigning).
    """
    if event_store is None and projection is None and ui_publisher is None:
        return prev_hook  # nothing to do; preserve existing hook

    def _sink(ev):
        if mode and isinstance(ev, dict) and "payload" in ev and isinstance(ev["payload"], dict):
            ev["payload"].setdefault("mode", mode)
        if event_store is not None:
            try:
                event_store.append(ev)
            except Exception as e:
                logger.error("event store append failed: %s", e)
        if projection is not None:
            try:
                projection.apply(ev)
            except Exception as e:
                logger.error("projection apply failed: %s", e)
        if ui_publisher is not None:
            ui_publisher(ev)
        if prev_hook:
            prev_hook(ev)

    return _sink
