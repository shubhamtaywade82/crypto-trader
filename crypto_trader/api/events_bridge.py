"""
crypto_trader.api.events_bridge — realtime bot → UI event relay over Redis pub/sub
===================================================================================
The bot hooks :class:`RedisEventPublisher` onto the wallet event hook; every domain
event is ``PUBLISH``-ed to a Redis channel. The FastAPI SSE endpoint consumes
:func:`redis_event_source` and streams events to the browser as they arrive — true
realtime, no polling. Publishing never raises into the trading path.
"""
from __future__ import annotations

import json
import logging
from typing import AsyncIterator, Optional

logger = logging.getLogger("crypto_trader.api.events_bridge")


class RedisEventPublisher:
    """Bot-side: relay each domain event dict to a Redis pub/sub channel."""

    def __init__(self, redis_url: str, channel: str = "events:ui"):
        import redis  # lazy
        self.r = redis.Redis.from_url(redis_url, decode_responses=True)
        self.channel = channel

    def __call__(self, event: dict) -> None:
        try:
            self.r.publish(self.channel, json.dumps(event, default=str))
        except Exception as e:  # never let UI relay break trading
            logger.debug("UI event publish failed: %s", e)


async def redis_event_source(redis_url: str, channel: str = "events:ui") -> AsyncIterator[dict]:
    """API-side: async generator yielding events published to ``channel``."""
    import redis.asyncio as aioredis  # lazy
    client = aioredis.from_url(redis_url, decode_responses=True)
    pubsub = client.pubsub()
    await pubsub.subscribe(channel)
    try:
        async for message in pubsub.listen():
            if message.get("type") != "message":
                continue
            data = message.get("data")
            try:
                yield json.loads(data)
            except (TypeError, ValueError):
                yield {"raw": data}
    finally:
        try:
            await pubsub.unsubscribe(channel)
            await client.aclose()
        except Exception:
            pass
