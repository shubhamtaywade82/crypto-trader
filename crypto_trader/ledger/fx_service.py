"""
FX rate service — INR ↔ USDT.

Responsibility:
  - Fetch the current INR/USDT spot rate from CoinDCX public ticker.
  - Cache the last known rate in memory and persist every snapshot to
    the fx_rates table so the ledger can reconstruct any historical
    conversion at its original rate.
  - Enforce a freshness guard: any operation that needs an FX rate will
    raise ``StaleRateError`` if the cached rate is older than
    ``max_age_ms`` (default 90 s).
  - Return conservative rates:
      * buy (INR → USDT)  → ask  (user pays more INR per USDT)
      * sell (USDT → INR) → bid  (user receives fewer INR per USDT)
      * mid is stored for reporting / reference only.
"""

from __future__ import annotations

import logging
import time
from decimal import Decimal
from typing import Optional

import requests

from .db import LedgerDB
from .models import FxRate

logger = logging.getLogger("crypto_trader.ledger.fx_service")

# CoinDCX public ticker endpoint — no auth required.
# The USDTINR pair is listed as "USDTINR" on CoinDCX spot.
_TICKER_URL = "https://public.coindcx.com/market_data/trade_history"
_TICKER_ALL  = "https://api.coindcx.com/exchange/ticker"

_D = Decimal


class StaleRateError(RuntimeError):
    """Raised when the cached FX rate is too old to be trusted."""


