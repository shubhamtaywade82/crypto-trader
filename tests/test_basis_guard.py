"""F3 — cross-venue basis guard in the WebSocket trading engine."""
import uuid
from decimal import Decimal

from crypto_trader.config import TradingConfig
from crypto_trader.engine_ws import WebSocketTradingEngine
from crypto_trader.events import EventBus, SignalRejectedEvent
from crypto_trader.wallet import DATA_DIR, EnhancedFuturesWallet, Playbook, PositionSide


class _FakeMD:
    def __init__(self, price=None, raises=False):
        self._price = price
        self._raises = raises

    def get_mark_price(self, symbol):
        if self._raises:
            raise ValueError("no coindcx price")
        return self._price


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


def _fresh_wallet(symbol):
    ns = f"test_{uuid.uuid4().hex}"
    for pat in (f"wallet_{ns}_{symbol}*", f"wallet_events_{ns}_{symbol}*"):
        for p in DATA_DIR.glob(pat):
            p.unlink(missing_ok=True)
    return EnhancedFuturesWallet(symbol=symbol, state_namespace=ns)


def _engine(cfg, bus=None, ws_price=100.0):
    eng = WebSocketTradingEngine(symbol="SOLUSDT", use_llm=False, cfg=cfg, event_bus=bus,
                                 wallet=_fresh_wallet("SOLUSDT"))
    eng.ws_feed = _FakeWS(ws_price)
    return eng


def _cfg(**kw):
    base = dict(basis_guard_enabled=True, max_cross_venue_basis=0.005, entry_basis_buffer=0.0005)
    base.update(kw)
    return TradingConfig(**base)


def test_basis_guard_blocks_wide_divergence():
    eng = _engine(_cfg())
    eng._coindcx_md = _FakeMD(price=101.0)   # 1% above Binance 100 -> exceeds 0.5%
    allowed, basis = eng._basis_guard(PositionSide.LONG, 100.0)
    assert not allowed
    assert abs(basis - 0.01) < 1e-9


def test_basis_guard_allows_tight_divergence():
    eng = _engine(_cfg())
    eng._coindcx_md = _FakeMD(price=100.2)   # 0.2% -> within 0.5%
    allowed, basis = eng._basis_guard(PositionSide.LONG, 100.0)
    assert allowed


def test_basis_guard_skips_when_coindcx_unavailable():
    eng = _engine(_cfg())
    eng._coindcx_md = _FakeMD(raises=True)
    allowed, basis = eng._basis_guard(PositionSide.SHORT, 100.0)
    assert allowed and basis == 0.0


def test_execute_entry_rejected_on_wide_basis_publishes_event(monkeypatch, tmp_path):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    bus = EventBus()
    rejects = []
    bus.subscribe(SignalRejectedEvent, lambda e: rejects.append(e))
    eng = _engine(_cfg(), bus=bus, ws_price=100.0)
    eng._coindcx_md = _FakeMD(price=101.5)   # 1.5% basis

    opened = []
    monkeypatch.setattr(eng.wallet, "open_position", lambda *a, **k: opened.append(a) or None)

    setup = {"side": PositionSide.LONG, "entry_price": 100.0, "sl_price": 99.0,
             "playbook": Playbook.INTRADAY, "tp_price": 101.0, "score": 0.9}
    eng._execute_entry(setup, final_score=0.9, llm_weight=0.0, explanation="",
                       mark_price=100.0, funding_rate=0.0, oi_delta=0.0, taker_ratio=1.0,
                       tech_score=0.9, regime=None, regime_score=0.0, advice=None,
                       dynamic_threshold=0.75)
    assert not opened          # no entry placed
    assert len(rejects) == 1   # rejection surfaced on the bus
