"""
crypto_trader.strategies.mr_state — Per-Symbol Mean-Reversion State Persistence
================================================================================
Implements PRD §11.5 (State Manager) and §18 (Restart Recovery).

Persists the minimum state needed to survive a crash/restart without losing
position awareness or stop-loss protection:

    • last_processed_candle_ts  — duplicate-candle guard survives restarts
    • last_entry_side           — "long" / "short" / None
    • last_entry_price          — float / None
    • last_entry_qty            — float / None
    • stop_order_id             — venue stop-market order ID (live mode)
    • consecutive_losses        — per-symbol loss streak
    • daily_trade_count         — daily trade counter per symbol
    • daily_date                — ISO date string for reset logic

One JSON file per symbol, atomically written on every state change.
Stored under ``~/.crypto_trader/mr_state_<SYMBOL>.json``.

The state is intentionally minimal — the canonical position/PnL ledger
lives in EnhancedFuturesWallet / event store.  This file only carries the
MR-specific restart-recovery fields the wallet doesn't own.

PRD §18 startup reconciliation sequence:
    Step 1: Load this state.
    Step 2: Fetch live wallet position (via EnhancedFuturesWallet).
    Step 3: If position open but no stop_order_id → recreate stop-loss.
    Step 4: Resume processing from last candle timestamp.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("crypto_trader.strategies.mr_state")

_DATA_DIR = Path.home() / ".crypto_trader"
_DATA_DIR.mkdir(exist_ok=True)


# ─── Atomic write helper (same pattern as RiskManager) ───────────────────────

def _atomic_write(path: Path, data: dict) -> None:
    """Write JSON atomically via temp-file + fsync + rename."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ─── State dataclass ─────────────────────────────────────────────────────────

@dataclass
class MRSymbolState:
    """
    Persisted MR state for one symbol.

    Fields
    ------
    symbol                  : str   — e.g. "ETHUSDT"
    last_processed_candle_ts: int   — Unix ms timestamp of last processed candle
    last_entry_side         : str   — "long", "short", or ""
    last_entry_price        : float — fill price of the open position
    last_entry_qty          : float — position quantity
    stop_order_id           : str   — venue stop-market order ID (live mode only)
    consecutive_losses      : int   — per-symbol loss streak counter
    daily_trade_count       : int   — trades opened today (resets at UTC midnight)
    daily_date              : str   — ISO date of last daily reset ("YYYY-MM-DD")
    daily_pnl               : float — realized PnL for the current UTC day
    """
    symbol: str = ""
    last_processed_candle_ts: int = 0
    last_entry_side: str = ""          # "long" | "short" | ""
    last_entry_price: float = 0.0
    last_entry_qty: float = 0.0
    stop_order_id: str = ""
    consecutive_losses: int = 0
    daily_trade_count: int = 0
    daily_date: str = ""
    daily_pnl: float = 0.0

    @property
    def has_open_entry(self) -> bool:
        return bool(self.last_entry_side)


# ─── Manager ─────────────────────────────────────────────────────────────────

