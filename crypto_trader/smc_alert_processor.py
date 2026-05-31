"""SMC Alert Processor — event-driven LLM commentary for Smart Money Concepts.

Subscribes to :class:`~crypto_trader.events.SMCAlertEvent`, builds a concise
SMC-focused prompt, routes it through the configured LLM (Ollama / OpenAI),
and broadcasts the commentary back to Telegram.
"""
import logging
import os
import time
from typing import Optional

from .events import EventBus, SMCAlertEvent
from .llm_advisor import OllamaAdvisor

logger = logging.getLogger("smc_alert_processor")


SMC_COMMENTARY_SYSTEM = """You are a crypto futures market-structure commentator specializing in Smart Money Concepts (SMC).
You receive a single SMC event (sweep, BOS, CHoCH, FVG, OB, or Price-Action pattern) and provide a short, actionable commentary.

Rules:
- Be concise (2-4 sentences max).
- Explain WHAT happened and WHAT it implies for near-term price action.
- Mention key liquidity levels or magnet zones if relevant.
- Do NOT give trade instructions (entry/SL/TP). The bot handles that.
- Tone: clinical, quantitative, no hype.

Output plain text only. No markdown, no JSON."""


def _build_smc_prompt(event: SMCAlertEvent) -> str:
    """Build a focused prompt from an SMC alert event."""
    lines = [
        f"Symbol: {event.symbol}",
        f"Timeframe: {event.timeframe}",
        f"HTF Bias: {event.htf_trend or 'unknown'}",
        f"Mark Price: {event.mark_price}",
        f"",
        f"SMC Event: {event.alert_type.upper()} | Direction: {event.direction.upper()}",
        f"Price Level: {event.price}",
        f"Details: {event.details}",
        f"",
        f"Provide a short commentary on what this SMC structure implies.",
    ]
    return "\n".join(lines)


class SMCAlertProcessor:
    """Wires SMCAlertEvent → LLM → Telegram.

    Usage::

        processor = SMCAlertProcessor(event_bus, llm_client, telegram_service)
        processor.start()   # subscribes to the bus
    """

    def __init__(
        self,
        event_bus: EventBus,
        llm_client: Optional[OllamaAdvisor] = None,
        telegram_service = None,
    ):
        self.bus = event_bus
        self.tg = telegram_service
        self._subscribed = False
        # Simple in-memory dedup: (symbol, alert_type, direction) → last timestamp
        self._last_alert_ts: dict = {}
        self._cooldown_seconds = 900  # 15 min cooldown per (sym, type, dir)

        # Build LLM client if not provided
        if llm_client is not None:
            self.llm = llm_client
        else:
            try:
                from .llm_advisor import build_advisor
                use_llm = os.getenv("USE_LLM", "true").lower() not in ("false", "0", "no")
                self.llm = build_advisor(
                    use_llm=use_llm,
                    llm_host=os.getenv("LLM_HOST"),
                    llm_model=os.getenv("LLM_MODEL"),
                    llm_logged=os.getenv("LOG_LLM", "").lower() in ("true", "1", "yes"),
                )
            except Exception as e:
                logger.warning("[SMC-ALERT] Failed to build LLM client: %s", e)
                self.llm = None

    def start(self):
        if self._subscribed:
            return
        self.bus.subscribe(SMCAlertEvent, self._on_smc_alert)
        self._subscribed = True
        logger.info("[SMC-ALERT] Processor started — listening for SMCAlertEvent")

    def _on_smc_alert(self, event: SMCAlertEvent):
        if self.llm is None:
            logger.debug("[SMC-ALERT] No LLM client — skipping %s", event.symbol)
            return

        # Deduplication: don't spam the same alert within cooldown
        key = (event.symbol, event.alert_type, event.direction)
        now = time.time()
        last = self._last_alert_ts.get(key, 0)
        if now - last < self._cooldown_seconds:
            logger.debug("[SMC-ALERT] %s %s %s suppressed (cooldown)", event.symbol, event.alert_type, event.direction)
            return
        self._last_alert_ts[key] = now

        # Build prompt and ask LLM
        prompt = _build_smc_prompt(event)
        logger.info("[SMC-ALERT] %s %s → querying LLM", event.symbol, event.alert_type)

        if self.llm is None:
            logger.warning("[SMC-ALERT] No LLM client available — skipping commentary")
            return
        if not hasattr(self.llm, "generate"):
            logger.error("[SMC-ALERT] LLM client %s has no 'generate' method — skipping commentary", type(self.llm).__name__)
            return

        try:
            commentary = self.llm.generate(prompt, temperature=0.3, system_prompt=SMC_COMMENTARY_SYSTEM)
        except Exception as e:
            logger.error("[SMC-ALERT] LLM call failed: %s", e)
            return

        if not commentary:
            logger.warning("[SMC-ALERT] LLM returned empty commentary for %s", event.symbol)
            return

        logger.info("[SMC-ALERT] %s commentary received (%d chars)", event.symbol, len(commentary))
        self._send_to_telegram(event, commentary)

    def _send_to_telegram(self, event: SMCAlertEvent, commentary: str):
        if self.tg is None or not getattr(self.tg, "enabled", False):
            logger.debug("[SMC-ALERT] Telegram disabled — logging locally")
            logger.info("[SMC-ALERT] %s %s | %s", event.symbol, event.alert_type, commentary[:200])
            return

        emoji_map = {
            "sweep": "🌊",
            "bos": "📈",
            "choch": "🔄",
            "fvg": "🧲",
            "ob": "📦",
            "pinbar": "📍",
            "engulfing": "🌀",
        }
        emoji = emoji_map.get(event.alert_type.lower(), "📊")
        direction_emoji = "🟢" if event.direction == "bullish" else "🔴" if event.direction == "bearish" else "⚪"

        msg = (
            f"{emoji} *SMC Alert — {event.symbol}* {direction_emoji}\n"
            f"`{event.alert_type.upper()}` @ `{event.price:.4f}`\n\n"
            f"{commentary}\n\n"
            f"_HTF: {event.htf_trend or 'N/A'} | {event.timeframe}_"
        )

        try:
            import asyncio
            if self.tg.loop and self.tg.loop.is_running():
                asyncio.run_coroutine_threadsafe(
                    self.tg.app.bot.send_message(
                        chat_id=self.tg.chat_id,
                        text=msg,
                        parse_mode="Markdown",
                    ),
                    self.tg.loop,
                )
                logger.info("[SMC-ALERT] Telegram message queued for %s", event.symbol)
            else:
                logger.warning("[SMC-ALERT] Telegram loop not running")
        except Exception as e:
            logger.error("[SMC-ALERT] Telegram send failed: %s", e)
