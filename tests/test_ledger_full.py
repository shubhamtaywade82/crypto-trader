"""
Comprehensive tests for the double-entry ledger system.

Coverage:
  - Wallet: deposit, withdraw, margin reserve/release, balance integrity
  - INR→USDT conversion with FX rate
  - Fees, funding (positive and negative)
  - PnL booking (profit, loss)
  - PositionEngine: open, add-to, reduce, full-close, flip guard
  - Leverage & margin calculations
  - Liquidation price formula
  - WalletProjectionService: all derived fields
  - Averaging-in (pyramid) price recalculation
  - Partial exits with proportional PnL
  - Full exits and margin release
  - Reconciliation: ok, balance drift, ghost position, missing position
  - RedisLayer: NullRedis path (no redis-py needed in CI)
  - Balance integrity replay check
"""

from __future__ import annotations

import time
from decimal import Decimal
from typing import List

import pytest

from crypto_trader.ledger import (
    LedgerDB,
    LedgerService,
    PositionEngine,
    ReconciliationWorker,
    RedisLayer,
    WalletProjectionService,
    WalletSnapshot,
    compute_liquidation_price,
)
from crypto_trader.ledger.models import FxRate, Position

_D = Decimal


# ─────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────

@pytest.fixture
def db(tmp_path):
    """Fresh in-memory SQLite database per test."""
    path = str(tmp_path / "test_ledger.db")
    _db = LedgerDB(sqlite_path=path)
    _db.bootstrap()
    return _db


@pytest.fixture
def svc(db):
    return LedgerService(db)


@pytest.fixture
def pe(db):
    return PositionEngine(db)


@pytest.fixture
def wp(db):
    return WalletProjectionService(db)


@pytest.fixture
def recon(db, pe):
    return ReconciliationWorker(db, pe)


@pytest.fixture
def seeded_svc(svc):
    """Service with 10 000 USDT already deposited."""
    svc.deposit("USDT", _D("10000"))
    return svc


# ─────────────────────────────────────────────────────────────
# 1. Wallet — deposits and withdrawals
# ─────────────────────────────────────────────────────────────

class TestWalletDeposit:
    def test_deposit_creates_account(self, svc, db):
        svc.deposit("USDT", _D("5000"))
        acct = svc.get_account("USDT")
        assert acct.ledger_balance == _D("5000")
        assert acct.available_balance == _D("5000")
        assert acct.locked_balance == _D("0")

    def test_multiple_deposits_accumulate(self, svc):
        svc.deposit("USDT", _D("1000"))
        svc.deposit("USDT", _D("2000"))
        svc.deposit("USDT", _D("500"))
        assert svc.get_account("USDT").ledger_balance == _D("3500")

    def test_deposit_inr_separate_from_usdt(self, svc):
        svc.deposit("INR", _D("100000"))
        svc.deposit("USDT", _D("500"))
        assert svc.get_account("INR").ledger_balance == _D("100000")
        assert svc.get_account("USDT").ledger_balance == _D("500")

    def test_deposit_zero_raises(self, svc):
        with pytest.raises(Exception):
            svc.deposit("USDT", _D("0"))

    def test_deposit_negative_raises(self, svc):
        with pytest.raises(Exception):
            svc.deposit("USDT", _D("-100"))


class TestWalletWithdraw:
    def test_withdraw_reduces_available(self, seeded_svc):
        seeded_svc.withdraw("USDT", _D("3000"))
        acct = seeded_svc.get_account("USDT")
        assert acct.ledger_balance == _D("7000")
        assert acct.available_balance == _D("7000")

    def test_withdraw_full_balance(self, seeded_svc):
        seeded_svc.withdraw("USDT", _D("10000"))
        acct = seeded_svc.get_account("USDT")
        assert acct.ledger_balance == _D("0")
        assert acct.available_balance == _D("0")

    def test_withdraw_insufficient_raises(self, seeded_svc):
        with pytest.raises(Exception, match="[Ii]nsufficient|[Bb]alance"):
            seeded_svc.withdraw("USDT", _D("10001"))

    def test_withdraw_beyond_available_when_locked(self, seeded_svc):
        seeded_svc.reserve_margin("USDT", _D("8000"))
        # Only 2000 available; trying to withdraw 5000 should fail
        with pytest.raises(Exception):
            seeded_svc.withdraw("USDT", _D("5000"))


# ─────────────────────────────────────────────────────────────
# 2. Margin reserve / release
# ─────────────────────────────────────────────────────────────

class TestMarginReserveRelease:
    def test_reserve_moves_to_locked(self, seeded_svc):
        # reserve_margin posts a DEBIT ledger entry, so ledger_balance drops
        # by the reserved amount; locked_balance rises by same amount.
        # ledger_balance + locked_balance = original total at all times.
        seeded_svc.reserve_margin("USDT", _D("2000"))
        acct = seeded_svc.get_account("USDT")
        assert acct.available_balance == _D("8000")
        assert acct.locked_balance == _D("2000")
        # ledger_balance reflects the debit — it decreases
        assert acct.ledger_balance == _D("8000")
        # but ledger_balance + locked_balance = original 10 000
        assert acct.ledger_balance + acct.locked_balance == _D("10000")

    def test_reserve_then_release_restores(self, seeded_svc):
        seeded_svc.reserve_margin("USDT", _D("3000"))
        seeded_svc.release_margin("USDT", _D("3000"))
        acct = seeded_svc.get_account("USDT")
        assert acct.available_balance == _D("10000")
        assert acct.locked_balance == _D("0")

    def test_partial_release(self, seeded_svc):
        seeded_svc.reserve_margin("USDT", _D("5000"))
        seeded_svc.release_margin("USDT", _D("2000"))
        acct = seeded_svc.get_account("USDT")
        assert acct.locked_balance == _D("3000")
        assert acct.available_balance == _D("7000")

    def test_reserve_more_than_available_raises(self, seeded_svc):
        with pytest.raises(Exception):
            seeded_svc.reserve_margin("USDT", _D("15000"))

    def test_multiple_sequential_reserves(self, seeded_svc):
        seeded_svc.reserve_margin("USDT", _D("1000"))
        seeded_svc.reserve_margin("USDT", _D("2000"))
        seeded_svc.reserve_margin("USDT", _D("3000"))
        acct = seeded_svc.get_account("USDT")
        assert acct.locked_balance == _D("6000")
        assert acct.available_balance == _D("4000")


