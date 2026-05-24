"""Config validation, kill switch, live-gate blocking, and wallet live-fill routing."""
from decimal import Decimal

import pytest

from crypto_trader.config import TradingConfig, TradingMode, DataSource
from crypto_trader.risk import RiskManager
from crypto_trader.wallet import EnhancedFuturesWallet, Order, OrderStatus, OrderType, PositionSide


# ── config validation ──────────────────────────────────────────────────────
def test_config_live_requires_credentials():
    cfg = TradingConfig(mode=TradingMode.LIVE, coindcx_api_key="", coindcx_api_secret="")
    errs = cfg.validate()
    assert any("COINDCX" in e for e in errs)


def test_config_rejects_leverage_above_cap():
    cfg = TradingConfig(mode=TradingMode.PAPER, max_leverage=10)
    assert any("MAX_LEVERAGE" in e for e in cfg.validate())


def test_config_paper_ok():
    cfg = TradingConfig(mode=TradingMode.PAPER, max_leverage=2, symbol="SOLUSDT")
    assert cfg.validate() == []
    assert "****" in cfg.redacted()["coindcx_api_key"] or cfg.redacted()["coindcx_api_key"] == "(unset)"


# ── kill switch ─────────────────────────────────────────────────────────────
def test_kill_switch_blocks_and_clears(tmp_path, monkeypatch):
    import crypto_trader.risk as risk_mod
    monkeypatch.setattr(risk_mod, "DATA_DIR", tmp_path)
    rm = RiskManager()
    rm.state_file = tmp_path / "risk_state.json"
    ok, _ = rm.can_trade(current_balance=1000)
    assert ok
    rm.trigger_kill_switch("reconciliation desync")
    ok, reason = rm.can_trade(current_balance=1000)
    assert not ok and "Kill switch" in reason
    rm.clear_kill_switch()
    ok, _ = rm.can_trade(current_balance=1000)
    assert ok


# ── live gate ───────────────────────────────────────────────────────────────
def test_live_gate_blocks_on_critical_failure(monkeypatch):
    from crypto_trader.engine_live import Check, LiveGateBlocked, LiveTradingSystem

    cfg = TradingConfig(mode=TradingMode.LIVE, max_leverage=2, symbol="SOLUSDT",
                        coindcx_api_key="k", coindcx_api_secret="s", data_source=DataSource.COINDCX)
    system = LiveTradingSystem(cfg=cfg)
    # Avoid any network / real components: stub preflight with a failing check.
    monkeypatch.setattr(system, "preflight", lambda: [
        Check("config", True, "valid"),
        Check("coindcx_auth", False, "401 unauthorized", critical=True),
    ])
    with pytest.raises(LiveGateBlocked):
        system.startup_self_test()


def test_paper_gate_passes_with_failing_noncritical(monkeypatch):
    from crypto_trader.engine_live import Check, LiveTradingSystem

    cfg = TradingConfig(mode=TradingMode.PAPER, max_leverage=2, symbol="SOLUSDT")
    system = LiveTradingSystem(cfg=cfg)
    monkeypatch.setattr(system, "preflight", lambda: [
        Check("config", True, "valid"),
        Check("market_data", False, "no feed", critical=False),
    ])
    passed, _ = system.startup_self_test()
    assert passed is True


# ── wallet live-fill routing ─────────────────────────────────────────────────
class _FakeExec:
    def place_order(self, symbol, side, qty, otype, *, trigger_price=None, limit_price=None,
                    reduce_only=False, expires_at=None, client_order_id=None):
        # venue fills at 123.45 regardless of mark price
        return Order(id="ex-9", symbol=symbol, side=side, order_type=otype, quantity=qty,
                     status=OrderStatus.FILLED, created_at=0, reduce_only=reduce_only,
                     filled_quantity=qty, avg_fill_price=Decimal("123.45"))

    def cancel_order(self, oid):
        return True

    def sync_positions(self):
        return {}


def _setup(side=PositionSide.LONG):
    return {
        "entry_price": 120.0, "side": side, "playbook": __import__(
            "crypto_trader.wallet", fromlist=["Playbook"]).Playbook.INTRADAY,
        "sl_price": 118.0, "tp_price": 125.0,
    }


def test_wallet_live_open_uses_venue_fill(tmp_path, monkeypatch):
    monkeypatch.setattr("crypto_trader.wallet.DATA_DIR", tmp_path)
    w = EnhancedFuturesWallet(symbol="SOLUSDT", initial_balance=1000, leverage=2,
                              state_namespace="livetest")
    w.attach_execution_engine(_FakeExec(), live=True)
    pos = w.open_position("SOLUSDT", _setup(), mark_price=120.0)
    assert pos is not None
    # Entry booked at the venue fill price, not the simulated/mark price.
    assert pos.entry_price == Decimal("123.45")


def test_wallet_paper_open_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr("crypto_trader.wallet.DATA_DIR", tmp_path)
    w = EnhancedFuturesWallet(symbol="SOLUSDT", initial_balance=1000, leverage=2,
                              state_namespace="papertest")
    pos = w.open_position("SOLUSDT", _setup(), mark_price=120.0)
    assert pos is not None
    # No execution engine -> simulated execution price near mark (not 123.45).
    assert pos.entry_price != Decimal("123.45")
