"""TDD: TradingConfig.from_env must validate MR_TIMEFRAME (and ST2 timeframes)
against the Binance kline interval set. A malformed value (e.g. a missing
newline merging MR_TIMEFRAME with the next env line → "15mCLOUD_OLLAMA_API_KEY=
key_1") must NOT flow into the Binance kline URL — it must fall back to the
default with a WARNING.
"""
import logging

import pytest

from crypto_trader.config import TradingConfig, _valid_interval


def test_mr_timeframe_valid_passthrough(monkeypatch):
    monkeypatch.setenv("MR_TIMEFRAME", "15m")
    cfg = TradingConfig.from_env()
    assert cfg.mr_timeframe == "15m"


def test_mr_timeframe_valid_alternate(monkeypatch):
    monkeypatch.setenv("MR_TIMEFRAME", "1h")
    cfg = TradingConfig.from_env()
    assert cfg.mr_timeframe == "1h"


def test_mr_timeframe_garbage_falls_back_to_default(monkeypatch, caplog):
    monkeypatch.setenv("MR_TIMEFRAME", "15mCLOUD_OLLAMA_API_KEY=key_1")
    with caplog.at_level(logging.WARNING, logger="crypto_trader.config"):
        cfg = TradingConfig.from_env()
    assert cfg.mr_timeframe == "15m"
    assert any("invalid MR_TIMEFRAME" in r.message for r in caplog.records)


def test_st2_timeframes_validated(monkeypatch, caplog):
    monkeypatch.setenv("ST2_TIMEFRAME", "garbage")
    monkeypatch.setenv("ST2_HTF_TIMEFRAME", "4h")
    with caplog.at_level(logging.WARNING, logger="crypto_trader.config"):
        cfg = TradingConfig.from_env()
    assert cfg.st2_timeframe == "1h"      # fallback default
    assert cfg.st2_htf_timeframe == "4h"  # valid passthrough
    assert any("invalid ST2_TIMEFRAME" in r.message for r in caplog.records)


def test_valid_interval_helper_direct(monkeypatch):
    monkeypatch.delenv("SOME_TF", raising=False)
    assert _valid_interval("SOME_TF", "15m") == "15m"
    monkeypatch.setenv("SOME_TF", "4h")
    assert _valid_interval("SOME_TF", "15m") == "4h"
    monkeypatch.setenv("SOME_TF", "nonsense")
    assert _valid_interval("SOME_TF", "15m") == "15m"
