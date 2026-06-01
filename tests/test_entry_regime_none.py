"""
Regression: regime=None must not crash the entry open path.

Both ST2 and MR entry evaluation call ``_execute_entry`` with ``regime=None``
(MR is the DEFAULT trading mode). Two spots inside ``_execute_entry`` formerly
dereferenced ``regime.value`` without a None-guard:

  * ``journal.log_open(regime=regime.value, ...)``      (TradeJournalEntry)
  * ``TradeOpenedEvent(regime=regime.value, ...)``       (Telegram event)

The sibling captures already guard (``regime.value if regime else <sentinel>``).
With regime=None the unguarded lines raised AttributeError *after* the wallet
had already booked the position — the throw was swallowed by the outer entry
try/except, tripping the kill switch on a perfectly valid trade and leaving
``_open_features`` unpopulated so outcome capture broke for that trade.

We drive ``_execute_entry`` directly with regime=None against a lightweight
shim (mirroring tests/test_outcome_wiring.py) so we don't boot the whole engine
(data feeds, WS threads). The assertions lock in: no raise, a position opens,
``_open_features`` is populated, and the regime is stored as the "" sentinel
(matching the str-typed consumers TradeJournalEntry.regime / TradeOpenedEvent.
regime, both ``""``) rather than crashing.
"""
import uuid
from types import SimpleNamespace

from crypto_trader.engine_ws import WebSocketTradingEngine, EntryContext
from crypto_trader.wallet import (
    DATA_DIR,
    EnhancedFuturesWallet,
    Playbook,
    PositionSide,
)
from crypto_trader.journal import TradeJournal


def _fresh_wallet(symbol: str) -> EnhancedFuturesWallet:
    ns = f"test_{uuid.uuid4().hex}"
    for pat in (f"wallet_{ns}_{symbol}*", f"wallet_events_{ns}_{symbol}*"):
        for p in DATA_DIR.glob(pat):
            p.unlink(missing_ok=True)
    return EnhancedFuturesWallet(symbol=symbol, state_namespace=ns)


class _FakeFeed:
    """Always-fresh feed returning a usable LTP/mid/spread."""

    def __init__(self, price: float):
        self._price = price
        self.data = SimpleNamespace(best_bid=price - 0.01, best_ask=price + 0.01)

    def is_fresh(self, _stale_ms):
        return True

    def data_age_ms(self):
        return 0

    def get_ltp(self):
        return self._price

    def get_mid_price(self):
        return self._price

    def get_spread(self):
        return 0.02


class _PassGate:
    """Margin/leverage engine stand-in: every gate passes."""

    def check_margin_utilization(self, *_a, **_k):
        return True, "ok"

    def check_liquidation_distance(self, *_a, **_k):
        return True, "ok"

    def validate_leverage_tier(self, *_a, **_k):
        return True, "ok"


class _Recorder:
    """adaptive_threshold / risk_manager stand-in that records calls."""

    def __init__(self):
        self.opens = 0
        self.trades = 0

    def record_open(self):
        self.opens += 1

    def record_trade(self):
        self.trades += 1


def _cfg():
    return SimpleNamespace(
        entry_order_style="market",
        feed_stale_ms=15000,
        basis_guard_enabled=False,
        entry_basis_buffer=0.0,
        risk_per_trade_pct=0.02,
        leverage=5,
        is_live=False,
        max_cross_venue_basis=0.01,
        mr_entry_band=0.015,
        mode=SimpleNamespace(value="paper"),
    )


def _setup():
    return {
        "side": PositionSide.LONG,
        "entry_price": 100.0,
        "sl_price": 95.0,
        "tp_price": 110.0,
        "playbook": Playbook.INTRADAY,
        "strategy": "mean_reversion",
    }


def _make_shim(tmp_path):
    wallet = _fresh_wallet("BTCUSDT")
    return SimpleNamespace(
        symbol="BTCUSDT",
        cfg=_cfg(),
        signal_publisher=None,
        ws_feed=_FakeFeed(100.0),
        wallet=wallet,
        margin_engine=_PassGate(),
        leverage_engine=_PassGate(),
        risk_manager=_Recorder(),
        adaptive_threshold=_Recorder(),
        journal=TradeJournal(data_dir=tmp_path / "journal"),
        mr_state=SimpleNamespace(record_entry=lambda **_k: None),
        event_bus=None,
        advisor=None,
        position_manager=None,    # AI position manager (engine attr; None = not wired)
        risk_budget_fraction=1.0,
        _open_features={},
        # cross-venue basis guard bypassed (cfg.basis_guard_enabled=False would
        # also short-circuit it, but attaching keeps the shim self-contained).
        _basis_guard=lambda side, mark: (True, 0.0),
    )


def test_entry_with_regime_none_does_not_crash_and_opens(tmp_path):
    shim = _make_shim(tmp_path)
    ctx = EntryContext(
        setup=_setup(),
        final_score=0.9,
        llm_weight=0.0,
        explanation="",
        mark_price=100.0,
        funding_rate=0.0,
        oi_delta=0.0,
        taker_ratio=1.0,
        tech_score=0.8,
        regime=None,            # <-- the bug trigger (MR/ST2 always pass None)
        regime_score=0.0,
        advice=None,
        dynamic_threshold=0.75,
        regime_ctx=None,
    )

    # 1) must not raise
    WebSocketTradingEngine._execute_entry(shim, ctx)

    # 2) a position was opened
    assert len(shim.wallet.positions) == 1
    assert shim.risk_manager.opens == 1

    # 3) open-side features cached for outcome capture, regime as "" sentinel
    assert "BTCUSDT" in shim._open_features
    feats = shim._open_features["BTCUSDT"]
    assert feats["regime"] in ("", None)  # guarded sentinel, not a crash


def test_entry_with_regime_none_emits_event_with_sentinel(tmp_path):
    """The TradeOpenedEvent publish path (event_bus set) must also survive
    regime=None and carry the "" sentinel rather than raising."""
    captured = []
    shim = _make_shim(tmp_path)
    shim.event_bus = SimpleNamespace(publish=lambda e: captured.append(e))

    ctx = EntryContext(
        setup=_setup(), final_score=0.9, llm_weight=0.0, explanation="",
        mark_price=100.0, funding_rate=0.0, oi_delta=0.0, taker_ratio=1.0,
        tech_score=0.8, regime=None, regime_score=0.0, advice=None,
        dynamic_threshold=0.75, regime_ctx=None,
    )

    WebSocketTradingEngine._execute_entry(shim, ctx)

    opened = [e for e in captured if e.__class__.__name__ == "TradeOpenedEvent"]
    assert len(opened) == 1
    assert opened[0].regime == ""
