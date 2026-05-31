"""
crypto_trader.position_management.sync_service
===============================================
Position Synchronization Layer — discovers ALL open positions on CoinDCX
Futures (manual, bot-opened, externally imported) and normalises them into
the local wallet state.

This is the "portfolio observer" component.  It does NOT make trading decisions;
it only keeps the local state consistent with what actually exists on the exchange.

Architecture:
  REST bootstrap  → loads the full snapshot on startup / reconnect
  WS live stream  → position_update, order_update, balance_update events
  Reconciler      → detects drift, triggers REST resync

Position sources:
  "bot"      → opened by this crypto-trader process (position exists in wallet already)
  "manual"   → opened externally; not in local wallet
  "imported" → adopted from a previous session that is now reloaded
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from enum import Enum
from typing import Callable, Dict, List, Optional

logger = logging.getLogger("crypto_trader.position_management.sync_service")


class PositionSource(str, Enum):
    BOT      = "bot"
    MANUAL   = "manual"
    IMPORTED = "imported"


class PositionSyncState(str, Enum):
    NEW                = "new"
    SYNCED             = "synced"
    PROTECTED          = "protected"       # SL/TP orders placed
    ACTIVELY_MANAGED   = "actively_managed"
    REDUCING           = "reducing"
    EXITING            = "exiting"
    CLOSED             = "closed"
    STALE              = "stale"
    RECONCILE_REQUIRED = "reconcile_required"


class ExternalPosition:
    """Canonical representation of a CoinDCX futures position."""

    def __init__(
        self,
        exchange_id: str,
        symbol: str,
        side: str,           # "LONG" | "SHORT"
        qty: float,
        entry_price: float,
        leverage: int,
        ltp: float = 0.0,
        unrealized_pnl: float = 0.0,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        source: PositionSource = PositionSource.MANUAL,
        sync_state: PositionSyncState = PositionSyncState.NEW,
        margin_used: float = 0.0,
        raw: Optional[dict] = None,
    ):
        self.exchange_id = exchange_id
        self.symbol = symbol
        self.side = side.upper()
        self.qty = qty
        self.entry_price = entry_price
        self.leverage = leverage
        self.ltp = ltp
        self.unrealized_pnl = unrealized_pnl
        self.stop_loss = stop_loss
        self.take_profit = take_profit
        self.source = source
        self.sync_state = sync_state
        self.margin_used = margin_used
        self.raw = raw or {}
        self.local_id = str(uuid.uuid4())
        self.first_seen_ms = int(time.time() * 1000)
        self.last_synced_ms = self.first_seen_ms

    @property
    def has_stop_loss(self) -> bool:
        return self.stop_loss is not None and self.stop_loss > 0

    @property
    def has_take_profit(self) -> bool:
        return self.take_profit is not None and self.take_profit > 0

    def to_dict(self) -> dict:
        return {
            "exchange_id":    self.exchange_id,
            "local_id":       self.local_id,
            "symbol":         self.symbol,
            "side":           self.side,
            "qty":            self.qty,
            "entry_price":    self.entry_price,
            "leverage":       self.leverage,
            "ltp":            self.ltp,
            "unrealized_pnl": self.unrealized_pnl,
            "stop_loss":      self.stop_loss,
            "take_profit":    self.take_profit,
            "source":         self.source.value,
            "sync_state":     self.sync_state.value,
            "margin_used":    self.margin_used,
            "first_seen_ms":  self.first_seen_ms,
            "last_synced_ms": self.last_synced_ms,
        }


class PositionSyncService:
    """
    Maintains a live registry of all open positions on the exchange.

    Responsibilities:
      1. Bootstrap from REST on start
      2. Stream live updates via WS callbacks
      3. Detect new external positions (manual trades)
      4. Trigger protective action (SL/TP setup) for unprotected positions
      5. Reconcile periodically against REST

    Callbacks are used to notify the management layer of events:
      on_new_position(ExternalPosition)   → new position detected
      on_position_update(ExternalPosition) → price / PnL / size changed
      on_position_closed(str)             → position exchange_id closed
      on_protection_required(ExternalPosition) → SL or TP missing
    """

    def __init__(
        self,
        rest_client=None,          # CoinDCX REST client (coindcx_client)
        ws_client=None,            # CoinDCX WS user stream
        wallet=None,               # EnhancedFuturesWallet (for known bot positions)
        reconcile_interval_s: int = 30,
        on_new_position: Optional[Callable[[ExternalPosition], None]] = None,
        on_position_update: Optional[Callable[[ExternalPosition], None]] = None,
        on_position_closed: Optional[Callable[[str], None]] = None,
        on_protection_required: Optional[Callable[[ExternalPosition], None]] = None,
    ):
        self.rest = rest_client
        self.ws = ws_client
        self.wallet = wallet
        self.reconcile_interval_s = reconcile_interval_s

        self.on_new_position        = on_new_position
        self.on_position_update     = on_position_update
        self.on_position_closed     = on_position_closed
        self.on_protection_required = on_protection_required

        # exchange_id → ExternalPosition
        self._positions: Dict[str, ExternalPosition] = {}
        self._lock = threading.RLock()
        self._running = False
        self._reconcile_thread: Optional[threading.Thread] = None

    # ── Lifecycle ────────────────────────────────────────────────────────

    def start(self):
        """Bootstrap from REST then start reconciliation loop."""
        self._bootstrap_rest()
        self._running = True
        self._reconcile_thread = threading.Thread(
            target=self._reconcile_loop, daemon=True, name="pm-reconcile"
        )
        self._reconcile_thread.start()
        logger.info("[SyncService] Started with %d positions", len(self._positions))

    def stop(self):
        self._running = False

    # ── REST bootstrap ────────────────────────────────────────────────────

    def _bootstrap_rest(self):
        if self.rest is None:
            logger.warning("[SyncService] No REST client — bootstrap skipped")
            return
        try:
            raw_positions = self._fetch_positions_rest()
            for raw in raw_positions:
                pos = self._parse_rest_position(raw)
                if pos is None:
                    continue
                with self._lock:
                    self._upsert(pos, trigger="bootstrap")
            logger.info("[SyncService] Bootstrapped %d positions from REST", len(self._positions))
        except Exception as exc:
            logger.error("[SyncService] REST bootstrap failed: %s", exc, exc_info=True)

    def _fetch_positions_rest(self) -> List[dict]:
        """Fetch open positions from CoinDCX REST API."""
        try:
            # CoinDCX futures: GET /exchange/v1/derivatives/futures/positions
            if hasattr(self.rest, "get_futures_positions"):
                return self.rest.get_futures_positions() or []
            if hasattr(self.rest, "get_positions"):
                return self.rest.get_positions() or []
        except Exception as exc:
            logger.warning("[SyncService] REST fetch_positions failed: %s", exc)
        return []

    def _parse_rest_position(self, raw: dict) -> Optional[ExternalPosition]:
        """Parse a CoinDCX REST position response into ExternalPosition."""
        try:
            # CoinDCX field names (adjust if the actual API differs):
            # id / positionId, symbol / pair, positionSide, positionAmt, entryPrice,
            # leverage, unRealizedProfit, stopLossPrice, takeProfitPrice, marginType
            exchange_id = str(
                raw.get("id") or raw.get("positionId") or raw.get("position_id", "")
            )
            symbol = str(raw.get("symbol") or raw.get("pair", ""))
            side_raw = str(raw.get("positionSide") or raw.get("side", "")).upper()
            side = "LONG" if side_raw in ("LONG", "BUY") else "SHORT"
            qty = float(raw.get("positionAmt") or raw.get("quantity") or raw.get("qty", 0))
            entry = float(raw.get("entryPrice") or raw.get("entry_price", 0))
            leverage = int(raw.get("leverage", 1))
            ltp = float(raw.get("markPrice") or raw.get("ltp") or raw.get("mark_price", entry))
            upnl = float(raw.get("unRealizedProfit") or raw.get("unrealized_pnl", 0))
            sl = raw.get("stopLossPrice") or raw.get("stop_loss_price") or raw.get("stop_loss")
            tp = raw.get("takeProfitPrice") or raw.get("take_profit_price") or raw.get("take_profit")
            margin = float(raw.get("initialMargin") or raw.get("margin_used") or raw.get("margin", 0))

            source = self._classify_source(exchange_id, symbol, side, qty)

            return ExternalPosition(
                exchange_id=exchange_id,
                symbol=symbol,
                side=side,
                qty=qty,
                entry_price=entry,
                leverage=leverage,
                ltp=ltp,
                unrealized_pnl=upnl,
                stop_loss=float(sl) if sl else None,
                take_profit=float(tp) if tp else None,
                source=source,
                sync_state=PositionSyncState.SYNCED,
                margin_used=margin,
                raw=raw,
            )
        except Exception as exc:
            logger.warning("[SyncService] Failed to parse REST position: %s | raw=%s", exc, raw)
            return None

    # ── WS event handlers (call these from the WS listener) ──────────────

    def on_ws_position_update(self, data: dict):
        """Called by WebSocket listener on position update events."""
        try:
            pos = self._parse_rest_position(data)
            if pos is None:
                return
            qty = pos.qty
            exchange_id = pos.exchange_id

            with self._lock:
                if qty <= 0:
                    # Position fully closed on exchange
                    existed = exchange_id in self._positions
                    if existed:
                        del self._positions[exchange_id]
                        logger.info("[SyncService] Position closed (WS): %s", exchange_id)
                    if existed and self.on_position_closed:
                        self.on_position_closed(exchange_id)
                    return
                self._upsert(pos, trigger="ws")
        except Exception as exc:
            logger.error("[SyncService] WS position update error: %s", exc)

    def on_ws_balance_update(self, data: dict):
        """Called by WebSocket listener on balance/margin update events."""
        # Pass to wallet for margin tracking if wallet is wired
        pass

    def on_ws_ltp_update(self, symbol: str, ltp: float):
        """Update LTP for all positions on this symbol."""
        with self._lock:
            for pos in self._positions.values():
                if pos.symbol == symbol:
                    pos.ltp = ltp
                    if pos.entry_price > 0:
                        qty = pos.qty
                        if pos.side == "LONG":
                            pos.unrealized_pnl = (ltp - pos.entry_price) * qty
                        else:
                            pos.unrealized_pnl = (pos.entry_price - ltp) * qty

    # ── Reconciliation ────────────────────────────────────────────────────

    def _reconcile_loop(self):
        while self._running:
            time.sleep(self.reconcile_interval_s)
            try:
                self._reconcile_once()
            except Exception as exc:
                logger.error("[SyncService] Reconcile error: %s", exc)

    def _reconcile_once(self):
        raw_positions = self._fetch_positions_rest()
        exchange_ids_live = set()
        for raw in raw_positions:
            pos = self._parse_rest_position(raw)
            if pos is None:
                continue
            exchange_ids_live.add(pos.exchange_id)
            with self._lock:
                self._upsert(pos, trigger="reconcile")

        # Detect locally-tracked positions that are no longer on exchange
        with self._lock:
            stale_ids = [eid for eid in self._positions if eid not in exchange_ids_live]
        for eid in stale_ids:
            logger.info("[SyncService] Position no longer on exchange, removing: %s", eid)
            with self._lock:
                self._positions.pop(eid, None)
            if self.on_position_closed:
                self.on_position_closed(eid)

    # ── Upsert logic ──────────────────────────────────────────────────────

    def _upsert(self, incoming: ExternalPosition, trigger: str):
        """Insert or update, firing appropriate callbacks."""
        eid = incoming.exchange_id
        existing = self._positions.get(eid)
        incoming.last_synced_ms = int(time.time() * 1000)

        if existing is None:
            # New position
            self._positions[eid] = incoming
            logger.info(
                "[SyncService] NEW position detected [%s]: %s %s qty=%.4f source=%s",
                trigger, incoming.symbol, incoming.side, incoming.qty, incoming.source,
            )
            if self.on_new_position:
                self.on_new_position(incoming)
            if not incoming.has_stop_loss or not incoming.has_take_profit:
                if self.on_protection_required:
                    self.on_protection_required(incoming)
        else:
            # Update existing
            existing.qty = incoming.qty
            existing.ltp = incoming.ltp
            existing.unrealized_pnl = incoming.unrealized_pnl
            existing.stop_loss = incoming.stop_loss
            existing.take_profit = incoming.take_profit
            existing.margin_used = incoming.margin_used
            existing.last_synced_ms = incoming.last_synced_ms
            if existing.sync_state == PositionSyncState.NEW:
                existing.sync_state = PositionSyncState.SYNCED
            if self.on_position_update:
                self.on_position_update(existing)

    def _classify_source(
        self, exchange_id: str, symbol: str, side: str, qty: float
    ) -> PositionSource:
        """Check if this position is already tracked in the local wallet."""
        if self.wallet is None:
            return PositionSource.MANUAL
        with self.wallet.lock:
            local_pos = self.wallet.positions.get(symbol)
            if local_pos and local_pos.status == "OPEN":
                return PositionSource.BOT
        return PositionSource.MANUAL

    # ── Accessors ─────────────────────────────────────────────────────────

    def get_all(self) -> List[ExternalPosition]:
        with self._lock:
            return list(self._positions.values())

    def get(self, exchange_id: str) -> Optional[ExternalPosition]:
        with self._lock:
            return self._positions.get(exchange_id)

    def get_by_symbol(self, symbol: str) -> Optional[ExternalPosition]:
        with self._lock:
            for pos in self._positions.values():
                if pos.symbol == symbol:
                    return pos
        return None

    def count(self) -> int:
        with self._lock:
            return len(self._positions)

    def mark_protected(self, exchange_id: str):
        with self._lock:
            pos = self._positions.get(exchange_id)
            if pos:
                pos.sync_state = PositionSyncState.PROTECTED

    def mark_actively_managed(self, exchange_id: str):
        with self._lock:
            pos = self._positions.get(exchange_id)
            if pos:
                pos.sync_state = PositionSyncState.ACTIVELY_MANAGED