class FXService:
    """
    Thread-safe FX rate provider for the INR ↔ USDT pair.

    Parameters
    ----------
    db:
        Shared LedgerDB instance (used to persist rate snapshots).
    max_age_ms:
        How stale the cached rate may be before operations are blocked.
        Default 90 000 ms (90 s).
    spread_fallback_pct:
        If the exchange returns only a mid price (no bid/ask),
        we synthesize bid = mid*(1 - spread/2) and ask = mid*(1 + spread/2).
        Default 0.10 % (0.001).
    """

    SOURCE = "coindcx_spot"
    PAIR   = "USDTINR"

    def __init__(
        self,
        db: LedgerDB,
        max_age_ms: int = 90_000,
        spread_fallback_pct: float = 0.001,
        timeout: int = 8,
    ) -> None:
        self._db = db
        self._max_age_ms = max_age_ms
        self._spread = _D(str(spread_fallback_pct))
        self._timeout = timeout
        self._cached: Optional[FxRate] = None
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "crypto-trader/4.0"})

    # ── Public API ────────────────────────────────────────────────────────

    def get_rate(self, *, allow_stale: bool = False) -> FxRate:
        """
        Return the current INR/USDT FxRate.

        Refreshes from the exchange if the cache is absent or stale.
        Raises StaleRateError if refresh fails and ``allow_stale=False``.
        """
        if self._cached is None or self._cached.is_stale(self._max_age_ms):
            try:
                self._refresh()
            except Exception as exc:
                if self._cached is not None and allow_stale:
                    logger.warning("[FXService] Using stale rate (%s ms old): %s",
                                   int(time.time() * 1000) - self._cached.captured_at, exc)
                    return self._cached
                raise StaleRateError(
                    f"FX rate refresh failed and no cached rate available: {exc}"
                ) from exc

        assert self._cached is not None
        if not allow_stale and self._cached.is_stale(self._max_age_ms):
            raise StaleRateError(
                f"FX rate is {int(time.time()*1000) - self._cached.captured_at} ms old "
                f"(limit {self._max_age_ms} ms)"
            )
        return self._cached

    def ask(self) -> _D:
        """Conservative rate for INR → USDT (user pays ask INR per USDT)."""
        return self.get_rate().ask

    def bid(self) -> _D:
        """Conservative rate for USDT → INR (user receives bid INR per USDT)."""
        return self.get_rate().bid

    def mid(self) -> _D:
        """Mid rate for reporting / display purposes only."""
        return self.get_rate().mid

    def inr_to_usdt(self, inr: _D) -> _D:
        """Convert INR amount to USDT using the ask rate (conservative buy)."""
        rate = self.ask()  # INR per 1 USDT
        if rate <= _D("0"):
            raise ValueError("FX ask rate is zero or negative")
        return inr / rate

    def usdt_to_inr(self, usdt: _D) -> _D:
        """Convert USDT amount to INR using the bid rate (conservative sell)."""
        return usdt * self.bid()

    def force_refresh(self) -> FxRate:
        """Force an immediate re-fetch, bypassing the cache TTL."""
        self._refresh()
        assert self._cached is not None
        return self._cached

    # ── Internal ─────────────────────────────────────────────────────────

    def _refresh(self) -> None:
        """Fetch rate from CoinDCX public ticker and update the cache."""
        rate = self._fetch_from_coindcx()
        self._cached = rate
        self._persist(rate)
        logger.debug("[FXService] Refreshed USDT/INR: bid=%s ask=%s mid=%s",
                     rate.bid, rate.ask, rate.mid)

    def _fetch_from_coindcx(self) -> FxRate:
        """
        Hit CoinDCX public /exchange/ticker and extract USDTINR.

        CoinDCX ticker response is a list of dicts:
            [{"market": "USDTINR", "bid": "85.20", "ask": "85.40", ...}, ...]
        """
        resp = self._session.get(_TICKER_ALL, timeout=self._timeout)
        resp.raise_for_status()
        tickers = resp.json()

        entry = None
        if isinstance(tickers, list):
            for t in tickers:
                if isinstance(t, dict) and t.get("market") == self.PAIR:
                    entry = t
                    break
        elif isinstance(tickers, dict):
            entry = tickers.get(self.PAIR)

        if entry is None:
            raise ValueError(f"USDTINR pair not found in CoinDCX ticker response")

        bid = _D(str(entry.get("bid") or entry.get("last_price") or 0))
        ask = _D(str(entry.get("ask") or entry.get("last_price") or 0))
        last = _D(str(entry.get("last_price") or entry.get("last") or 0))

        # Fallback: if only last_price is available, synthesize bid/ask
        if bid <= _D("0") or ask <= _D("0"):
            mid_price = last if last > _D("0") else _D("85")  # hard fallback
            half = mid_price * self._spread / _D("2")
            bid = mid_price - half
            ask = mid_price + half
            logger.warning("[FXService] No bid/ask from ticker; synthesising from last=%s", last)

        mid = (bid + ask) / _D("2")

        return FxRate(
            from_currency="INR",
            to_currency="USDT",
            bid=bid,
            ask=ask,
            mid=mid,
            source=self.SOURCE,
        )

    def _persist(self, rate: FxRate) -> None:
        """Persist the rate snapshot to the fx_rates table."""
        try:
            sql = self._db._adapt(
                "INSERT INTO fx_rates "
                "(from_currency, to_currency, bid, ask, mid, source, captured_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)"
            )
            with self._db.cursor() as cur:
                cur.execute(sql, (
                    rate.from_currency,
                    rate.to_currency,
                    str(rate.bid),
                    str(rate.ask),
                    str(rate.mid),
                    rate.source,
                    rate.captured_at,
                ))
        except Exception as exc:
            # Rate persistence is best-effort — do not block trading
            logger.error("[FXService] Failed to persist FX rate: %s", exc)

    def get_latest_persisted(self, max_age_ms: int = 300_000) -> Optional[FxRate]:
        """
        Load the most recent rate from the database.

        Useful for warm-starting after a process restart so we don't have
        to wait for the first live refresh before accepting operations.
        """
        sql = self._db._adapt(
            "SELECT bid, ask, mid, source, captured_at FROM fx_rates "
            "WHERE from_currency = %s AND to_currency = %s "
            "ORDER BY captured_at DESC LIMIT 1"
        )
        try:
            with self._db.cursor() as cur:
                cur.execute(sql, ("INR", "USDT"))
                row = cur.fetchone()
        except Exception:
            return None

        if row is None:
            return None

        rate = FxRate(
            from_currency="INR",
            to_currency="USDT",
            bid=_D(str(row[0])),
            ask=_D(str(row[1])),
            mid=_D(str(row[2])),
            source=str(row[3]),
            captured_at=int(row[4]),
        )
        if rate.is_stale(max_age_ms):
            return None
        return rate

    def warm_start(self) -> bool:
        """
        Pre-load cache from the database (call once at startup).

        Returns True if a usable rate was found, False otherwise.
        """
        rate = self.get_latest_persisted()
        if rate is not None:
            self._cached = rate
            logger.info("[FXService] Warm-started from DB: bid=%s ask=%s", rate.bid, rate.ask)
            return True
        return False
