"""TDD: bump-to-minimum position sizing in WebSocketTradingEngine._execute_entry.

On a tiny account, risk-based sizing (risk_per_trade_pct × 1/N) can produce a
notional below CoinDCX's venue minimum. Per operator decision, such orders must
be BUMPED UP to the smallest venue-legal size when margin allows (accepting
>target risk on the trade), and SKIPPED cleanly when even the minimum can't be
afforded.

Driven through the REAL _execute_entry seam (custom_quantity captured), mirroring
tests/test_multi_engine_cfg_wiring.py. All ~/.crypto_trader writes are redirected
to tmp_path.
"""
import uuid
from decimal import Decimal

import pytest

from crypto_trader.config import TradingConfig
from crypto_trader.engine_ws import WebSocketTradingEngine
from crypto_trader.exchanges.instrument_mapper import InstrumentSpec
from crypto_trader.wallet import DATA_DIR, EnhancedFuturesWallet, Playbook, PositionSide


# ─────────────────────────────────────────────────────────────────────────────
# Fakes / harness
# ─────────────────────────────────────────────────────────────────────────────

class _FakeWS:
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


class _FakeMapper:
    def __init__(self, spec):
        self._spec = spec

    def get_spec(self, symbol, *a, **kw):
        return self._spec


class _FakeExecEngine:
    def __init__(self, spec):
        self.mapper = _FakeMapper(spec)


def _spec(min_notional="6.0", min_quantity="0.01", qty_increment="0.01"):
    return InstrumentSpec(
        internal_symbol="BTCUSDT",
        pair="B-BTC_USDT",
        price_increment=Decimal("0.01"),
        quantity_increment=Decimal(qty_increment),
        min_quantity=Decimal(min_quantity),
        min_notional=Decimal(min_notional),
        max_leverage=5,
        maker_fee_rate=0.0002,
        taker_fee_rate=0.0005,
        status="active",
    )


def _fresh_wallet(symbol):
    ns = f"test_{uuid.uuid4().hex}"
    for pat in (f"wallet_{ns}_{symbol}*", f"wallet_events_{ns}_{symbol}*"):
        for p in DATA_DIR.glob(pat):
            p.unlink(missing_ok=True)
    return EnhancedFuturesWallet(symbol=symbol, state_namespace=ns)


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setattr("crypto_trader.risk.DATA_DIR", tmp_path)


def _build_engine(cfg, symbol, n_symbols, wallet):
    return WebSocketTradingEngine(
        symbol=symbol,
        wallet=wallet,
        leverage=cfg.leverage,
        use_llm=False,
        event_bus=None,
        cfg=cfg,
        risk_budget_fraction=1.0 / max(1, n_symbols),
    )


def _setup(sl_price=99.0):
    return {
        "side": PositionSide.LONG,
        "entry_price": 100.0,
        "sl_price": sl_price,
        "tp_price": 101.0,
        "playbook": Playbook.INTRADAY,
        "score": 0.9,
    }


def _capture_open(monkeypatch, wallet, captured):
    def _fake_open(symbol, setup, entry_price, custom_margin=None,
                   custom_quantity=None, external_fill=None, **kw):
        captured["qty"] = custom_quantity
        captured["entry"] = entry_price
        captured["sl"] = setup["sl_price"]
        return None

    monkeypatch.setattr(wallet, "open_position", _fake_open)


def _run_entry(eng):
    eng._execute_entry(_setup(), final_score=0.75, llm_weight=0.0, explanation="",
                       mark_price=100.0, funding_rate=0.0, oi_delta=0.0, taker_ratio=1.0,
                       tech_score=0.9, regime=None, regime_score=0.0, advice=None,
                       dynamic_threshold=0.75, regime_ctx=None)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Bump-to-minimum: sub-min notional gets bumped, rounded up to increment
# ─────────────────────────────────────────────────────────────────────────────