class MRStateManager:
    """
    Loads, saves, and mutates per-symbol MR state.

    Typical usage
    -------------
    ::

        mgr = MRStateManager("ETHUSDT")
        state = mgr.load()
        # ... process candle ...
        mgr.mark_candle_processed(candle_ts_ms)
        mgr.record_entry(side="long", price=2510.0, qty=0.05)

    Restart-recovery helper::

        mgr.reconcile_on_startup(wallet_position)
    """

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol.upper()
        self._path = _DATA_DIR / f"mr_state_{self.symbol}.json"
        self._state: Optional[MRSymbolState] = None

    # ── I/O ──────────────────────────────────────────────────────────────────

    def load(self) -> MRSymbolState:
        """Load state from disk. Returns a fresh state if file is missing/corrupt."""
        if not self._path.exists():
            self._state = MRSymbolState(symbol=self.symbol)
            return self._state
        try:
            raw = json.loads(self._path.read_text())
            self._state = MRSymbolState(**{k: v for k, v in raw.items() if k in MRSymbolState.__dataclass_fields__})
            self._state.symbol = self.symbol  # ensure symbol matches file name
            logger.debug("[MR-STATE] Loaded %s: side=%r candle_ts=%d",
                         self.symbol, self._state.last_entry_side, self._state.last_processed_candle_ts)
        except Exception as e:
            logger.warning("[MR-STATE] Corrupt state for %s — starting fresh: %s", self.symbol, e)
            self._state = MRSymbolState(symbol=self.symbol)
        return self._state

    def save(self) -> None:
        """Persist current state atomically."""
        if self._state is None:
            return
        try:
            _atomic_write(self._path, asdict(self._state))
        except Exception as e:
            logger.error("[MR-STATE] Failed to save state for %s: %s", self.symbol, e)

    @property
    def state(self) -> MRSymbolState:
        if self._state is None:
            self.load()
        return self._state  # type: ignore[return-value]

    # ── Candle guard ─────────────────────────────────────────────────────────

    def mark_candle_processed(self, candle_ts_ms: int) -> None:
        """Record the timestamp of the last processed candle (duplicate guard)."""
        self.state.last_processed_candle_ts = int(candle_ts_ms)
        self.save()

    def is_candle_processed(self, candle_ts_ms: int) -> bool:
        return self.state.last_processed_candle_ts == int(candle_ts_ms)

    # ── Entry / exit tracking ─────────────────────────────────────────────────

    def record_entry(
        self,
        side: str,
        price: float,
        qty: float,
        stop_order_id: str = "",
    ) -> None:
        """Called immediately after an entry fills."""
        s = self.state
        s.last_entry_side = side.lower()
        s.last_entry_price = float(price)
        s.last_entry_qty = float(qty)
        s.stop_order_id = str(stop_order_id)
        self._bump_daily_count()
        self.save()
        logger.info("[MR-STATE] %s entry recorded: side=%s price=%.4f qty=%.6f",
                    self.symbol, side, price, qty)

    def record_close(self, pnl: float) -> None:
        """Called when the position is closed (any reason)."""
        s = self.state
        s.daily_pnl += float(pnl)
        if pnl < 0:
            s.consecutive_losses += 1
            logger.warning("[MR-STATE] %s loss recorded. Streak: %d. Daily PnL: %.2f",
                           self.symbol, s.consecutive_losses, s.daily_pnl)
        else:
            s.consecutive_losses = 0
            logger.info("[MR-STATE] %s win recorded. Streak reset. Daily PnL: %.2f",
                        self.symbol, s.daily_pnl)
        # Clear position fields
        s.last_entry_side = ""
        s.last_entry_price = 0.0
        s.last_entry_qty = 0.0
        s.stop_order_id = ""
        self.save()

    def update_stop_order_id(self, stop_order_id: str) -> None:
        """Update the venue stop-market order ID after placement/recreation."""
        self.state.stop_order_id = str(stop_order_id)
        self.save()

    def clear_entry(self) -> None:
        """Clear position fields (e.g. after manual reconciliation confirms flat)."""
        s = self.state
        s.last_entry_side = ""
        s.last_entry_price = 0.0
        s.last_entry_qty = 0.0
        s.stop_order_id = ""
        self.save()

    # ── Daily counters ────────────────────────────────────────────────────────

    def _today_iso(self) -> str:
        return datetime.now(timezone.utc).date().isoformat()

    def _bump_daily_count(self) -> None:
        today = self._today_iso()
        s = self.state
        if s.daily_date != today:
            s.daily_trade_count = 0
            s.daily_pnl = 0.0
            s.daily_date = today
            logger.debug("[MR-STATE] %s: daily counters reset for %s", self.symbol, today)
        s.daily_trade_count += 1

    def reset_daily_if_new_day(self) -> bool:
        """Return True if daily counters were reset (new UTC day)."""
        today = self._today_iso()
        if self.state.daily_date != today:
            self.state.daily_date = today
            self.state.daily_trade_count = 0
            self.state.daily_pnl = 0.0
            self.save()
            return True
        return False

    # ── Restart reconciliation (PRD §18) ─────────────────────────────────────

    def reconcile_on_startup(self, wallet_position) -> str:
        """
        PRD §18 Steps 1-4: Reconcile persisted MR state vs live wallet position.

        Parameters
        ----------
        wallet_position : EnhancedPosition | None
            The live open position for this symbol from the wallet.
            Pass ``None`` if the wallet reports no open position.

        Returns
        -------
        str — human-readable reconciliation outcome for logging.
        """
        s = self.load()
        flat = wallet_position is None or getattr(wallet_position, "status", "") != "OPEN"

        if flat and s.has_open_entry:
            # Wallet is flat but state thinks we have a position.
            # Position was closed externally (exchange SL hit, manual close, etc.)
            logger.warning(
                "[MR-STATE] %s: state has open %s entry but wallet is flat "
                "— clearing stale state.",
                self.symbol, s.last_entry_side,
            )
            self.clear_entry()
            return f"{self.symbol}: stale MR state cleared (wallet is flat)"

        if not flat and not s.has_open_entry:
            # Wallet has a position but MR state lost it (crash before save).
            # Rebuild partial state from wallet position.
            try:
                side = wallet_position.side.value.lower()   # "long" / "short"
                price = float(wallet_position.entry_price)
                qty = float(wallet_position.quantity)
                s.last_entry_side = side
                s.last_entry_price = price
                s.last_entry_qty = qty
                self.save()
                logger.warning(
                    "[MR-STATE] %s: wallet has open %s @ %.4f but MR state was empty "
                    "— rebuilt from wallet.",
                    self.symbol, side, price,
                )
                return (
                    f"{self.symbol}: MR state rebuilt from wallet "
                    f"(side={side} price={price:.4f} qty={qty:.6f})"
                )
            except Exception as e:
                logger.error("[MR-STATE] Reconcile rebuild failed for %s: %s", self.symbol, e)
                return f"{self.symbol}: reconcile rebuild FAILED: {e}"

        if not flat and s.has_open_entry:
            # Both agree. Log the consistency check.
            logger.info(
                "[MR-STATE] %s: consistent — open %s @ %.4f. stop_order=%r",
                self.symbol, s.last_entry_side, s.last_entry_price,
                s.stop_order_id or "(none)",
            )
            return (
                f"{self.symbol}: consistent — open {s.last_entry_side} "
                f"@ {s.last_entry_price:.4f}"
            )

        # Both flat — clean state.
        return f"{self.symbol}: flat — no reconciliation needed"

    def missing_stop_order(self) -> bool:
        """True when we hold a position but have no recorded stop order."""
        s = self.state
        return s.has_open_entry and not s.stop_order_id
