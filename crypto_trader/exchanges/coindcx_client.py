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
    ):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.timeout = timeout
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
    def _send(self, method: str, url: str, *, headers: dict, data: Optional[str], params: Optional[dict]) -> Any:
        last_exc: Optional[Exception] = None
        for attempt in range(self.max_retries):
            try:
                resp = self.session.request(
                    method, url, headers=headers, data=data, params=params, timeout=self.timeout
                )
                if resp.status_code == 429:
                    sleep_s = 5 * (attempt + 1)
                    logger.warning(f"CoinDCX rate limited; backing off {sleep_s}s")
                    time.sleep(sleep_s)
                    continue
                if resp.status_code >= 500:
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
                return _safe_json(resp)
            except requests.Timeout:
                sleep_s = self.backoff_base ** attempt
                logger.warning(f"CoinDCX timeout (attempt {attempt+1}); retry in {sleep_s}s")
                time.sleep(sleep_s)
                last_exc = requests.Timeout("CoinDCX request timed out")
            except CoinDCXError:
                raise
            except Exception as e:  # network blip
                sleep_s = self.backoff_base ** attempt
                logger.warning(f"CoinDCX request error: {e}; retry in {sleep_s}s")
                time.sleep(sleep_s)
                last_exc = e
        raise last_exc if last_exc else CoinDCXError("Max retries exceeded")

    # ── public API ───────────────────────────────────────────────────────────
    def get_public(self, endpoint: str, params: Optional[dict] = None) -> Any:
        """Unauthenticated GET (market data, instruments)."""
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        return self._send("GET", url, headers={}, data=None, params=params)

    def post_signed(self, endpoint: str, payload: Optional[dict] = None) -> Any:
        """Authenticated POST. A fresh ``timestamp`` is injected per request."""
        body_obj = dict(payload or {})
        body_obj["timestamp"] = self._now_ms()
        body = json.dumps(body_obj, separators=(",", ":"))
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        return self._send("POST", url, headers=self._signed_headers(body), data=body, params=None)


def _safe_json(resp: "requests.Response") -> Any:
    try:
        return resp.json()
    except ValueError:
        return resp.text
