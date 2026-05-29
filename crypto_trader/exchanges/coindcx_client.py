"""
crypto_trader.exchanges.coindcx_client — Signed CoinDCX HTTP client
====================================================================
Low-level transport for CoinDCX REST. Handles HMAC-SHA256 request signing,
retries with exponential backoff, and rate-limit handling. Knows nothing about
trading semantics — that lives in ``coindcx_execution``.

CoinDCX authentication scheme (private endpoints):
    body  = JSON string of the payload, which MUST include a "timestamp" (ms)
    sig   = HMAC_SHA256(api_secret, body)          # hex digest
    headers:
        X-AUTH-APIKEY    = api_key
        X-AUTH-SIGNATURE = sig
        Content-Type     = application/json
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from typing import Any, Optional

import requests

from ..patterns.circuit_breaker import CircuitBreaker, CircuitOpenError

logger = logging.getLogger("crypto_trader.exchanges.coindcx")

COINDCX_BASE = "https://api.coindcx.com"


class CoinDCXError(Exception):
    """Raised on non-retryable CoinDCX API errors."""

    def __init__(self, message: str, status_code: Optional[int] = None, body: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class CoinDCXClient:
    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        base_url: str = COINDCX_BASE,
        max_retries: int = 3,
        backoff_base: float = 2.0,
        timeout: int = 15,
        circuit_breaker: Optional[CircuitBreaker] = None,
    ):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.timeout = timeout
        # Circuit breaker trips on persistent 5xx / network failures so that a
        # down venue fails fast instead of exhausting the full retry budget on
        # every call.  Pass a shared instance to coordinate across all callers.
        # When None a per-instance default is created (5 failures, 60s recovery).
        self._cb: CircuitBreaker = circuit_breaker or CircuitBreaker(
            name="coindcx",
            failure_threshold=5,
            recovery_timeout=60.0,
            expected_exception=(CoinDCXError, requests.Timeout, OSError),
        )
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "User-Agent": "crypto-trader/4.0",
        })

    # ── signing ────────────────────────────────────────────────────────────
    @staticmethod
    def _now_ms() -> int:
        return int(time.time() * 1000)

    def _sign(self, body: str) -> str:
        return hmac.new(
            self.api_secret.encode("utf-8"),
            body.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _signed_headers(self, body: str) -> dict:
        if not self.api_key or not self.api_secret:
            raise CoinDCXError("CoinDCX credentials are required for signed requests")
        return {
            "X-AUTH-APIKEY": self.api_key,
            "X-AUTH-SIGNATURE": self._sign(body),
            "Content-Type": "application/json",
        }

    # ── transport ──────────────────────────────────────────────────────────
    def _send(self, method: str, url: str, *, headers: dict, data: Optional[str],
              params: Optional[dict], retry_safe: bool = True) -> Any:
        """Send a request with bounded retries and circuit-breaker protection.

        ``retry_safe`` MUST be False for non-idempotent order mutations
        (orders/create, positions/exit, orders/edit). CoinDCX has no
        client_order_id idempotency key, so retrying after a timeout / 5xx where
        the venue may already have accepted the order would place a DUPLICATE.
        For those calls a single attempt is made and any ambiguous failure is
        surfaced to the caller (which reconciles against the venue) instead of
        being blindly retried.

        The circuit breaker trips after ``failure_threshold`` consecutive venue-side
        failures (5xx, timeouts, network errors).  Once OPEN, all calls raise
        ``CircuitOpenError`` immediately without touching the network.  4xx errors
        (authentication, validation) never count as failures because they indicate
        a caller bug, not a venue outage.
        """
        if self._cb.is_open:
            raise CircuitOpenError(
                f"CoinDCX circuit is OPEN — venue may be down. "
                f"Retry in {self._cb.recovery_timeout:.0f}s."
            )

        # Non-idempotent calls get exactly one attempt; 429 is the only safe
        # retry because it means the request was rejected before processing.
        max_attempts = self.max_retries if retry_safe else 1
        last_exc: Optional[Exception] = None
        for attempt in range(max_attempts):
            try:
                resp = self.session.request(
                    method, url, headers=headers, data=data, params=params, timeout=self.timeout
                )
                if resp.status_code == 429:
                    # Honour Retry-After when the venue supplies it (G4). 429 is
                    # safe to retry even for order mutations (request rejected).
                    retry_after = _parse_float(resp.headers.get("Retry-After"))
                    sleep_s = retry_after if retry_after and retry_after > 0 else 5 * (attempt + 1)
                    logger.warning(f"CoinDCX rate limited; backing off {sleep_s:.1f}s")
                    if not retry_safe:
                        # Give the venue its requested cool-off, then one retry is
                        # still safe because a 429 was never processed.
                        time.sleep(sleep_s)
                        raise CoinDCXError("rate limited (429)", 429, resp.text)
                    time.sleep(sleep_s)
                    continue
                if resp.status_code >= 500:
                    if not retry_safe:
                        # Ambiguous: the order may already be live. Do NOT retry.
                        raise CoinDCXError(
                            f"CoinDCX {resp.status_code} on non-idempotent call "
                            f"(not retried — reconcile against venue)",
                            resp.status_code, resp.text,
                        )
                    sleep_s = self.backoff_base ** attempt
                    logger.warning(f"CoinDCX {resp.status_code}; retry in {sleep_s}s")
                    time.sleep(sleep_s)
                    last_exc = CoinDCXError("server error", resp.status_code, resp.text)
                    continue
                if resp.status_code >= 400:
                    # 4xx (auth/validation) is not retryable
                    raise CoinDCXError(
                        f"CoinDCX {resp.status_code}: {resp.text[:300]}",
                        resp.status_code,
                        _safe_json(resp),
                    )
                self._apply_backpressure(resp)
                self._cb._on_success()
                return _safe_json(resp)
            except requests.Timeout:
                if not retry_safe:
                    # Ambiguous: the order may have reached the venue. Do NOT
                    # retry — surface so the caller reconciles.
                    raise CoinDCXError(
                        "CoinDCX timeout on non-idempotent call "
                        "(not retried — reconcile against venue)"
                    )
                sleep_s = self.backoff_base ** attempt
                logger.warning(f"CoinDCX timeout (attempt {attempt+1}); retry in {sleep_s}s")
                time.sleep(sleep_s)
                last_exc = requests.Timeout("CoinDCX request timed out")
            except CoinDCXError:
                raise
            except Exception as e:  # network blip
                if not retry_safe:
                    raise
                sleep_s = self.backoff_base ** attempt
                logger.warning(f"CoinDCX request error: {e}; retry in {sleep_s}s")
                time.sleep(sleep_s)
                last_exc = e
        self._cb._on_failure()
        raise last_exc if last_exc else CoinDCXError("Max retries exceeded")

    def _apply_backpressure(self, resp: "requests.Response") -> None:
        """Proactively throttle before hitting 429 (G4).

        Best-effort: CoinDCX does not document standard rate-limit headers, so we
        defensively read common variants. When the remaining quota drops below
        ~15% of the limit, sleep a short bounded delay scaled by depletion so a
        burst of signed calls eases off instead of slamming into a hard 429.
        """
        h = resp.headers
        remaining = _parse_float(h.get("X-RateLimit-Remaining") or h.get("RateLimit-Remaining"))
        limit = _parse_float(h.get("X-RateLimit-Limit") or h.get("RateLimit-Limit"))
        if remaining is None or not limit or limit <= 0:
            return
        ratio = remaining / limit
        if ratio < 0.15:
            sleep_s = min(1.0, (0.15 - ratio) / 0.15)  # 0..1s, more as we deplete
            logger.warning("CoinDCX rate-limit low (%.0f/%.0f); backpressure %.2fs",
                           remaining, limit, sleep_s)
            time.sleep(sleep_s)

    # ── clock skew (G1) ────────────────────────────────────────────────────────
    def measure_clock_skew_ms(self) -> Optional[float]:
        """Estimate local-vs-venue clock drift in ms (positive => local ahead).

        CoinDCX exposes no dedicated server-time endpoint, so we read the standard
        HTTP ``Date`` response header (present on every response, including errors)
        and correct for ~half the round trip. Resolution is ~1s, which is enough to
        catch the multi-second drift that breaks HMAC signature windows. Returns
        ``None`` when the probe fails.
        """
        import email.utils
        url = f"{self.base_url}/exchange/ticker"
        try:
            t0 = time.time()
            resp = self.session.get(url, timeout=3)
            t1 = time.time()
            date_hdr = resp.headers.get("Date")
            if not date_hdr:
                return None
            server_dt = email.utils.parsedate_to_datetime(date_hdr)
            if server_dt is None:
                return None
            server_ms = server_dt.timestamp() * 1000.0
            local_ms = ((t0 + t1) / 2.0) * 1000.0
            return local_ms - server_ms
        except Exception as e:  # network blip — advisory only
            logger.debug("clock-skew probe failed: %s", e)
            return None

    # ── public API ───────────────────────────────────────────────────────────
    def get_public(self, endpoint: str, params: Optional[dict] = None) -> Any:
        """Unauthenticated GET (market data, instruments)."""
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        return self._send("GET", url, headers={}, data=None, params=params)

    def post_signed(self, endpoint: str, payload: Optional[dict] = None,
                    *, retry_safe: bool = True) -> Any:
        """Authenticated POST. A fresh ``timestamp`` is injected per request.

        Pass ``retry_safe=False`` for non-idempotent order mutations so a
        timeout / 5xx is never blindly retried into a duplicate order.
        """
        body_obj = dict(payload or {})
        body_obj["timestamp"] = self._now_ms()
        body = json.dumps(body_obj, separators=(",", ":"))
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        return self._send("POST", url, headers=self._signed_headers(body),
                          data=body, params=None, retry_safe=retry_safe)

    def get_signed(self, endpoint: str, payload: Optional[dict] = None) -> Any:
        """Authenticated GET with a signed JSON body (CoinDCX wallet/margin reads).

        Several CoinDCX read endpoints (futures wallets, cross_margin_details,
        wallet transactions) are GETs that still require the HMAC signature over
        a JSON body containing ``timestamp``.
        """
        body_obj = dict(payload or {})
        body_obj["timestamp"] = self._now_ms()
        body = json.dumps(body_obj, separators=(",", ":"))
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        return self._send("GET", url, headers=self._signed_headers(body), data=body, params=None)


def _safe_json(resp: "requests.Response") -> Any:
    try:
        return resp.json()
    except ValueError:
        return resp.text


def _parse_float(value: Any) -> Optional[float]:
    """Parse a header value to float; None on absence/garbage."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