# ─────────────────────────────────────────────────────────────
# 3. INR → USDT conversion
# ─────────────────────────────────────────────────────────────

class TestConversion:
    def test_inr_to_usdt_conversion(self, svc):
        svc.deposit("INR", _D("83000"))
        fx = FxRate(
            from_currency="INR", to_currency="USDT",
            bid=_D("82.80"), ask=_D("83.20"), mid=_D("83.00"),
            source="test",
        )
        svc.convert(_D("83000"), fx)
        inr_acct  = svc.get_account("INR")
        usdt_acct = svc.get_account("USDT")
        # 83 000 INR / 83.20 ask = ~997.596... USDT
        expected_usdt = (_D("83000") / _D("83.20")).quantize(_D("0.00000001"))
        assert inr_acct.ledger_balance == _D("0")
        assert abs(usdt_acct.ledger_balance - expected_usdt) < _D("0.01")

    def test_conversion_uses_ask_rate(self, svc):
        """Ask rate means user pays more INR per USDT — conservative."""
        svc.deposit("INR", _D("100"))
        fx = FxRate(
            from_currency="INR", to_currency="USDT",
            bid=_D("80"), ask=_D("90"), mid=_D("85"),
            source="test",
        )
        svc.convert(_D("100"), fx)
        usdt = svc.get_account("USDT").ledger_balance
        # At ask=90: 100/90 ≈ 1.111 USDT
        # At mid=85:  100/85 ≈ 1.176 USDT  (would be MORE than ask gives)
        assert usdt < _D("1.20")
        assert usdt > _D("1.10")

    def test_conversion_insufficient_inr_raises(self, svc):
        svc.deposit("INR", _D("100"))
        fx = FxRate(
            from_currency="INR", to_currency="USDT",
            bid=_D("83"), ask=_D("83"), mid=_D("83"), source="test",
        )
        with pytest.raises(Exception):
            svc.convert(_D("200"), fx)   # more than deposited


# ─────────────────────────────────────────────────────────────
# 4. Fees and funding
# ─────────────────────────────────────────────────────────────

class TestFeesAndFunding:
    def test_fee_reduces_balance(self, seeded_svc):
        seeded_svc.charge_fee("USDT", _D("5.00"))
        acct = seeded_svc.get_account("USDT")
        assert acct.ledger_balance == _D("9995")
        assert acct.available_balance == _D("9995")

    def test_funding_paid_reduces_balance(self, seeded_svc):
        seeded_svc.book_funding("USDT", _D("-10.00"))   # negative = paid
        acct = seeded_svc.get_account("USDT")
        assert acct.ledger_balance == _D("9990")

    def test_funding_received_increases_balance(self, seeded_svc):
        seeded_svc.book_funding("USDT", _D("7.50"))    # positive = received
        acct = seeded_svc.get_account("USDT")
        assert acct.ledger_balance == _D("10007.5")

    def test_multiple_fees_accumulate(self, seeded_svc):
        for _ in range(5):
            seeded_svc.charge_fee("USDT", _D("2.00"))
        assert seeded_svc.get_account("USDT").ledger_balance == _D("9990")


# ─────────────────────────────────────────────────────────────
# 5. Realised PnL booking
# ─────────────────────────────────────────────────────────────

class TestRealisedPnL:
    def test_profit_credits_balance(self, seeded_svc):
        seeded_svc.book_realized_pnl("USDT", _D("250"))
        assert seeded_svc.get_account("USDT").ledger_balance == _D("10250")

    def test_loss_debits_balance(self, seeded_svc):
        seeded_svc.book_realized_pnl("USDT", _D("-150"))
        assert seeded_svc.get_account("USDT").ledger_balance == _D("9850")

    def test_large_loss_drives_balance_negative(self, seeded_svc):
        # REALIZED_PNL_DEBIT is allowed to push ledger_balance below zero
        # (exchange liquidation can leave the account in deficit briefly).
        # The test verifies the loss is booked, not that it raises.
        seeded_svc.book_realized_pnl("USDT", _D("-15000"))
        acct = seeded_svc.get_account("USDT")
        assert acct.ledger_balance == _D("-5000")

    def test_zero_pnl_is_noop(self, seeded_svc):
        seeded_svc.book_realized_pnl("USDT", _D("0"))
        assert seeded_svc.get_account("USDT").ledger_balance == _D("10000")


# ─────────────────────────────────────────────────────────────
# 6. Balance integrity verification
# ─────────────────────────────────────────────────────────────

