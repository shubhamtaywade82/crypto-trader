"""
Redis hot-projection layer for the ledger.

Responsibilities
----------------
* Cache WalletSnapshot and Position objects so the trading engine can read
  them at sub-millisecond latency without touching the database.
* Distributed locks (via SET NX PX) to prevent concurrent order placement
  from racing on the same account.
* Idempotency keys so duplicate WebSocket fills are silently discarded.
* Pub/Sub publication when wallet or position state changes (so other
  processes like a dashboard can subscribe).

Design notes
------------
* All values are stored as JSON strings — no binary serialisation needed.
* Redis is a cache/bus, NOT the source of truth.  The database is.
  If Redis is unavailable, the system degrades gracefully to DB reads.
* Key schema:
    wallet:{user_id}:{venue}           → JSON WalletSnapshot
    position:{position_uid}            → JSON Position
    positions_index:{user_id}:{venue}  → SMEMBERS of open position_uid strings
    lock:{scope}:{key}                 → NX lock (string)
    idempotency:{scope}:{key}          → SET member (string, TTL 24 h)
"""

from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager
from decimal import Decimal
from typing import Any, Dict, Iterator, List, Optional

from .models import Position, WalletSnapshot

logger = logging.getLogger("crypto_trader.ledger.redis_layer")

_LOCK_TTL_MS   = 5_000   # 5 s — long enough for one order round-trip
_IDEM_TTL_S    = 86_400  # 24 h idempotency window


def _to_json(obj: Any) -> str:
    """Serialise any dict that might contain Decimal values."""
    def _default(v):
        if isinstance(v, Decimal):
            return str(v)
        raise TypeError(f"Not serialisable: {type(v)}")
    return json.dumps(obj, default=_default)


class _NullRedis:
    """
    No-op Redis substitute used when redis-py is not installed or
    REDIS_URL is not set.  All operations silently succeed or return None.
    """
    def get(self, key: str) -> None:               return None
    def set(self, *a, **kw) -> bool:               return True
    def delete(self, *keys) -> int:                return 0
    def sadd(self, key: str, *members) -> int:     return 0
    def smembers(self, key: str) -> set:           return set()
    def srem(self, key: str, *members) -> int:     return 0
    def expire(self, key: str, seconds: int) -> bool: return True
    def publish(self, channel: str, msg: str) -> int: return 0
    def exists(self, *keys) -> int:                return 0

    # Pipeline stub
    def pipeline(self) -> "_NullPipe":             return _NullPipe()


class _NullPipe:
    def set(self, *a, **kw):   return self
    def delete(self, *a):      return self
    def sadd(self, *a):        return self
    def srem(self, *a):        return self
    def publish(self, *a):     return self
    def execute(self):         return []


def _build_client(redis_url: str) -> Any:
    """
    Attempt to connect to Redis.  Falls back to _NullRedis so that the
    rest of the application can run without Redis installed.
    """
    if not redis_url:
        logger.info("[RedisLayer] REDIS_URL not set — using null Redis (no caching)")
        return _NullRedis()
    try:
        import redis as redis_lib  # type: ignore
        client = redis_lib.from_url(redis_url, decode_responses=True)
        client.ping()
        logger.info("[RedisLayer] Connected to Redis at %s", redis_url)
        return client
    except Exception as exc:
        logger.warning("[RedisLayer] Redis unavailable (%s) — using null Redis", exc)
        return _NullRedis()


