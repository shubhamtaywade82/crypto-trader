from dataclasses import dataclass, field
from typing import Dict, List, Callable, Any, Optional
import threading
import logging

logger = logging.getLogger("events")

@dataclass
class Event:
    timestamp: float = field(default_factory=lambda: __import__('time').time())

@dataclass
class TradeOpenedEvent(Event):
    symbol: str = ""
    side: str = ""
    leverage: int = 1
    entry_price: float = 0.0
    stop_loss: float = 0.0
    take_profit: float = 0.0
    margin: float = 0.0
    notional: float = 0.0
    regime: str = ""
    tech_score: float = 0.0
    llm_decision: str = ""
    funding: float = 0.0
    oi_delta: float = 0.0
    liquidation_price: float = 0.0
    wallet_balance: float = 0.0

@dataclass
class TradeClosedEvent(Event):
    symbol: str = ""
    side: str = ""
    realized_pnl: float = 0.0
    pnl_percent: float = 0.0
    exit_price: float = 0.0
    reason: str = ""
    duration_minutes: float = 0.0
    mfe: float = 0.0
    mae: float = 0.0
    fees: float = 0.0
    slippage: float = 0.0
    tds: float = 0.0
    wallet_balance: float = 0.0

@dataclass
class RiskHaltEvent(Event):
    reason: str = ""
    daily_pnl: float = 0.0
    trades_today: int = 0
    cooldown_until: Optional[float] = None

@dataclass
class SystemFailureEvent(Event):
    component: str = ""
    error: str = ""
    severity: str = "CRITICAL"

@dataclass
class SignalRejectedEvent(Event):
    symbol: str = ""
    reason: str = ""
    tech_score: float = 0.0
    final_score: float = 0.0
    threshold: float = 0.0

@dataclass
class RegimeChangeEvent(Event):
    symbol: str = ""
    old_regime: str = ""
    new_regime: str = ""
    atr_expansion: float = 0.0
    oi_spike: float = 0.0

@dataclass
class CommandEvent(Event):
    command: str = ""
    params: Dict[str, Any] = field(default_factory=dict)
    origin: str = "telegram"

@dataclass
class OrderSubmittedEvent(Event):
    symbol: str = ""
    side: str = ""
    client_order_id: str = ""
    exchange_order_id: str = ""
    order_type: str = ""
    quantity: float = 0.0
    reduce_only: bool = False
    venue: str = "coindcx"

@dataclass
class OrderFilledEvent(Event):
    symbol: str = ""
    side: str = ""
    client_order_id: str = ""
    exchange_order_id: str = ""
    fill_price: float = 0.0
    fill_quantity: float = 0.0
    fee: float = 0.0
    partial: bool = False
    venue: str = "coindcx"

@dataclass
class ReconciliationMismatchEvent(Event):
    symbol: str = ""
    kind: str = ""          # e.g. "position_qty", "ghost_position", "orphan_order", "balance"
    internal: str = ""
    exchange: str = ""
    repaired: bool = False
    detail: str = ""

@dataclass
class KillSwitchTriggeredEvent(Event):
    reason: str = ""
    source: str = ""        # e.g. "reconciliation", "feed_stale", "manual"

@dataclass
class FeedStaleEvent(Event):
    source: str = ""        # "binance" | "coindcx" | "all"
    age_ms: float = 0.0
    trading_disabled: bool = False

@dataclass
class SMCAlertEvent(Event):
    """Published when a new SMC structure is detected on the 15m timeframe.

    The SMC alert processor subscribes to these events, forwards the
    structured context to the LLM, and broadcasts the LLM commentary to
    Telegram.
    """
    symbol: str = ""
    alert_type: str = ""       # "sweep" | "bos" | "choch" | "fvg" | "ob" | "pinbar" | "engulfing"
    direction: str = ""        # "bullish" | "bearish"
    price: float = 0.0
    details: str = ""          # human-readable summary
    timeframe: str = "15m"
    htf_trend: str = ""        # higher-timeframe bias context
    mark_price: float = 0.0

class EventBus:
    def __init__(self):
        self._subscribers: Dict[type, List[Callable]] = {}
        self._lock = threading.Lock()

    def subscribe(self, event_type: type, callback: Callable):
        with self._lock:
            if event_type not in self._subscribers:
                self._subscribers[event_type] = []
            if callback not in self._subscribers[event_type]:
                self._subscribers[event_type].append(callback)
                logger.debug(f"Subscribed {callback} to {event_type.__name__}")

    def publish(self, event: Event):
        event_type = type(event)
        with self._lock:
            subscribers = self._subscribers.get(event_type, []).copy()
            # Also notify generic Event subscribers
            if Event in self._subscribers and event_type != Event:
                subscribers.extend(self._subscribers[Event])

        for callback in subscribers:
            try:
                callback(event)
            except Exception as e:
                logger.error(f"Error in event callback {callback} for {event_type.__name__}: {e}")

# Global EventBus instance for convenience, 
# though dependency injection is preferred for testing.
bus = EventBus()