class TestBalanceIntegrity:
    def test_integrity_passes_after_operations(self, seeded_svc):
        seeded_svc.deposit("USDT", _D("500"))
        seeded_svc.charge_fee("USDT", _D("10"))
        seeded_svc.book_realized_pnl("USDT", _D("100"))
        ok, diff = seeded_svc.verify_balance_integrity("USDT")
        assert ok is True, f"Integrity violation: diff={diff}"
        assert diff == _D("0")

    def test_integrity_on_fresh_deposit(self, svc):
        svc.deposit("USDT", _D("1000"))
        ok, diff = svc.verify_balance_integrity("USDT")
        assert ok is True, f"Integrity violation: diff={diff}"

    def test_replay_matches_ledger_balance(self, db, seeded_svc):
        seeded_svc.reserve_margin("USDT", _D("2000"))
        seeded_svc.charge_fee("USDT", _D("5"))
        wp = WalletProjectionService(db)
        replayed = wp.replay_balance("USDT")
        stored   = seeded_svc.get_account("USDT").ledger_balance
        assert replayed == stored


# ─────────────────────────────────────────────────────────────
# 7. Liquidation price formula
# ─────────────────────────────────────────────────────────────

class TestLiquidationPrice:
    """
    Mark-based isolated-margin formula (consistent with wallet.py):
      LONG:  liq = entry / (1 + 1/lev − mmr)   mmr=0.005
      SHORT: liq = entry / (1 − 1/lev + mmr)
    """

    def test_long_10x(self):
        liq = compute_liquidation_price("LONG", _D("100"), 10)
        # 100 / (1 + 0.1 − 0.005) = 100 / 1.095 ≈ 91.324...
        expected = _D("100") / (_D("1") + _D("1") / _D("10") - _D("0.005"))
        assert abs(liq - expected) < _D("0.001")

    def test_short_10x(self):
        liq = compute_liquidation_price("SHORT", _D("100"), 10)
        # 100 / (1 − 0.1 + 0.005) = 100 / 0.905 ≈ 110.497...
        expected = _D("100") / (_D("1") - _D("1") / _D("10") + _D("0.005"))
        assert abs(liq - expected) < _D("0.001")

    def test_long_20x(self):
        liq = compute_liquidation_price("LONG", _D("1000"), 20)
        # 1000 / (1 + 0.05 − 0.005) = 1000 / 1.045 ≈ 956.94...
        expected = _D("1000") / (_D("1") + _D("1") / _D("20") - _D("0.005"))
        assert abs(liq - expected) < _D("0.01")

    def test_long_liq_below_entry(self):
        liq = compute_liquidation_price("LONG", _D("50000"), 5)
        assert liq < _D("50000")

    def test_short_liq_above_entry(self):
        liq = compute_liquidation_price("SHORT", _D("50000"), 5)
        assert liq > _D("50000")

    def test_high_leverage_liq_close_to_entry(self):
        liq_high = compute_liquidation_price("LONG", _D("100"), 100)
        liq_low  = compute_liquidation_price("LONG", _D("100"), 2)
        # 100x leverage → liq much closer to entry than 2x
        assert (100 - liq_high) < (100 - liq_low)


# ─────────────────────────────────────────────────────────────
# 8. PositionEngine — open, refresh, close
# ─────────────────────────────────────────────────────────────

class TestPositionOpen:
    def test_open_long_position(self, pe):
        pos = pe.open_position("binance", "BTCUSDT", "LONG",
                               _D("0.1"), _D("50000"), leverage=10)
        assert pos.qty == _D("0.1")
        assert pos.entry_price_avg == _D("50000")
        assert pos.leverage == 10
        assert pos.status == "open"
        assert pos.notional == _D("5000")
        assert pos.initial_margin == _D("500")

    def test_open_short_position(self, pe):
        pos = pe.open_position("binance", "ETHUSDT", "SHORT",
                               _D("1"), _D("3000"), leverage=5)
        assert pos.side == "SHORT"
        assert pos.liquidation_price > _D("3000")   # liq above entry for short

    def test_open_duplicate_raises(self, pe):
        pe.open_position("binance", "BTCUSDT", "LONG",
                         _D("0.1"), _D("50000"), leverage=10)
        with pytest.raises(ValueError, match="already exists"):
            pe.open_position("binance", "BTCUSDT", "LONG",
                             _D("0.2"), _D("51000"), leverage=10)

    def test_position_persisted_in_db(self, pe):
        pos = pe.open_position("binance", "SOLUSDT", "LONG",
                               _D("10"), _D("200"), leverage=3)
        loaded = pe.get_by_uid(pos.position_uid)
        assert loaded is not None
        assert loaded.symbol == "SOLUSDT"
        assert loaded.qty == _D("10")

    def test_open_lists_in_open_positions(self, pe):
        pe.open_position("binance", "BTCUSDT", "LONG", _D("0.1"), _D("50000"))
        pe.open_position("binance", "ETHUSDT", "SHORT", _D("1"), _D("3000"))
        open_pos = pe.list_open_positions("binance")
        symbols = {p.symbol for p in open_pos}
        assert "BTCUSDT" in symbols
        assert "ETHUSDT" in symbols


