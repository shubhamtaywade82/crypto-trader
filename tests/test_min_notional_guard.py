import pytest
from decimal import Decimal
from unittest.mock import MagicMock

from crypto_trader.wallet import EnhancedFuturesWallet, PaperExecutionEngine, PositionSide, Playbook
from crypto_trader.exchanges.instrument_mapper import InstrumentSpec
from crypto_trader.execution.signal_bus import WalletSignalAdapter, Signal
from crypto_trader.engine_ws import WebSocketTradingEngine, EntryContext


def test_paper_execution_engine_mapper():
    """Verify that PaperExecutionEngine has a mock mapper and it returns standard InstrumentSpec."""
    wallet = MagicMock(spec=EnhancedFuturesWallet)
    engine = PaperExecutionEngine(wallet)
    
    assert hasattr(engine, "mapper")
    spec = engine.mapper.get_spec("BTCUSDT")
    assert isinstance(spec, InstrumentSpec)
    assert spec.min_notional == Decimal("6.0")
    assert spec.min_quantity == Decimal("0.001")


def test_wallet_signal_adapter_min_notional_guard():
    """Verify that WalletSignalAdapter blocks execution of sub-notional signals."""
    wallet = MagicMock(spec=EnhancedFuturesWallet)
    wallet.wallet_balance = Decimal("100.0")
    
    # Set up a mock live execution engine with a mapper
    execution_engine = MagicMock()
    spec = InstrumentSpec(
        internal_symbol="SOLUSDT",
        pair="B-SOL_USDT",
        price_increment=Decimal("0.01"),
        quantity_increment=Decimal("0.01"),
        min_quantity=Decimal("0.01"),
        min_notional=Decimal("6.0"),
        max_leverage=5,
        maker_fee_rate=0.0002,
        taker_fee_rate=0.0005,
        status="active"
    )
    execution_engine.mapper.get_spec.return_value = spec
    wallet.execution_engine = execution_engine
    
    adapter = WalletSignalAdapter(wallet)
    
    # 1. Under min_notional signal: price=50.0, quantity=0.1 => notional = 5.0 (< 6.0)
    sig_sub_notional = Signal(
        strategy_id="test_strategy",
        symbol="SOLUSDT",
        side="LONG",
        quantity=0.1,
        price=50.0,
        mode="live"
    )
    
    adapter.execute(sig_sub_notional)
    # open_position should NOT have been called because of the notional constraint
    wallet.open_position.assert_not_called()
    
    # 2. Under min_quantity signal: price=50.0, quantity=0.005 (< 0.01)
    sig_sub_qty = Signal(
        strategy_id="test_strategy",
        symbol="SOLUSDT",
        side="LONG",
        quantity=0.005,
        price=50.0,
        mode="live"
    )
    adapter.execute(sig_sub_qty)
    wallet.open_position.assert_not_called()
    
    # 3. Valid signal: price=50.0, quantity=0.20 => notional = 10.0 (> 6.0)
    sig_valid = Signal(
        strategy_id="test_strategy",
        symbol="SOLUSDT",
        side="LONG",
        quantity=0.20,
        price=50.0,
        mode="live"
    )
    adapter.execute(sig_valid)
    wallet.open_position.assert_called_once()


class MockWSFeed:
    def __init__(self, ltp=100.0, mid=100.0):
        self._ltp = ltp
        self._mid = mid
        self.data = MagicMock()
        self.data.best_bid = 99.9
        self.data.best_ask = 100.1

    def is_fresh(self, stale_ms):
        return True

    def get_ltp(self):
        return self._ltp

    def get_mid_price(self):
        return self._mid

    def get_spread(self):
        return 0.2


