import asyncio
import threading
import logging
import os
from typing import Optional, Dict, Any
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler
from .events import (
    EventBus, Event, TradeOpenedEvent, TradeClosedEvent, RiskHaltEvent,
    SystemFailureEvent, CommandEvent, SignalRejectedEvent, RegimeChangeEvent
)
from datetime import datetime, timezone

logger = logging.getLogger("telegram_bot")

# Suppress verbose httpx logs (polling)
logging.getLogger("httpx").setLevel(logging.WARNING)

class TelegramService:
    def __init__(self, token: str, chat_id: str, event_bus: EventBus):
        self.token = token
        self.chat_id = chat_id
        self.event_bus = event_bus
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.app: Optional[ApplicationBuilder] = None
        self._thread: Optional[threading.Thread] = None

    def start(self):
        """Start the Telegram bot in a background thread with its own event loop."""
        self._thread = threading.Thread(target=self._run_event_loop, daemon=True)
        self._thread.start()
        logger.info("TelegramService thread started")

    def _run_event_loop(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        
        self.app = ApplicationBuilder().token(self.token).build()
        
        # Register Command Handlers
        self.app.add_handler(CommandHandler("start", self._cmd_start))
        self.app.add_handler(CommandHandler("status", self._cmd_status))
        self.app.add_handler(CommandHandler("kill", self._cmd_kill))
        self.app.add_handler(CommandHandler("resume", self._cmd_resume))
        self.app.add_handler(CommandHandler("pnl", self._cmd_pnl))

        # Subscribe to EventBus
        self.event_bus.subscribe(TradeOpenedEvent, self._handle_event)
        self.event_bus.subscribe(TradeClosedEvent, self._handle_event)
        self.event_bus.subscribe(RiskHaltEvent, self._handle_event)
        self.event_bus.subscribe(SystemFailureEvent, self._handle_event)
        self.event_bus.subscribe(SignalRejectedEvent, self._handle_event)
        self.event_bus.subscribe(RegimeChangeEvent, self._handle_event)

        logger.info("Telegram Bot initialized and subscribed to EventBus")
        self.app.run_polling(close_loop=False, stop_signals=False)

    def _handle_event(self, event: Event):
        """Bridge sync EventBus to async Telegram broadcasting."""
        if self.loop and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(self._broadcast_event(event), self.loop)

    async def _broadcast_event(self, event: Event):
        message = self._format_event(event)
        if message:
            try:
                await self.app.bot.send_message(
                    chat_id=self.chat_id, 
                    text=message, 
                    parse_mode="Markdown"
                )
            except Exception as e:
                logger.error(f"Failed to send Telegram message: {e}")

    def _format_event(self, event: Event) -> Optional[str]:
        if isinstance(event, TradeOpenedEvent):
            return (
                f"🟢 *LONG OPENED* — {event.symbol}\n\n"
                if "LONG" in event.side.upper() else
                f"🟢 *SHORT OPENED* — {event.symbol}\n\n"
                f"Entry: {event.entry_price:.2f}\n"
                f"SL: {event.stop_loss:.2f}\n"
                f"TP: {event.take_profit:.2f}\n\n"
                f"Leverage: {event.leverage}x\n"
                f"Margin: ${event.margin:.2f}\n"
                f"Notional: ${event.notional:.2f}\n\n"
                f"Regime: {event.regime}\n"
                f"Tech Score: {event.tech_score:.2f}\n"
                f"LLM: {event.llm_decision}\n\n"
                f"Funding: {event.funding:+.4%}\n"
                f"OI Δ: {event.oi_delta:+.2%}\n\n"
                f"Liq Price: {event.liquidation_price:.2f}"
            )
        
        if isinstance(event, TradeClosedEvent):
            emoji = "🔴" if event.realized_pnl < 0 else "🟢"
            return (
                f"{emoji} *POSITION CLOSED* — {event.symbol}\n\n"
                f"PnL: ${event.realized_pnl:+.2f} ({event.pnl_percent:+.2f}%)\n"
                f"Reason: {event.reason}\n\n"
                f"Held: {event.duration_minutes:.1f}m\n"
                f"MFE: {event.mfe:+.2%}\n"
                f"MAE: {event.mae:+.2%}\n\n"
                f"Fees: ${event.fees:.2f}\n"
                f"Slippage: {event.slippage:.3f}%"
            )

        if isinstance(event, RiskHaltEvent):
            return (
                f"🛑 *TRADING HALTED*\n\n"
                f"Reason: {event.reason}\n"
                f"Daily PnL: {event.daily_pnl:+.2f} USDT\n"
                f"Trades today: {event.trades_today}\n\n"
                f"Cooldown until next UTC session."
            )

        if isinstance(event, SystemFailureEvent):
            return (
                f"⚠️ *SYSTEM FAILURE* — {event.component}\n\n"
                f"Error: `{event.error}`\n"
                f"Severity: {event.severity}"
            )

        if isinstance(event, SignalRejectedEvent):
            return (
                f"⚠️ *SIGNAL REJECTED* — {event.symbol}\n\n"
                f"Reason: {event.reason}\n"
                f"Tech Score: {event.tech_score:.2f}\n"
                f"Final Score: {event.final_score:.2f}\n"
                f"Threshold: {event.threshold:.2f}"
            )

        if isinstance(event, RegimeChangeEvent):
            return (
                f"📊 *REGIME CHANGE* — {event.symbol}\n\n"
                f"Previous: {event.old_regime}\n"
                f"Current: {event.new_regime}\n\n"
                f"ATR Expansion: {event.atr_expansion:+.1%}\n"
                f"OI Spike: {event.oi_spike:+.1%}"
            )

        return None

    # ── Command Handlers ──

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("Crypto Trader Bot Active. Monitoring markets...")

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        # This will eventually need to query the engines. 
        # For now, just a heartbeat.
        self.event_bus.publish(CommandEvent(command="STATUS", params={"chat_id": update.effective_chat.id}))
        await update.message.reply_text("Status request sent to engines...")

    async def _cmd_kill(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        self.event_bus.publish(CommandEvent(command="KILL_ALL"))
        await update.message.reply_text("🛑 KILL ALL command published to EventBus.")

    async def _cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        self.event_bus.publish(CommandEvent(command="RESUME_ALL"))
        await update.message.reply_text("✅ RESUME ALL command published to EventBus.")

    async def _cmd_pnl(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        self.event_bus.publish(CommandEvent(command="PNL_REPORT", params={"chat_id": update.effective_chat.id}))
        await update.message.reply_text("Fetching PnL report...")