class RedisLayer:
    """
    Hot-projection layer backed by Redis.

    Parameters
    ----------
    redis_url:
        ``redis://...`` or ``rediss://...`` URL.  Empty string → NullRedis.
    default_ttl_s:
        Expiry for wallet/position snapshots.  Default 300 s (5 min).
        The DB is the source of truth, so stale cache entries self-heal.
    """

    _WALLET_TTL_S   = 300
    _POSITION_TTL_S = 300

    def __init__(self, redis_url: str = "", default_ttl_s: int = 300) -> None:
        self._r = _build_client(redis_url)
        self._wallet_ttl   = default_ttl_s
        self._position_ttl = default_ttl_s

    # ── Wallet Snapshot ───────────────────────────────────────────────────

    def put_wallet(self, snap: WalletSnapshot) -> None:
        """Cache a WalletSnapshot."""
        key = self._wallet_key(snap.account_id, snap.venue)
        try:
            self._r.set(key, _to_json(snap.to_dict()), ex=self._wallet_ttl)
        except Exception as exc:
            logger.debug("[RedisLayer] put_wallet failed: %s", exc)

    def get_wallet(
        self,
        user_id: str,
        venue: str,
    ) -> Optional[WalletSnapshot]:
        """Return cached WalletSnapshot or None."""
        key = self._wallet_key(user_id, venue)
        try:
            raw = self._r.get(key)
            if raw is None:
                return None
            d = json.loads(raw)
            return self._dict_to_wallet(d, user_id, venue)
        except Exception as exc:
            logger.debug("[RedisLayer] get_wallet failed: %s", exc)
            return None

    def invalidate_wallet(self, user_id: str, venue: str) -> None:
        """Force next read to go to the database."""
        try:
            self._r.delete(self._wallet_key(user_id, venue))
        except Exception:
            pass

    # ── Position ──────────────────────────────────────────────────────────

    def put_position(self, pos: Position) -> None:
        """Cache a Position and register it in the venue index."""
        key = self._pos_key(pos.position_uid)
        idx = self._pos_index_key(pos.user_id, pos.venue)
        try:
            pipe = self._r.pipeline()
            pipe.set(key, _to_json(pos.to_dict()), ex=self._position_ttl)
            if pos.status == "open":
                pipe.sadd(idx, pos.position_uid)
            else:
                pipe.srem(idx, pos.position_uid)
            pipe.execute()
        except Exception as exc:
            logger.debug("[RedisLayer] put_position failed: %s", exc)

    def get_position(self, position_uid: str) -> Optional[Dict]:
        """Return cached position dict or None."""
        try:
            raw = self._r.get(self._pos_key(position_uid))
            return json.loads(raw) if raw else None
        except Exception as exc:
            logger.debug("[RedisLayer] get_position failed: %s", exc)
            return None

    def list_open_position_uids(self, user_id: str, venue: str) -> List[str]:
        """Return UIDs of all cached open positions for a venue."""
        try:
            members = self._r.smembers(self._pos_index_key(user_id, venue))
            return list(members)
        except Exception:
            return []

    def invalidate_position(self, position_uid: str) -> None:
        try:
            self._r.delete(self._pos_key(position_uid))
        except Exception:
            pass

    # ── Distributed locks ─────────────────────────────────────────────────

    @contextmanager
    def lock(self, scope: str, key: str, ttl_ms: int = _LOCK_TTL_MS) -> Iterator[bool]:
        """
        Acquire a distributed lock.  Yields True if acquired, False if not.

        Usage::

            with redis_layer.lock("order", user_id) as acquired:
                if not acquired:
                    raise RuntimeError("Concurrent order placement blocked")
                place_order(...)
        """
        lock_key = f"lock:{scope}:{key}"
        acquired = False
        try:
            acquired = bool(
                self._r.set(lock_key, "1", nx=True, px=ttl_ms)
            )
            yield acquired
        finally:
            if acquired:
                try:
                    self._r.delete(lock_key)
                except Exception:
                    pass

    def try_lock(self, scope: str, key: str, ttl_ms: int = _LOCK_TTL_MS) -> bool:
        """
        Non-context-manager lock.  Returns True if the lock was acquired.
        Caller must call ``release_lock()`` when done.
        """
        lock_key = f"lock:{scope}:{key}"
        try:
            return bool(self._r.set(lock_key, "1", nx=True, px=ttl_ms))
        except Exception:
            return True  # Fail open so lock loss doesn't halt trading

    def release_lock(self, scope: str, key: str) -> None:
        try:
            self._r.delete(f"lock:{scope}:{key}")
        except Exception:
            pass

    # ── Idempotency ───────────────────────────────────────────────────────

    def is_duplicate(self, scope: str, key: str) -> bool:
        """
        Return True if this (scope, key) has been seen before.

        On first call returns False and records the key.
        On subsequent calls returns True (the operation was already processed).
        """
        idem_key = f"idempotency:{scope}:{key}"
        try:
            result = self._r.set(idem_key, "1", nx=True, ex=_IDEM_TTL_S)
            # SET NX returns None if key already existed
            return result is None
        except Exception:
            return False  # Fail open — let the operation proceed

    # ── Pub/Sub ───────────────────────────────────────────────────────────

    def publish_wallet_update(self, user_id: str, venue: str, snap: WalletSnapshot) -> None:
        channel = f"wallet_updates:{user_id}"
        try:
            self._r.publish(channel, _to_json({
                "venue": venue,
                "wallet": snap.to_dict(),
            }))
        except Exception:
            pass

    def publish_position_update(self, pos: Position) -> None:
        channel = f"position_updates:{pos.user_id}"
        try:
            self._r.publish(channel, _to_json(pos.to_dict()))
        except Exception:
            pass

    # ── Helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _wallet_key(user_id: str, venue: str) -> str:
        return f"wallet:{user_id}:{venue}"

    @staticmethod
    def _pos_key(position_uid: str) -> str:
        return f"position:{position_uid}"

    @staticmethod
    def _pos_index_key(user_id: str, venue: str) -> str:
        return f"positions_index:{user_id}:{venue}"

    @staticmethod
    def _dict_to_wallet(d: Dict, user_id: str, venue: str) -> WalletSnapshot:
        _d = lambda k: Decimal(str(d[k])) if d.get(k) is not None else Decimal("0")
        return WalletSnapshot(
            venue=d.get("venue", venue),
            settlement_currency=d.get("settlement_currency", "USDT"),
            wallet_balance=_d("wallet_balance"),
            margin_balance=_d("margin_balance"),
            available_balance=_d("available_balance"),
            locked_balance=_d("locked_balance"),
            unrealized_pnl=_d("unrealized_pnl"),
            realized_pnl=_d("realized_pnl"),
            funding_accrued=_d("funding_accrued"),
            fees_accrued=_d("fees_accrued"),
            account_id=user_id,
            snapshot_at=int(d.get("snapshot_at", int(time.time() * 1000))),
        )