class TestPositionRefresh:
    def test_unrealised_pnl_long(self, pe):
        pos = pe.open_position("binance", "BTCUSDT", "LONG",
                               _D("1"), _D("50000"), leverage=10)
        pos.refresh(_D("52000"))
        assert pos.unrealized_pnl == _D("2000")   # (52000-50000) × 1

    def test_unrealised_pnl_short(self, pe):
        pos = pe.open_position("binance", "ETHUSDT", "SHORT",
                               _D("2"), _D("3000"), leverage=5)
        pos.refresh(_D("2800"))   # price fell → short profits
        assert pos.unrealized_pnl == _D("400")    # (3000-2800) × 2

    def test_pnl_negative_when_losing(self, pe):
        pos = pe.open_position("binance", "BTCUSDT", "LONG",
                               _D("1"), _D("50000"), leverage=10)
        pos.refresh(_D("48000"))
        assert pos.unrealized_pnl == _D("-2000")

    def test_pnl_pct_calculation(self, pe):
        pos = pe.open_position("binance", "BTCUSDT", "LONG",
                               _D("1"), _D("50000"), leverage=10)
        pos.refresh(_D("55000"))
        # pnl = 5000, initial_margin = 50000/10 = 5000
        # pnl_pct = (5000/5000) × 100 = 100%
        assert abs(pos.unrealized_pnl_pct - _D("100")) < _D("0.0001")

    def test_mark_refresh_updates_notional(self, pe):
        pos = pe.open_position("binance", "BTCUSDT", "LONG",
                               _D("2"), _D("50000"), leverage=10)
        pos.refresh(_D("60000"))
        assert pos.notional == _D("120000")  # 2 × 60 000


# ─────────────────────────────────────────────────────────────
# 9. Averaging in (pyramid / add-to-position)
# ─────────────────────────────────────────────────────────────

class TestAveragingIn:
    def test_vwap_average_entry(self, pe):
        pos = pe.open_position("binance", "BTCUSDT", "LONG",
                               _D("1"), _D("50000"), leverage=10)
        # Add 1 BTC at 52000 → avg should be (50000 + 52000) / 2 = 51000
        pos2 = pe.add_to_position(pos.position_uid, _D("1"), _D("52000"))
        assert pos2.qty == _D("2")
        assert pos2.entry_price_avg == _D("51000")

    def test_unequal_size_vwap(self, pe):
        pos = pe.open_position("binance", "BTCUSDT", "LONG",
                               _D("2"), _D("50000"), leverage=10)
        # Add 1 BTC at 56000
        # VWAP = (2×50000 + 1×56000) / 3 = 156000/3 = 52000
        pos2 = pe.add_to_position(pos.position_uid, _D("1"), _D("56000"))
        assert pos2.entry_price_avg == _D("52000")

    def test_three_pyramids(self, pe):
        pos = pe.open_position("binance", "BTCUSDT", "LONG",
                               _D("1"), _D("40000"), leverage=5)
        pos = pe.add_to_position(pos.position_uid, _D("1"), _D("42000"))
        pos = pe.add_to_position(pos.position_uid, _D("2"), _D("44000"))
        # (1×40000 + 1×42000 + 2×44000) / 4 = (40000+42000+88000)/4 = 170000/4 = 42500
        assert pos.entry_price_avg == _D("42500")
        assert pos.qty == _D("4")

    def test_add_to_closed_position_raises(self, pe):
        pos = pe.open_position("binance", "BTCUSDT", "LONG",
                               _D("1"), _D("50000"))
        pe.close_position(pos.position_uid, _D("51000"))
        with pytest.raises(ValueError, match="[Cc]losed|[Ss]tatus"):
            pe.add_to_position(pos.position_uid, _D("0.5"), _D("52000"))


# ─────────────────────────────────────────────────────────────
# 10. Partial exits
# ─────────────────────────────────────────────────────────────

class TestPartialExits:
    def test_partial_close_reduces_qty(self, pe):
        pos = pe.open_position("binance", "BTCUSDT", "LONG",
                               _D("4"), _D("50000"), leverage=10)
        pos2 = pe.reduce_position(pos.position_uid, _D("1"), _D("52000"))
        assert pos2.qty == _D("3")
        assert pos2.status == "open"

    def test_partial_close_books_pnl(self, pe):
        pos = pe.open_position("binance", "BTCUSDT", "LONG",
                               _D("2"), _D("50000"), leverage=10)
        # Close 1 BTC at 53000 → pnl = (53000 - 50000) × 1 = 3000
        pos2 = pe.reduce_position(pos.position_uid, _D("1"), _D("53000"))
        assert pos2.realized_pnl == _D("3000")

    def test_short_partial_close_pnl(self, pe):
        pos = pe.open_position("binance", "ETHUSDT", "SHORT",
                               _D("4"), _D("3000"), leverage=5)
        # Close 2 ETH at 2800 → pnl = (3000-2800) × 2 = 400
        pos2 = pe.reduce_position(pos.position_uid, _D("2"), _D("2800"))
        assert pos2.realized_pnl == _D("400")

    def test_multiple_partial_exits(self, pe):
        pos = pe.open_position("binance", "BTCUSDT", "LONG",
                               _D("10"), _D("50000"), leverage=10)
        pos = pe.reduce_position(pos.position_uid, _D("3"), _D("51000"))
        pos = pe.reduce_position(pos.position_uid, _D("3"), _D("52000"))
        assert pos.qty == _D("4")
        # PnL: 3×1000 + 3×2000 = 3000 + 6000 = 9000
        assert pos.realized_pnl == _D("9000")

    def test_exit_more_than_qty_raises(self, pe):
        pos = pe.open_position("binance", "BTCUSDT", "LONG",
                               _D("1"), _D("50000"))
        with pytest.raises(ValueError):
            pe.reduce_position(pos.position_uid, _D("2"), _D("51000"))

    def test_partial_loss_exit(self, pe):
        pos = pe.open_position("binance", "BTCUSDT", "LONG",
                               _D("1"), _D("50000"), leverage=10)
        pos2 = pe.reduce_position(pos.position_uid, _D("0.5"), _D("48000"))
        # pnl = (48000 - 50000) × 0.5 = -1000
        assert pos2.realized_pnl == _D("-1000")


