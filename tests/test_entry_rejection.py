"""
Regression: a pre-fill venue order REJECTION (CoinDCX 4xx, e.g. min-notional)
must NOT trip the kill switch / live HALT. No position was opened, so it's a
benign skip. Ambiguous failures (5xx/timeout/unknown) MUST still halt.
"""
import pytest

from crypto_trader.engine_ws import WebSocketTradingEngine
from crypto_trader.exchanges.coindcx_client import CoinDCXError


class TestBenignClassification:
    def test_min_notional_400_is_benign(self):
        e = CoinDCXError(
            'CoinDCX 400: {"code":400,"message":"Minimum order value should be 612.0 INR"}',
            400, {"code": 400},
        )
        assert WebSocketTradingEngine._is_benign_entry_rejection(e) is True

    def test_generic_4xx_is_benign(self):
        for sc in (400, 401, 403, 404, 422):
            assert WebSocketTradingEngine._is_benign_entry_rejection(
                CoinDCXError("rejected", sc, None)) is True

    def test_429_is_not_benign(self):
        # rate limit: the client treats it as ambiguous on non-idempotent calls
        assert WebSocketTradingEngine._is_benign_entry_rejection(
            CoinDCXError("rate limited (429)", 429, None)) is False

    def test_5xx_is_not_benign(self):
        assert WebSocketTradingEngine._is_benign_entry_rejection(
            CoinDCXError("server error", 503, None)) is False

    def test_no_status_code_is_not_benign(self):
        # e.g. timeout on non-idempotent call → status_code None → ambiguous
        assert WebSocketTradingEngine._is_benign_entry_rejection(
            CoinDCXError("timeout, reconcile against venue")) is False

    def test_non_coindcx_exception_is_not_benign(self):
        assert WebSocketTradingEngine._is_benign_entry_rejection(ValueError("boom")) is False
