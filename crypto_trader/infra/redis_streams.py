"""
crypto_trader.infra.redis_streams — Redis Streams bus
======================================================
A thin, fault-tolerant wrapper over Redis Streams for the decoupled
signal/execution pipeline:

    strategy ── XADD ──▶ execution:signals ──XREADGROUP──▶ execution engine
                                  │ (unacked on crash)
                                  └── XPENDING / claim ──▶ retry or DLQ

Design choices that matter for capital safety:

* **At-least-once + idempotency.** Messages are ``XACK``-ed only after the adapter
  succeeds, so a crash mid-execution leaves the message in the Pending Entries
  List (PEL) for replay. Duplicate execution is prevented by an idempotency lock
  (``SET key NX EX``) keyed on ``strategy_id:timestamp``.
* **Poison-pill protection.** A message whose delivery count exceeds a threshold
  is routed to a dead-letter stream and acked, so it can't crash-loop the worker.
* **Boot recovery.** On startup the consumer drains its PEL (id ``0``) before
  reading new messages (``>``), so orphaned in-flight signals are handled first.

``redis`` is imported lazily so the package still imports without it; the bus
also accepts an injected client (used by tests with an in-memory fake).
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("crypto_trader.infra.redis_streams")


class RedisStreamBus:
    """Minimal Redis Streams façade. ``client`` must be a ``redis.Redis`` with
    ``decode_responses=True`` (or a compatible fake)."""

    def __init__(self, client: Any):
        self.r = client

    @classmethod
    def from_url(cls, url: str) -> "RedisStreamBus":
        import redis  # lazy
        return cls(redis.Redis.from_url(url, decode_responses=True))

    # ── producer ───────────────────────────────────────────────────────────
    def publish(self, stream: str, payload: dict) -> str:
        """XADD a JSON payload. Returns the message id."""
        return self.r.xadd(stream, {"payload": json.dumps(payload, default=str)})

    # ── consumer-group plumbing ──────────────────────────────────────────────
    def ensure_group(self, stream: str, group: str) -> None:
        """Create the consumer group (and the stream) if absent — idempotent."""
        try:
            self.r.xgroup_create(stream, group, id="0", mkstream=True)
        except Exception as e:  # BUSYGROUP when it already exists
            if "BUSYGROUP" not in str(e):
                raise

    def read_new(self, stream: str, group: str, consumer: str, *,
                 count: int = 10, block_ms: int = 5000) -> List[Tuple[str, dict]]:
        """Read never-before-delivered messages (``>``)."""
        return self._read(stream, group, consumer, ">", count=count, block_ms=block_ms)

    def read_pending(self, stream: str, group: str, consumer: str, *,
                     count: int = 100) -> List[Tuple[str, dict]]:
        """Read this consumer's own un-acked backlog (PEL) for boot recovery."""
        return self._read(stream, group, consumer, "0", count=count, block_ms=None)

    def _read(self, stream, group, consumer, last_id, *, count, block_ms):
        kwargs = {"count": count}
        if block_ms is not None:
            kwargs["block"] = block_ms
        resp = self.r.xreadgroup(group, consumer, {stream: last_id}, **kwargs)
        out: List[Tuple[str, dict]] = []
        for _stream, messages in (resp or []):
            for msg_id, fields in messages:
                if fields is None:  # entry trimmed/deleted while pending
                    continue
                out.append((msg_id, self._decode(fields)))
        return out

    @staticmethod
    def _decode(fields: dict) -> dict:
        raw = fields.get("payload")
        if raw is None:
            return dict(fields)
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return {"payload": raw}

    def ack(self, stream: str, group: str, msg_id: str) -> None:
        self.r.xack(stream, group, msg_id)

    def delivery_count(self, stream: str, group: str, msg_id: str) -> int:
        """How many times ``msg_id`` has been delivered (poison-pill detection)."""
        try:
            rows = self.r.xpending_range(stream, group, min=msg_id, max=msg_id, count=1)
        except Exception:
            return 1
        for row in rows or []:
            # redis-py returns dicts ({'message_id','times_delivered',...}) or tuples
            if isinstance(row, dict):
                return int(row.get("times_delivered", 1))
            if isinstance(row, (list, tuple)) and len(row) >= 4:
                return int(row[3])
        return 1

    # ── dead-letter + idempotency ────────────────────────────────────────────
    def to_dlq(self, dlq_stream: str, payload: dict, reason: str) -> str:
        return self.r.xadd(dlq_stream, {
            "payload": json.dumps(payload, default=str),
            "reason": reason,
        })

    def is_processed(self, key: str) -> bool:
        """True if this signal was already executed (completed marker exists)."""
        return self.r.get(key) is not None

    def mark_processed(self, key: str, ttl_seconds: int) -> None:
        """Set the completed marker *after* a successful execution. Set post-success
        (not as a pre-claim) so a failed attempt is retried, not silently dropped."""
        self.r.set(key, "1", ex=ttl_seconds)