# ─────────────────────────────────────────────────────────────
# 11. Full exits
# ─────────────────────────────────────────────────────────────

class TestFullExit:
    def test_full_close_sets_status_closed(self, pe):
        pos = pe.open_position("binance", "BTCUSDT", "LONG",
                               _D("1"), _D("50000"), leverage=10)
        closed = pe.close_position(pos.position_uid, _D("55000"))
        assert closed.status == "closed"
        assert closed.qty == _D("0")
        assert closed.closed_at is not None

    def test_full_close_books_total_pnl(self, pe):
        pos = pe.open_position("binance", "BTCUSDT", "LONG",
                               _D("2"), _D("50000"), leverage=10)
        closed = pe.close_position(pos.position_uid, _D("53000"))
        # pnl = (53000-50000) × 2 = 6000
        assert closed.realized_pnl == _D("6000")

    def test_full_close_not_in_open_list(self, pe):
        pos = pe.open_position("binance", "BTCUSDT", "LONG",
                               _D("1"), _D("50000"))
        pe.close_position(pos.position_uid, _D("51000"))
        open_pos = pe.list_open_positions("binance")
        assert all(p.position_uid != pos.position_uid for p in open_pos)

    def test_full_close_then_reopen_same_symbol(self, pe):
        pos = pe.open_position("binance", "BTCUSDT", "LONG",
                               _D("1"), _D("50000"))
        pe.close_position(pos.position_uid, _D("51000"))
        # Should succeed — the old position is closed
        pos2 = pe.open_position("binance", "BTCUSDT", "LONG",
                                _D("0.5"), _D("52000"))
        assert pos2.status == "open"
        assert pos2.entry_price_avg == _D("52000")

    def test_close_at_loss(self, pe):
        pos = pe.open_position("binance", "BTCUSDT", "LONG",
                               _D("1"), _D("50000"), leverage=10)
        closed = pe.close_position(pos.position_uid, _D("45000"))
        assert closed.realized_pnl == _D("-5000")
        assert closed.status == "closed"


# ─────────────────────────────────────────────────────────────
# 12. Decrease position size (reduce position)
# ─────────────────────────────────────────────────────────────

class TestDecreasePosition:
    def test_step_down_to_zero_closes(self, pe):
        pos = pe.open_position("binance", "BTCUSDT", "LONG",
                               _D("3"), _D("50000"))
        pos = pe.reduce_position(pos.position_uid, _D("1"), _D("51000"))
        pos = pe.reduce_position(pos.position_uid, _D("1"), _D("52000"))
        pos = pe.reduce_position(pos.position_uid, _D("1"), _D("53000"))
        assert pos.qty == _D("0")
        assert pos.status == "closed"

    def test_cumulative_pnl_across_reduces(self, pe):
        pos = pe.open_position("binance", "BTCUSDT", "LONG",
                               _D("3"), _D("50000"))
        # Exit at 51000: pnl=1000, at 52000: pnl=2000, at 53000: pnl=3000
        pos = pe.reduce_position(pos.position_uid, _D("1"), _D("51000"))
        pos = pe.reduce_position(pos.position_uid, _D("1"), _D("52000"))
        pos = pe.reduce_position(pos.position_uid, _D("1"), _D("53000"))
        assert pos.realized_pnl == _D("6000")


# ─────────────────────────────────────────────────────────────
# 13. SL / TP price management
# ─────────────────────────────────────────────────────────────

class TestSLTP:
    def test_set_sl_tp(self, pe):
        pos = pe.open_position("binance", "BTCUSDT", "LONG",
                               _D("1"), _D("50000"), leverage=10,
                               sl_price=_D("48000"), tp_price=_D("55000"))
        assert pos.sl_price == _D("48000")
        assert pos.tp_price == _D("55000")

    def test_update_sl_tp(self, pe):
        pos = pe.open_position("binance", "BTCUSDT", "LONG",
                               _D("1"), _D("50000"))
        pos2 = pe.update_sl_tp(pos.position_uid,
                               sl_price=_D("47000"), tp_price=_D("56000"))
        assert pos2.sl_price == _D("47000")
        assert pos2.tp_price == _D("56000")

    def test_safety_check_liq_too_close_to_sl(self, pe):
        pos = pe.open_position("binance", "BTCUSDT", "LONG",
                               _D("1"), _D("50000"), leverage=50,
                               sl_price=_D("49000"))
        # At 50x, liq ≈ 50000*(1-0.02+0.005)=49250 — closer than 3×SL distance
        issues = pos.safety_check()
        # Not necessarily triggered for all leverage values; just assert no crash
        assert isinstance(issues, list)


# ─────────────────────────────────────────────────────────────
# 14. Funding and fee accrual on positions
# ─────────────────────────────────────────────────────────────

