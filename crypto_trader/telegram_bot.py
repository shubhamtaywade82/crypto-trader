import asyncio
import threading
import logging
import os
from typing import Optional, Dict, Any
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler
from .events import (
    EventBus, Event, TradeOpenedEvent, TradeClosedEvent, RiskHaltEvent,
    SystemFailureEvent, CommandEvent, SignalRejectedEvent,
)

logger = logging.getLogger("telegram_bot")

# Suppress verbose httpx logs (polling)
logging.getLogger("httpx").setLevel(logging.WARNING)


class TelegramService:
    def __init__(self, token: str, chat_id: str, event_bus: EventBus):
        self.token = token
        self.chat_id = str(chat_id).strip()
        self.event_bus = event_bus
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.app: Optional[Any] = None
        self._thread: Optional[threading.Thread] = None
        self._wallet_ref = None  # injected by launcher for /status and /pnl

        is_placeholder_token = not token or any(p in token.lower() for p in ["placeholder", "your_bot", "your_token"])
        is_placeholder_chat = not chat_id or any(p in str(chat_id).lower() for p in ["placeholder", "your_chat"])
        self.enabled = not (is_placeholder_token or is_placeholder_chat)
        if not self.enabled:
            logger.warning("[Telegram] Credentials missing or placeholder — running in mock mode.")

    def attach_wallet(self, wallet) -> None:
        """Optional: give the service a wallet reference for /status and /pnl."""
        self._wallet_ref = wallet

    def _is_authorized(self, update: Update) -> bool:
        """Allow only the configured chat ID."""
        return str(update.effective_chat.id) == self.chat_id

    def start(self):
        if not self.enabled:
            logger.info("[Telegram] Mock mode — logging events instead of sending.")
            for ev_type in (TradeOpenedEvent, TradeClosedEvent, RiskHaltEvent,
                            SystemFailureEvent, SignalRejectedEvent):
                self.event_bus.subscribe(ev_type, self._handle_event_disabled)
            return

        self._thread = threading.Thread(target=self._run_event_loop, daemon=True)
        self._thread.start()
        logger.info("TelegramService thread started")

    def _run_event_loop(self):
        try:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)

            self.app = ApplicationBuilder().token(self.token).build()

            self.app.add_handler(CommandHandler("start",  self._cmd_start))
            self.app.add_handler(CommandHandler("status", self._cmd_status))
            self.app.add_handler(CommandHandler("kill",   self._cmd_kill))
            self.app.add_handler(CommandHandler("resume", self._cmd_resume))
            self.app.add_handler(CommandHandler("pnl",    self._cmd_pnl))

            for ev_type in (TradeOpenedEvent, TradeClosedEvent, RiskHaltEvent,
                            SystemFailureEvent, SignalRejectedEvent):
                self.event_bus.subscribe(ev_type, self._handle_event)

            logger.info("Telegram Bot initialized and subscribed to EventBus")
            self.app.run_polling(close_loop=False, stop_signals=False)
        except Exception as e:
            logger.error("[Telegram] Failed to start: %s", e)
            self.enabled = False

    def _handle_event(self, event: Event):
        if self.enabled and self.loop and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(self._broadcast_event(event), self.loop)

    def _handle_event_disabled(self, event: Event):
        msg = self._format_event(event)
        if msg:
            clean = msg.replace("*", "").replace("`", "").strip()
            logger.info("[Telegram Mock] %s", clean)

    async def _broadcast_event(self, event: Event):
        if not self.enabled or not self.app:
            return
        message = self._format_event(event)
        if not message:
            return
        try:
            await self.app.bot.send_message(
                chat_id=self.chat_id,
                text=message,
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.error("[Telegram] Send failed: %s", e)

    def _format_event(self, event: Event) -> Optional[str]:
        def esc(val) -> str:
            return str(val).replace("_", r"\_")

        if isinstance(event, TradeOpenedEvent):
            side_label = "LONG" if "LONG" in event.side.upper() else "SHORT"
            emoji = "🟢"
            return (
                f"{emoji} *{side_label} OPENED* — {esc(event.symbol)}\n\n"
                f"Entry: `{event.entry_price:.4f}`\n"
                f"SL: `{event.stop_loss:.4f}`\n"
                f"TP: `{event.take_profit:.4f}`\n\n"
                f"Leverage: {event.leverage}x  |  Margin: ${event.margin:.2f}\n"
                f"Notional: ${event.notional:.2f}\n\n"
                f"Regime: {esc(event.regime)}\n"
                f"Tech Score: {event.tech_score:.2f}\n"
                f"LLM: {esc(event.llm_decision)}\n\n"
                f"Funding: {event.funding:+.4%}  |  OI Δ: {event.oi_delta:+.2%}\n"
                f"Liq Price: `{event.liquidation_price:.4f}`"
            )

        if isinstance(event, TradeClosedEvent):
            emoji = "🟢" if event.realized_pnl >= 0 else "🔴"
            return (
                f"{emoji} *CLOSED* — {esc(event.symbol)}\n\n"
                f"PnL: `{event.realized_pnl:+.2f} USDT` ({event.pnl_percent:+.2f}%)\n"
                f"Reason: {esc(event.reason)}\n\n"
                f"Held: {event.duration_minutes:.1f}m\n"
                f"MFE: {event.mfe:+.2%}  |  MAE: {event.mae:+.2%}\n"
                f"Fees: ${event.fees:.2f}"
                + (f"  |  TDS: ${event.tds:.2f}" if event.tds else "")
            )

        if isinstance(event, RiskHaltEvent):
            return (
                f"🛑 *TRADING HALTED*\n\n"
                f"Reason: {esc(event.reason)}\n"
                f"Daily PnL: `{event.daily_pnl:+.2f} USDT`\n"
                f"Trades today: {event.trades_today}"
            )

        if isinstance(event, SystemFailureEvent):
            return (
                f"⚠️ *SYSTEM FAILURE* — {esc(event.component)}\n\n"
                f"Error: `{event.error}`\n"
                f"Severity: {event.severity}"
            )

        if isinstance(event, SignalRejectedEvent):
            # Only alert on basis-guard rejections (noisy otherwise)
            if "basis" not in event.reason.lower():
                return None
            return (
                f"⚠️ *SIGNAL REJECTED* — {esc(event.symbol)}\n\n"
                f"Reason: {esc(event.reason)}\n"
                f"Score: {event.final_score:.2f} / {event.threshold:.2f}"
            )

        return None

    # ── Command handlers ──────────────────────────────────────────────────────

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        await update.message.reply_text("Antigravity trading bot active. Use /status /pnl /kill /resume.")

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        if self._wallet_ref is not None:
            w = self._wallet_ref
            pos_lines = []
            for sym, pos in w.positions.items():
                if pos.status == "OPEN":
                    pos_lines.append(
                        f"  {sym} {pos.side.value} @ {float(pos.entry_price):.4f} "
                        f"(upnl={float(pos.unrealized_pnl):+.2f})"
                    )
            pos_text = "\n".join(pos_lines) if pos_lines else "  none"
            msg = (
                f"*Bot Status*\n\n"
                f"Balance: `{float(w.wallet_balance):.2f} USDT`\n"
                f"Realized PnL: `{float(w.realized_pnl_total):+.2f} USDT`\n"
                f"Open positions:\n{pos_text}"
            )
        else:
            self.event_bus.publish(CommandEvent(command="STATUS", params={}))
            msg = "Status request sent — check logs."
        await update.message.reply_text(msg, parse_mode="Markdown")

    async def _cmd_pnl(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        if self._wallet_ref is not None:
            w = self._wallet_ref
            msg = (
                f"*PnL Report*\n\n"
                f"Realized: `{float(w.realized_pnl_total):+.2f} USDT`\n"
                f"Unrealized: `{float(w.unrealized_pnl_total):+.2f} USDT`\n"
                f"Balance: `{float(w.wallet_balance):.2f} USDT`\n"
                f"Margin used: `{float(w.total_margin_used):.2f} USDT`"
            )
        else:
            self.event_bus.publish(CommandEvent(command="PNL_REPORT", params={}))
            msg = "PnL request sent — check logs."
        await update.message.reply_text(msg, parse_mode="Markdown")

    async def _cmd_kill(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            await update.message.reply_text("Unauthorized.")
            return
        self.event_bus.publish(CommandEvent(command="KILL_ALL"))
        await update.message.reply_text("🛑 KILL ALL sent to all engines.")

    async def _cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            await update.message.reply_text("Unauthorized.")
            return
        self.event_bus.publish(CommandEvent(command="RESUME_ALL"))
        await update.message.reply_text("✅ RESUME ALL sent to all engines.")
