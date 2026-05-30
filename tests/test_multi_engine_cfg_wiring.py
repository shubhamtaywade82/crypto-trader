"""TDD: multi_engine must pass the loaded TradingConfig into each per-symbol
WebSocketTradingEngine so sizing/strategy/regime/guards honor the config instead
of silently falling back to hardcoded defaults.

Covers:
  - per-symbol engine receives cfg (cfg is not None, spec risk_per_trade_pct=0.005)
  - sizing composition: risk_budget = margin_balance * risk_per_trade_pct * (1/N)
    driven through the REAL _execute_entry path (custom_quantity captured)
  - leverage precedence: explicit CLI --leverage override wins over cfg.leverage,
    otherwise cfg.leverage is used and the constructor leverage matches cfg.leverage
  - construction smoke test: building a per-symbol engine with a real default cfg
    (mean_reversion + legacy default) does not raise

All ~/.crypto_trader writes are redirected to tmp_path (Path.home patch +
risk DATA_DIR patch) so the developer's real state is never touched.
"""
import uuid
from decimal import Decimal
from pathlib import Path

import pytest

from crypto_trader.config import TradingConfig
from crypto_trader.engine_ws import WebSocketTradingEngine
from crypto_trader.wallet import DATA_DIR, EnhancedFuturesWallet, Playbook, PositionSide


# ─────────────────────────────────────────────────────────────────────────────
# Fakes / harness (mirrors tests/test_basis_guard.py)
# ─────────────────────────────────────────────────────────────────────────────

class _FakeWS:
    """Always-fresh WebSocket feed returning a fixed mid/ltp price."""

    def __init__(self, price):
        self._p = price

        class _D:
            best_bid = price
            best_ask = price

        self.data = _D()

    def get_ltp(self):
        return self._p

    def get_mid_price(self):
        return self._p

    def get_spread(self):
        return 0.0

    def is_fresh(self, max_age_ms):
        return True

    def data_age_ms(self):
        return 0


def _fresh_wallet(symbol):
    ns = f"test_{uuid.uuid4().hex}"
    for pat in (f"wallet_{ns}_{symbol}*", f"wallet_events_{ns}_{symbol}*"):
        for p in DATA_DIR.glob(pat):
            p.unlink(missing_ok=True)
    return EnhancedFuturesWallet(symbol=symbol, state_namespace=ns)


def _isolate(monkeypatch, tmp_path):
    """Redirect every ~/.crypto_trader write (wallet/journal/outcomes/risk) to tmp."""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setattr("crypto_trader.risk.DATA_DIR", tmp_path)


def _build_engine_like_multi(cfg, symbol, n_symbols, leverage, wallet, bus=None):
    """Construct a per-symbol engine EXACTLY as multi_engine.main() does after the
    cfg-wiring change: shared wallet, cfg passed, risk_budget_fraction = 1/N."""
    risk_fraction = 1.0 / max(1, n_symbols)
    eng = WebSocketTradingEngine(
        symbol=symbol,
        wallet=wallet,
        leverage=leverage,
        use_llm=False,
        event_bus=bus,
        cfg=cfg,
        risk_budget_fraction=risk_fraction,
    )
    return eng


# ─────────────────────────────────────────────────────────────────────────────
# 1. cfg flows into the per-symbol engine
# ─────────────────────────────────────────────────────────────────────────────