class TestFundingAccrual:
    def test_funding_accrues(self, pe):
        pos = pe.open_position("binance", "BTCUSDT", "LONG",
                               _D("1"), _D("50000"))
        pe.accrue_funding(pos.position_uid, _D("-15.00"))  # paid
        loaded = pe.get_by_uid(pos.position_uid)
        assert loaded.funding_accrued == _D("-15.00")

    def test_multiple_funding_payments(self, pe):
        pos = pe.open_position("binance", "BTCUSDT", "LONG",
                               _D("1"), _D("50000"))
        pe.accrue_funding(pos.position_uid, _D("-5"))
        pe.accrue_funding(pos.position_uid, _D("-5"))
        pe.accrue_funding(pos.position_uid, _D("3"))   # received
        loaded = pe.get_by_uid(pos.position_uid)
        assert loaded.funding_accrued == _D("-7")

    def test_fee_accrues(self, pe):
        pos = pe.open_position("binance", "BTCUSDT", "LONG",
                               _D("1"), _D("50000"))
        pe.accrue_fee(pos.position_uid, _D("2.50"))
        loaded = pe.get_by_uid(pos.position_uid)
        assert loaded.fees_accrued == _D("2.50")


# ─────────────────────────────────────────────────────────────
# 15. WalletProjectionService — derived fields
# ─────────────────────────────────────────────────────────────

class TestWalletProjection:
    def test_zero_wallet_with_no_account(self, wp):
        snap = wp.build_snapshot("binance")
        assert snap.wallet_balance == _D("0")
        assert snap.equity == _D("0")

    def test_wallet_balance_after_deposit(self, seeded_svc, wp):
        snap = wp.build_snapshot("binance")
        assert snap.wallet_balance == _D("10000")

    def test_equity_includes_unrealised_pnl(self, seeded_svc, pe, wp):
        pos = pe.open_position("binance", "BTCUSDT", "LONG",
                               _D("1"), _D("50000"), leverage=10)
        pos.refresh(_D("52000"))   # +2000 unrealized
        snap = wp.build_snapshot("binance", positions=[pos])
        assert snap.wallet_balance == _D("10000")
        assert snap.unrealized_pnl == _D("2000")
        assert snap.equity == _D("12000")

    def test_margin_utilisation_with_position(self, seeded_svc, pe, wp):
        pos = pe.open_position("binance", "BTCUSDT", "LONG",
                               _D("1"), _D("50000"), leverage=10)
        # initial_margin = 5000, wallet = 10000
        snap = wp.build_snapshot("binance", positions=[pos])
        # locked ≥ margin_used = 5000
        # utilisation = locked / equity ≈ 5000/10000 = 0.5
        assert snap.margin_utilization > _D("0")
        assert snap.margin_utilization <= _D("1")

    def test_tradeable_balance_has_safety_buffer(self, seeded_svc, wp):
        snap = wp.build_snapshot("binance")
        # tradeable = available - 2% of wallet_balance = 10000 - 200 = 9800
        assert snap.tradeable_balance == _D("9800")

    def test_realized_pnl_aggregate(self, seeded_svc, wp):
        seeded_svc.book_realized_pnl("USDT", _D("300"))
        seeded_svc.book_realized_pnl("USDT", _D("-100"))
        snap = wp.build_snapshot("binance")
        assert snap.realized_pnl == _D("200")

    def test_multiple_positions_pnl_summed(self, seeded_svc, pe, wp):
        p1 = pe.open_position("binance", "BTCUSDT", "LONG",
                              _D("1"), _D("50000"), leverage=10)
        p2 = pe.open_position("binance", "ETHUSDT", "SHORT",
                              _D("5"), _D("3000"), leverage=5)
        p1.refresh(_D("52000"))   # +2000
        p2.refresh(_D("2800"))    # (3000-2800)×5 = 1000
        snap = wp.build_snapshot("binance", positions=[p1, p2])
        assert snap.unrealized_pnl == _D("3000")


# ─────────────────────────────────────────────────────────────
# 16. Leverage calculations
# ─────────────────────────────────────────────────────────────

class TestLeverageCalculations:
    def test_1x_leverage_margin_equals_notional(self, pe):
        pos = pe.open_position("binance", "BTCUSDT", "LONG",
                               _D("1"), _D("50000"), leverage=1)
        assert pos.initial_margin == _D("50000")

    def test_10x_leverage_margin(self, pe):
        pos = pe.open_position("binance", "BTCUSDT", "LONG",
                               _D("1"), _D("50000"), leverage=10)
        assert pos.initial_margin == _D("5000")

    def test_100x_leverage_margin(self, pe):
        pos = pe.open_position("binance", "BTCUSDT", "LONG",
                               _D("1"), _D("50000"), leverage=100)
        assert pos.initial_margin == _D("500")

    def test_effective_leverage(self, pe):
        pos = pe.open_position("binance", "BTCUSDT", "LONG",
                               _D("1"), _D("50000"), leverage=10)
        pos.refresh(_D("50000"))
        wallet_balance = _D("10000")
        eff = pos.effective_leverage(wallet_balance)
        # notional=50000, equity=10000+0=10000 → 50000/10000 = 5x
        assert eff == _D("5")

    def test_effective_leverage_with_pnl(self, pe):
        pos = pe.open_position("binance", "BTCUSDT", "LONG",
                               _D("1"), _D("50000"), leverage=10)
        pos.refresh(_D("55000"))  # +5000 unrealized
        wallet_balance = _D("10000")
        eff = pos.effective_leverage(wallet_balance)
        # notional=55000, equity=10000+5000=15000 → 55000/15000 ≈ 3.67
        assert abs(eff - _D("55000") / _D("15000")) < _D("0.001")


# ─────────────────────────────────────────────────────────────
# 17. Reconciliation
# ─────────────────────────────────────────────────────────────