def test_websocket_trading_engine_min_notional_guard():
    """Verify that WebSocketTradingEngine._execute_entry BUMPS a sub-notional
    risk-based size up to the venue minimum (instead of skipping) when margin
    allows. Risk-based qty 0.04 (notional $2.00 < $6.00 min) must be bumped to
    a venue-legal qty whose notional >= min_notional and qty >= min_quantity,
    rounded UP to the quantity increment, and that bumped qty must reach
    open_position."""
    wallet = MagicMock(spec=EnhancedFuturesWallet)
    wallet.margin_balance = Decimal("100.0")
    wallet.positions = {}
    wallet.leverage = 5
    wallet.maintenance_margin_ratio = 0.005
    wallet.open_position.return_value = None

    execution_engine = MagicMock()
    spec = InstrumentSpec(
        internal_symbol="SOLUSDT",
        pair="B-SOL_USDT",
        price_increment=Decimal("0.01"),
        quantity_increment=Decimal("0.01"),
        min_quantity=Decimal("0.01"),
        min_notional=Decimal("6.0"),
        max_leverage=5,
        maker_fee_rate=0.0002,
        taker_fee_rate=0.0005,
        status="active"
    )
    execution_engine.mapper.get_spec.return_value = spec
    wallet.execution_engine = execution_engine
    wallet.get_open_position.return_value = None
    
    event_bus = MagicMock()
    
    engine = WebSocketTradingEngine(
        symbol="SOLUSDT",
        leverage=5,
        wallet=wallet,
        event_bus=event_bus,
        use_llm=False
    )
    engine.ws_feed = MockWSFeed(ltp=50.0, mid=50.0)
    engine.risk_manager = MagicMock()
    engine.risk_manager.can_trade.return_value = (True, "")
    
    # Config setup
    cfg = MagicMock()
    cfg.risk_per_trade_pct = 0.02
    cfg.feed_stale_ms = 15000
    cfg.basis_guard_enabled = False
    cfg.allowed_regimes = None
    cfg.leverage = 5
    cfg.entry_basis_buffer = 0.0
    # Pin newer config knobs to real values so the engine's fee/spread gates
    # don't compare a float against a bare MagicMock (added after this test).
    cfg.fee_awareness_enabled = False
    cfg.entry_order_style = "market"
    cfg.adaptive_entry_spread_threshold_bps = 2.0
    cfg.is_live = False  # unit test: keep the market path, skip live maker-limit
    engine.cfg = cfg
    
    # Use EntryContext setup: stop distance = 5.0 (from 50.0 to 45.0)
    # risk budget = 100.0 * 0.02 * 1.0 = 2.0
    # quantity = 2.0 / 5.0 = 0.40.
    # Case 1: normal sizing: 0.40 * 50.0 = 20.0 notional (> 6.0) -> Valid order.
    # But let's manipulate risk budget to make quantity tiny: e.g. risk_per_trade_pct = 0.002
    # risk budget = 100.0 * 0.002 = 0.2
    # quantity = 0.2 / 5.0 = 0.04. Notional = 2.0 (< 6.0) -> Should block!
    cfg.risk_per_trade_pct = 0.002
    
    setup = {
        "side": PositionSide.LONG,
        "entry_price": 50.0,
        "sl_price": 45.0,
        "playbook": Playbook.INTRADAY,
        "strategy": "legacy"
    }
    
    ctx = EntryContext(
        setup=setup,
        final_score=1.0,
        llm_weight=0.0,
        explanation="",
        mark_price=50.0,
        funding_rate=0.0,
        oi_delta=0.0,
        taker_ratio=1.0,
        tech_score=1.0,
        regime=None,
        regime_score=1.0,
        advice=None,
        dynamic_threshold=0.5,
        regime_ctx=None
    )
    
    engine._execute_entry(ctx)

    # Bump-to-minimum: notional $2.00 < $6.00 → bump up. Smallest qty satisfying
    # both min_notional ($6.0 / $50 = 0.12) and min_quantity (0.01), rounded up
    # to the 0.01 increment, is 0.12. Margin to afford it = 6.0 / 5 = 1.2 < 100,
    # so the bumped order is placed (not skipped).
    wallet.open_position.assert_called_once()
    _, kwargs = wallet.open_position.call_args
    bumped = Decimal(str(kwargs["custom_quantity"]))
    assert bumped == Decimal("0.12")
    assert bumped * Decimal("50.0") >= spec.min_notional
    assert bumped >= spec.min_quantity