def test_per_symbol_engine_receives_cfg(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    cfg = TradingConfig()  # spec defaults, no profile → risk_per_trade_pct = 0.005
    eng = _build_engine_like_multi(cfg, "BTCUSDT", n_symbols=4, leverage=cfg.leverage,
                                   wallet=_fresh_wallet("BTCUSDT"))
    assert eng.cfg is not None
    assert eng.cfg.risk_per_trade_pct == pytest.approx(0.005)


# ─────────────────────────────────────────────────────────────────────────────
# 2. sizing composition through the real _execute_entry path
# ─────────────────────────────────────────────────────────────────────────────

def test_risk_budget_composes_with_one_over_n(monkeypatch, tmp_path):
    """quantity passed to wallet.open_position must equal
    margin_balance * 0.005 * (1/N) / stop_distance — driven through _execute_entry."""
    _isolate(monkeypatch, tmp_path)
    n_symbols = 4
    cfg = TradingConfig(basis_guard_enabled=False)  # no cross-venue guard in test
    wallet = _fresh_wallet("BTCUSDT")
    # Pin a known margin balance (no open positions → margin_balance == wallet_balance).
    wallet.wallet_balance = Decimal("10000")
    margin_balance = float(wallet.margin_balance)
    assert margin_balance == pytest.approx(10000.0)

    eng = _build_engine_like_multi(cfg, "BTCUSDT", n_symbols=n_symbols,
                                   leverage=cfg.leverage, wallet=wallet)
    eng.ws_feed = _FakeWS(100.0)  # entry mid = 100

    captured = {}

    def _fake_open(symbol, setup, entry_price, custom_margin=None,
                   custom_quantity=None, external_fill=None, **kw):
        captured["qty"] = custom_quantity
        captured["entry"] = entry_price
        captured["sl"] = setup["sl_price"]
        return None  # short-circuit downstream booking

    monkeypatch.setattr(wallet, "open_position", _fake_open)

    setup = {
        "side": PositionSide.LONG,
        "entry_price": 100.0,
        "sl_price": 99.0,            # 1% stop → stop_distance derived from re-anchor
        "tp_price": 101.0,
        "playbook": Playbook.INTRADAY,
        "score": 0.9,
    }
    # final_score == dynamic_threshold → size_multiplier = min(1.0, 1.0) = 1.0,
    # regime_ctx=None → no regime scalar. Isolates the 0.005 * (1/N) composition.
    eng._execute_entry(setup, final_score=0.75, llm_weight=0.0, explanation="",
                       mark_price=100.0, funding_rate=0.0, oi_delta=0.0, taker_ratio=1.0,
                       tech_score=0.9, regime=None, regime_score=0.0, advice=None,
                       dynamic_threshold=0.75, regime_ctx=None)

    assert "qty" in captured, "open_position was not reached — entry vetoed unexpectedly"
    entry = float(captured["entry"])
    stop_distance = abs(entry - float(captured["sl"]))
    expected_risk_budget = margin_balance * 0.005 * (1.0 / n_symbols)
    expected_qty = expected_risk_budget / stop_distance
    assert float(captured["qty"]) == pytest.approx(expected_qty, rel=1e-9)


# ─────────────────────────────────────────────────────────────────────────────
# 3. leverage precedence
# ─────────────────────────────────────────────────────────────────────────────

def test_cfg_leverage_used_when_no_cli_override(monkeypatch, tmp_path):
    """With no explicit CLI override, resolved leverage == cfg.leverage and the
    constructor's leverage matches it (no silent mismatch)."""
    _isolate(monkeypatch, tmp_path)
    cfg = TradingConfig(max_leverage=3)
    from crypto_trader.multi_engine import resolve_leverage
    resolved = resolve_leverage(cli_leverage=None, cfg=cfg)
    assert resolved == 3
    assert cfg.leverage == 3  # cfg unchanged when no override


def test_cli_override_wins_over_cfg(monkeypatch, tmp_path):
    """An explicit CLI --leverage override must win over cfg.leverage AND make
    cfg.leverage agree so the sizing path (cfg.leverage) and constructor agree."""
    _isolate(monkeypatch, tmp_path)
    cfg = TradingConfig(max_leverage=2)
    from crypto_trader.multi_engine import resolve_leverage
    resolved = resolve_leverage(cli_leverage=5, cfg=cfg)
    assert resolved == 5
    # cfg is reconciled so cfg.leverage (read at engine_ws.py:1538) agrees with
    # the constructor leverage — no silent mismatch.
    assert cfg.leverage == 5


# ─────────────────────────────────────────────────────────────────────────────
# 4. construction smoke test (default cfg incl. strategy_mode / regime filters)
# ─────────────────────────────────────────────────────────────────────────────

def test_default_cfg_construction_does_not_raise(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    cfg = TradingConfig()  # default strategy_mode + allowed_regimes etc.
    eng = _build_engine_like_multi(cfg, "ETHUSDT", n_symbols=3, leverage=cfg.leverage,
                                   wallet=_fresh_wallet("ETHUSDT"))
    assert eng.cfg is cfg


def test_mean_reversion_cfg_construction_does_not_raise(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    cfg = TradingConfig(strategy_mode="mean_reversion")
    eng = _build_engine_like_multi(cfg, "ETHUSDT", n_symbols=3, leverage=cfg.leverage,
                                   wallet=_fresh_wallet("ETHUSDT"))
    assert eng.cfg.strategy_mode == "mean_reversion"
