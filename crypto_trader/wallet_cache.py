"""
crypto_trader.wallet_cache — Redis hot-cache for EnhancedFuturesWallet

Provides sub-millisecond reads for wallet state and real-time pub/sub
updates.  Redis is a cache, NOT the source of truth — the database
remains canonical.  If Redis is unavailable the system degrades
gracefully to DB reads.

Key schema
----------
    wallet:{mode}:{namespace}:snapshot   → JSON wallet snapshot
    wallet:{mode}:{namespace}:positions  → JSON list of open positions

All keys have a 300 s TTL so stale entries self-heal.
"""
from __future__ import annotations

import json
import logging
from decimal import Decimal
from typing import Any, Dict, List, Optional

logger = logging.getLogger("crypto_trader.wallet_cache")

_DEFAULT_TTL_S = 300


def _to_json(obj: Any) -> str:
    """Serialise dicts that may contain Decimal values."""
    def _default(v):
        if isinstance(v, Decimal):
            return str(v)
        raise TypeError(f"Not serialisable: {type(v)}")
    return json.dumps(obj, default=_default)


class _NullRedis:
    """No-op substitute when redis-py is missing or REDIS_URL is unset."""
    def get(self, key: str) -> None:               return None
    def set(self, *a, **kw) -> bool:               return True
    def delete(self, *keys) -> int:                return 0
    def expire(self, key: str, seconds: int) -> bool: return True
    def publish(self, channel: str, msg: str) -> int: return 0


def _build_client(redis_url: str) -> Any:
    if not redis_url:
        logger.debug("[WalletCache] REDIS_URL not set — using null Redis")
        return _NullRedis()
    try:
        import redis as redis_lib  # type: ignore
        client = redis_lib.from_url(redis_url, decode_responses=True)
        client.ping()
        logger.info("[WalletCache] Connected to Redis at %s", redis_url)
        return client
    except Exception as exc:
        logger.warning("[WalletCache] Redis unavailable (%s) — using null Redis", exc)
        return _NullRedis()


class WalletRedisCache:
    """
    Lightweight Redis cache for wallet state.

    Parameters
    ----------
    redis_url:
        ``redis://...`` URL.  Empty → NullRedis (no caching).
    mode:
        ``paper`` or ``live`` — prefixes keys for isolation.
    namespace:
        Wallet state namespace (e.g. ``default``, ``paper``, ``live``).
    ttl_s:
        Key expiry in seconds.  Default 300 s.
    """

    def __init__(
        self,
        redis_url: str = "",
        mode: str = "paper",
        namespace: str = "default",
        ttl_s: int = _DEFAULT_TTL_S,
    ) -> None:
        self._r = _build_client(redis_url)
        self._mode = mode
        self._ns = namespace
        self._ttl = ttl_s

    # ── Key builders ──────────────────────────────────────────────────────

    def _snap_key(self) -> str:
        return f"wallet:{self._mode}:{self._ns}:snapshot"

    def _positions_key(self) -> str:
        return f"wallet:{self._mode}:{self._ns}:positions"

    def _channel(self) -> str:
        return f"wallet_updates:{self._mode}:{self._ns}"

    # ── Write ─────────────────────────────────────────────────────────────

    def put_snapshot(self, snapshot: Dict[str, Any]) -> None:
        """Cache a wallet snapshot dict."""
        try:
            self._r.set(self._snap_key(), _to_json(snapshot), ex=self._ttl)
        except Exception as exc:
            logger.debug("[WalletCache] put_snapshot failed: %s", exc)

    def put_positions(self, positions: List[Dict[str, Any]]) -> None:
        """Cache open positions list."""
        try:
            self._r.set(self._positions_key(), _to_json(positions), ex=self._ttl)
        except Exception as exc:
            logger.debug("[WalletCache] put_positions failed: %s", exc)

    def invalidate(self) -> None:
        """Force next read to go to the database."""
        try:
            self._r.delete(self._snap_key(), self._positions_key())
        except Exception:
            pass

    # ── Read ──────────────────────────────────────────────────────────────

    def get_snapshot(self) -> Optional[Dict[str, Any]]:
        """Return cached snapshot dict or None."""
        try:
            raw = self._r.get(self._snap_key())
            return json.loads(raw) if raw else None
        except Exception as exc:
            logger.debug("[WalletCache] get_snapshot failed: %s", exc)
            return None

    def get_positions(self) -> Optional[List[Dict[str, Any]]]:
        """Return cached positions list or None."""
        try:
            raw = self._r.get(self._positions_key())
            return json.loads(raw) if raw else None
        except Exception as exc:
            logger.debug("[WalletCache] get_positions failed: %s", exc)
            return None

    # ── Pub/Sub ───────────────────────────────────────────────────────────

    def publish_update(self, snapshot: Dict[str, Any]) -> None:
        """Publish wallet update for real-time dashboards."""
        try:
            self._r.publish(self._channel(), _to_json({
                "mode": self._mode,
                "namespace": self._ns,
                "wallet": snapshot,
            }))
        except Exception:
            pass