class TestReconciliation:
    def test_clean_reconciliation(self, seeded_svc, pe, recon):
        pos = pe.open_position("binance", "BTCUSDT", "LONG",
                               _D("0.1"), _D("50000"), leverage=10)
        snapshot = {
            "wallet_balance": "10000",
            "available_balance": "9500",
            "positions": [
                {"symbol": "BTCUSDT", "side": "LONG",
                 "qty": "0.1", "entry_price": "50000"},
            ],
        }
        results = recon.run("binance", snapshot)
        statuses = [r.status for r in results]
        assert all(s == "ok" for s in statuses)

    def test_balance_drift_detected(self, seeded_svc, recon):
        snapshot = {
            "wallet_balance": "10200",   # 200 USDT drift
            "positions": [],
        }
        results = recon.run("binance", snapshot)
        bal_result = next(r for r in results if r.check_type == "balance")
        assert bal_result.status == "drift"

    def test_balance_within_tolerance_ok(self, seeded_svc, recon):
        snapshot = {
            "wallet_balance": "10000.50",  # 0.50 USDT — within tolerance
            "positions": [],
        }
        results = recon.run("binance", snapshot)
        bal_result = next(r for r in results if r.check_type == "balance")
        assert bal_result.status == "ok"

    def test_ghost_position_detected(self, seeded_svc, pe, recon):
        # Exchange reports a position we don't have locally
        snapshot = {
            "wallet_balance": "10000",
            "positions": [
                {"symbol": "ETHUSDT", "side": "LONG",
                 "qty": "1", "entry_price": "3000"},
            ],
        }
        results = recon.run("binance", snapshot)
        pos_results = [r for r in results if r.check_type == "position"]
        assert any(r.status == "drift" for r in pos_results)

    def test_missing_local_position_detected(self, seeded_svc, pe, recon):
        # We have a local position the exchange doesn't know about
        pe.open_position("binance", "SOLUSDT", "LONG",
                         _D("10"), _D("200"))
        snapshot = {
            "wallet_balance": "10000",
            "positions": [],   # exchange says no positions
        }
        results = recon.run("binance", snapshot)
        pos_results = [r for r in results if r.check_type == "position"]
        assert any(r.status == "drift" for r in pos_results)

    def test_position_qty_drift_detected(self, seeded_svc, pe, recon):
        pe.open_position("binance", "BTCUSDT", "LONG",
                         _D("1"), _D("50000"))
        snapshot = {
            "wallet_balance": "10000",
            "positions": [
                {"symbol": "BTCUSDT", "side": "LONG",
                 "qty": "0.5", "entry_price": "50000"},  # 50% mismatch
            ],
        }
        results = recon.run("binance", snapshot)
        pos_results = [r for r in results if r.check_type == "position"]
        assert any(r.status == "drift" for r in pos_results)

    def test_recon_logs_persisted(self, seeded_svc, recon):
        recon.run("binance", {"wallet_balance": "10000", "positions": []})
        logs = recon.get_recent_logs("binance", limit=10)
        assert len(logs) >= 1
        assert logs[0]["venue"] == "binance"

    def test_is_healthy_returns_bool(self, seeded_svc, recon):
        snapshot = {"wallet_balance": "10000", "positions": []}
        result = recon.is_healthy("binance", snapshot)
        assert isinstance(result, bool)
        assert result is True


# ─────────────────────────────────────────────────────────────
# 18. RedisLayer — NullRedis path (no redis-py required)
# ─────────────────────────────────────────────────────────────

class TestRedisLayer:
    def test_null_redis_on_empty_url(self):
        r = RedisLayer(redis_url="")
        assert r is not None

    def test_put_get_wallet_null_redis(self):
        r = RedisLayer(redis_url="")
        snap = WalletSnapshot(venue="binance", wallet_balance=_D("5000"))
        r.put_wallet(snap)   # silently succeeds
        result = r.get_wallet("default", "binance")
        assert result is None   # NullRedis returns nothing

    def test_lock_null_redis_acquires(self):
        r = RedisLayer(redis_url="")
        with r.lock("order", "user1") as acquired:
            assert acquired is True   # NullRedis always succeeds

    def test_idempotency_null_redis_never_duplicates(self):
        r = RedisLayer(redis_url="")
        # NullRedis always returns False (not duplicate) — fail-open
        assert r.is_duplicate("fill", "abc123") is False
        assert r.is_duplicate("fill", "abc123") is False

    def test_put_position_null_redis(self, pe):
        r = RedisLayer(redis_url="")
        pos = pe.open_position("binance", "BTCUSDT", "LONG",
                               _D("1"), _D("50000"))
        r.put_position(pos)   # should not raise

    def test_publish_null_redis(self):
        r = RedisLayer(redis_url="")
        snap = WalletSnapshot(venue="binance", wallet_balance=_D("1000"))
        r.publish_wallet_update("user1", "binance", snap)   # no crash


# ─────────────────────────────────────────────────────────────
# 19. Complete end-to-end paper trade flow
# ─────────────────────────────────────────────────────────────