def test_bump_below_min_notional_to_venue_minimum(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    cfg = TradingConfig(basis_guard_enabled=False)  # risk_per_trade_pct = 0.005
    wallet = _fresh_wallet("BTCUSDT")
    # Tiny account: 30 USDT. risk_budget = 30 * 0.005 * (1/4) = 0.0375,
    # stop_distance = 1.0 → qty = 0.0375, notional = $3.75, below the $6 min.
    wallet.wallet_balance = Decimal("30")
    wallet.execution_engine = _FakeExecEngine(_spec(min_notional="6.0", min_quantity="0.01"))

    eng = _build_engine(cfg, "BTCUSDT", n_symbols=4, wallet=wallet)
    eng.ws_feed = _FakeWS(100.0)

    captured = {}
    _capture_open(monkeypatch, wallet, captured)
    _run_entry(eng)

    assert "qty" in captured, "open_position was not reached — entry vetoed unexpectedly"
    bumped = Decimal(str(captured["qty"]))
    entry = Decimal(str(captured["entry"]))
    # Risk-based qty would be 0.0375 → notional $3.75. Compute actual:
    risk_based_qty = (Decimal("30") * Decimal("0.005") * (Decimal("1") / 4)) / Decimal("1.0")
    risk_notional = risk_based_qty * entry
    # Confirm the risk-based notional was below the venue min (so a bump happened).
    assert risk_notional < Decimal("6.0")
    # Bumped qty must satisfy BOTH constraints, rounded up to 0.01 increment.
    assert bumped * entry >= Decimal("6.0")
    assert bumped >= Decimal("0.01")
    # 6.0 / 100 = 0.06 → already a multiple of 0.01, so bumped == 0.06.
    assert bumped == Decimal("0.06")


def test_bump_rounds_up_to_quantity_increment(monkeypatch, tmp_path):
    """min_notional that does not divide evenly by entry*increment must round UP."""
    _isolate(monkeypatch, tmp_path)
    cfg = TradingConfig(basis_guard_enabled=False)
    wallet = _fresh_wallet("BTCUSDT")
    wallet.wallet_balance = Decimal("49")
    # min_notional 6.5 / entry 100 = 0.065 → not a multiple of 0.01 → round up 0.07.
    wallet.execution_engine = _FakeExecEngine(_spec(min_notional="6.5", min_quantity="0.01"))

    eng = _build_engine(cfg, "BTCUSDT", n_symbols=4, wallet=wallet)
    eng.ws_feed = _FakeWS(100.0)

    captured = {}
    _capture_open(monkeypatch, wallet, captured)
    _run_entry(eng)

    assert "qty" in captured
    bumped = Decimal(str(captured["qty"]))
    assert bumped == Decimal("0.07")  # ceil(0.065 / 0.01) * 0.01
    assert bumped * Decimal("100.0") >= Decimal("6.5")


# ─────────────────────────────────────────────────────────────────────────────
# 2. Affordability veto: bumped notional unaffordable → no order placed
# ─────────────────────────────────────────────────────────────────────────────

def test_affordability_veto_skips_when_minimum_unaffordable(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    cfg = TradingConfig(basis_guard_enabled=False, max_leverage=1)  # leverage 1
    wallet = _fresh_wallet("BTCUSDT")
    # Margin balance only $0.50; venue min notional $6 → required margin $6/1 = $6
    # which exceeds the $0.50 available → veto.
    wallet.wallet_balance = Decimal("0.50")
    wallet.execution_engine = _FakeExecEngine(_spec(min_notional="6.0", min_quantity="0.01"))

    eng = _build_engine(cfg, "BTCUSDT", n_symbols=1, wallet=wallet)
    eng.ws_feed = _FakeWS(100.0)

    captured = {}
    _capture_open(monkeypatch, wallet, captured)
    _run_entry(eng)

    assert "qty" not in captured, "order was placed despite being unaffordable"


# ─────────────────────────────────────────────────────────────────────────────
# 3. stop_distance <= 0 still hard-skips
# ─────────────────────────────────────────────────────────────────────────────

def test_zero_stop_distance_skips(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    cfg = TradingConfig(basis_guard_enabled=False)
    wallet = _fresh_wallet("BTCUSDT")
    wallet.wallet_balance = Decimal("10000")
    wallet.execution_engine = _FakeExecEngine(_spec())

    eng = _build_engine(cfg, "BTCUSDT", n_symbols=1, wallet=wallet)
    eng.ws_feed = _FakeWS(100.0)

    captured = {}
    _capture_open(monkeypatch, wallet, captured)
    # sl == entry → stop_distance 0 (re-anchored stop equals entry).
    setup = _setup(sl_price=100.0)
    eng._execute_entry(setup, final_score=0.75, llm_weight=0.0, explanation="",
                       mark_price=100.0, funding_rate=0.0, oi_delta=0.0, taker_ratio=1.0,
                       tech_score=0.9, regime=None, regime_score=0.0, advice=None,
                       dynamic_threshold=0.75, regime_ctx=None)

    assert "qty" not in captured, "entry should skip when stop_distance == 0"


# ─────────────────────────────────────────────────────────────────────────────
# 4. Paper / no-mapper fallback: uses risk-based qty, no crash
# ─────────────────────────────────────────────────────────────────────────────

def test_no_mapper_fallback_uses_risk_based_qty(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    cfg = TradingConfig(basis_guard_enabled=False)
    wallet = _fresh_wallet("BTCUSDT")
    wallet.wallet_balance = Decimal("10000")
    # No execution_engine attribute → no mapper → fallback path.
    assert getattr(wallet, "execution_engine", None) is None

    eng = _build_engine(cfg, "BTCUSDT", n_symbols=4, wallet=wallet)
    eng.ws_feed = _FakeWS(100.0)

    captured = {}
    _capture_open(monkeypatch, wallet, captured)
    _run_entry(eng)

    assert "qty" in captured, "fallback should still place the risk-based order"
    bumped = Decimal(str(captured["qty"]))
    entry = Decimal(str(captured["entry"]))
    stop = abs(entry - Decimal(str(captured["sl"])))
    expected = (Decimal("10000") * Decimal("0.005") * (Decimal("1") / 4)) / stop
    assert bumped == pytest.approx(expected, rel=1e-9)


# ─────────────────────────────────────────────────────────────────────────────
# 3. Spec-fetch failure: LIVE skips (can't verify min-notional), PAPER falls back
# ─────────────────────────────────────────────────────────────────────────────

class _RaisingMapper:
    def get_spec(self, symbol, *a, **kw):
        raise RuntimeError("instrument endpoint unavailable")


class _RaisingExecEngine:
    def __init__(self):
        self.mapper = _RaisingMapper()


def test_spec_unavailable_in_live_skips_entry(monkeypatch, tmp_path):
    """Live: without the spec we can't guarantee a venue-legal size, so sending
    the risk-based qty would just earn a 612-INR rejection. Skip the tick."""
    from crypto_trader.config import TradingMode
    _isolate(monkeypatch, tmp_path)
    cfg = TradingConfig(basis_guard_enabled=False, mode=TradingMode.LIVE)
    assert cfg.is_live
    wallet = _fresh_wallet("BTCUSDT")
    wallet.wallet_balance = Decimal("10000")
    wallet.execution_engine = _RaisingExecEngine()

    eng = _build_engine(cfg, "BTCUSDT", n_symbols=4, wallet=wallet)
    eng.ws_feed = _FakeWS(100.0)

    captured = {}
    _capture_open(monkeypatch, wallet, captured)
    _run_entry(eng)

    assert "qty" not in captured, "live entry must be skipped when spec is unverifiable"


def test_spec_unavailable_in_paper_falls_back(monkeypatch, tmp_path):
    """Paper: no venue floor — fall back to the risk-based qty (don't skip)."""
    _isolate(monkeypatch, tmp_path)
    cfg = TradingConfig(basis_guard_enabled=False)  # default PAPER
    assert not cfg.is_live
    wallet = _fresh_wallet("BTCUSDT")
    wallet.wallet_balance = Decimal("10000")
    wallet.execution_engine = _RaisingExecEngine()

    eng = _build_engine(cfg, "BTCUSDT", n_symbols=4, wallet=wallet)
    eng.ws_feed = _FakeWS(100.0)

    captured = {}
    _capture_open(monkeypatch, wallet, captured)
    _run_entry(eng)

    assert "qty" in captured, "paper entry should fall back to the risk-based order"