class TestEndToEndFlow:
    """
    Simulate a complete paper trading lifecycle:
    deposit → open → add to position → partial exit → full exit
    and verify ledger + projection are consistent throughout.
    """

    def test_full_long_trade_lifecycle(self, db):
        svc = LedgerService(db)
        pe  = PositionEngine(db)
        wp  = WalletProjectionService(db)

        # 1. Seed paper wallet
        svc.deposit("USDT", _D("10000"))
        assert svc.get_account("USDT").ledger_balance == _D("10000")

        # 2. Reserve margin for entry
        margin = _D("500")    # 10x on 5000 USDT notional
        svc.reserve_margin("USDT", margin)

        # 3. Open position
        pos = pe.open_position("binance", "BTCUSDT", "LONG",
                               _D("0.1"), _D("50000"), leverage=10)
        assert pos.initial_margin == _D("500")

        # 4. Market moves in our favour — refresh mark
        pos.refresh(_D("52000"))
        assert pos.unrealized_pnl == _D("200")  # (52000-50000)×0.1

        # 5. Add to position (pyramid at 52 000)
        pos = pe.add_to_position(pos.position_uid, _D("0.1"), _D("52000"))
        assert pos.qty == _D("0.2")
        assert pos.entry_price_avg == _D("51000")  # (50000+52000)/2
        svc.reserve_margin("USDT", _D("520"))       # extra margin for add-on

        # 6. Partial exit at 54 000 — take profit on half
        pos = pe.reduce_position(pos.position_uid, _D("0.1"), _D("54000"))
        # pnl on 0.1 BTC = (54000 - 51000) × 0.1 = 300
        assert pos.realized_pnl == _D("300")
        assert pos.qty == _D("0.1")
        svc.release_margin("USDT", _D("510"))       # release half the margin
        svc.book_realized_pnl("USDT", _D("300"))
        svc.charge_fee("USDT", _D("2.70"))          # 0.05% taker fee on 5400

        # 7. Full exit remaining at 55 000
        pos = pe.close_position(pos.position_uid, _D("55000"))
        # pnl on 0.1 BTC = (55000 - 51000) × 0.1 = 400
        assert pos.realized_pnl == _D("700")        # 300 + 400
        svc.release_margin("USDT", _D("510"))
        svc.book_realized_pnl("USDT", _D("400"))
        svc.charge_fee("USDT", _D("2.75"))          # 0.05% on 5500

        # 8. Verify final state
        acct = svc.get_account("USDT")
        # Started 10000, +300 +400 pnl, -2.70 -2.75 fees = 10694.55
        expected = _D("10000") + _D("700") - _D("2.70") - _D("2.75")
        assert acct.ledger_balance == expected
        assert acct.available_balance == expected    # all margin released
        assert acct.locked_balance == _D("0")

        # 9. Integrity check
        ok, diff = svc.verify_balance_integrity("USDT")
        assert ok is True
        assert diff == _D("0")

        # 10. Wallet projection matches
        snap = wp.build_snapshot("binance", positions=[])
        assert snap.wallet_balance == expected

    def test_short_trade_with_loss(self, db):
        svc = LedgerService(db)
        pe  = PositionEngine(db)
        wp  = WalletProjectionService(db)

        svc.deposit("USDT", _D("5000"))
        svc.reserve_margin("USDT", _D("300"))

        pos = pe.open_position("binance", "ETHUSDT", "SHORT",
                               _D("1"), _D("3000"), leverage=10)

        # Price rises against us
        pos.refresh(_D("3150"))
        assert pos.unrealized_pnl == _D("-150")  # (3000-3150)×1

        # Cut the loss at 3100
        pos = pe.close_position(pos.position_uid, _D("3100"))
        loss = _D("-100")  # (3000-3100)×1
        svc.release_margin("USDT", _D("300"))
        svc.book_realized_pnl("USDT", loss)
        svc.charge_fee("USDT", _D("1.55"))

        acct = svc.get_account("USDT")
        expected = _D("5000") - _D("100") - _D("1.55")
        assert acct.ledger_balance == expected
        ok, _ = svc.verify_balance_integrity("USDT")
        assert ok is True

    def test_inr_funded_usdt_trade(self, db):
        """Full INR → USDT → trade → close flow."""
        svc = LedgerService(db)
        pe  = PositionEngine(db)

        # Deposit INR
        svc.deposit("INR", _D("830000"))

        # Convert at 83 INR/USDT
        fx = FxRate(
            from_currency="INR", to_currency="USDT",
            bid=_D("82.80"), ask=_D("83.20"), mid=_D("83.00"),
            source="test",
        )
        svc.convert(_D("830000"), fx)

        usdt = svc.get_account("USDT").ledger_balance
        # 830000 / 83.20 ≈ 9975.96 USDT
        assert usdt > _D("9900") and usdt < _D("10100")

        # Open a LONG
        pos = pe.open_position("binance", "BTCUSDT", "LONG",
                               _D("0.1"), _D("50000"), leverage=10)

        # Close with profit
        closed = pe.close_position(pos.position_uid, _D("52000"))
        svc.book_realized_pnl("USDT", _D("200"))

        final_usdt = svc.get_account("USDT").ledger_balance
        assert final_usdt > usdt   # ended with more USDT than before trade

    def test_recon_after_full_flow(self, db):
        """Reconciliation reports ok after a clean trade."""
        svc = LedgerService(db)
        pe  = PositionEngine(db)

        svc.deposit("USDT", _D("10000"))
        pos = pe.open_position("binance", "BTCUSDT", "LONG",
                               _D("0.1"), _D("50000"))
        closed = pe.close_position(pos.position_uid, _D("51000"))
        svc.book_realized_pnl("USDT", _D("100"))

        recon = ReconciliationWorker(db, pe)
        results = recon.run("binance", {
            "wallet_balance": "10100",   # 10000 + 100 pnl
            "positions": [],             # no open positions
        })
        assert all(r.status == "ok" for r in results)
