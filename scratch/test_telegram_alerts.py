#!/usr/bin/env python3
"""Send a single round of test alerts to Telegram to verify formatting."""
import os
import sys
import time

# Load .env
if os.path.exists(".env"):
    import dotenv
    dotenv.load_dotenv(".env")

from crypto_trader.events import (
    EventBus, TradeOpenedEvent, TradeClosedEvent,
    RiskHaltEvent, SystemFailureEvent, SignalRejectedEvent,
)
from crypto_trader.telegram_bot import TelegramService


def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        print("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID required in .env")
        sys.exit(1)

    bus = EventBus()
    tg = TelegramService(token, chat_id, bus)
    tg.enabled = True
    tg.start()

    # Give Telegram bot time to start its loop
    time.sleep(3)

    print("Sending test alerts...")

    # 1. Trade Opened
    bus.publish(TradeOpenedEvent(
        symbol="SOLUSDT",
        side="SHORT",
        leverage=5,
        entry_price=169.45,
        stop_loss=174.00,
        take_profit=161.00,
        margin=9.80,
        notional=49.00,
        regime="TRENDING_DOWN",
        tech_score=0.78,
        llm_decision="neutral",
        funding=-0.0001,
        oi_delta=0.02,
        liquidation_price=203.34,
        wallet_balance=49.18,
    ))
    time.sleep(1)

    # 2. Trade Closed (win)
    bus.publish(TradeClosedEvent(
        symbol="SOLUSDT",
        side="SHORT",
        realized_pnl=1.23,
        pnl_percent=2.51,
        exit_price=165.20,
        reason="TAKE_PROFIT",
        duration_minutes=45.5,
        mfe=0.035,
        mae=-0.008,
        fees=0.12,
        slippage=0.01,
        tds=0.05,
        wallet_balance=50.41,
    ))
    time.sleep(1)

    # 3. Trade Closed (loss)
    bus.publish(TradeClosedEvent(
        symbol="BTCUSDT",
        side="LONG",
        realized_pnl=-0.85,
        pnl_percent=-1.70,
        exit_price=104200.00,
        reason="STOP_LOSS",
        duration_minutes=12.0,
        mfe=0.008,
        mae=-0.018,
        fees=0.15,
        wallet_balance=49.56,
    ))
    time.sleep(1)

    # 4. Risk Halt
    bus.publish(RiskHaltEvent(
        reason="Max drawdown exceeded",
        daily_pnl=-2.45,
        trades_today=3,
    ))
    time.sleep(1)

    # 5. System Failure
    bus.publish(SystemFailureEvent(
        component="websocket_feed",
        error="Connection reset by peer",
        severity="HIGH",
    ))
    time.sleep(1)

    # 6. Signal Rejected (basis-guard)
    bus.publish(SignalRejectedEvent(
        symbol="ETHUSDT",
        reason="basis guard: cross-venue basis 0.012 > max 0.005",
        tech_score=0.82,
        final_score=0.82,
        threshold=0.72,
    ))
    time.sleep(1)

    # 7. SMC Alerts (send directly — bypass LLM for speed)
    smc_alerts = [
        ("🌊", "SOLUSDT", "SWEEP", "bearish", 169.20,
         "Liquidity sweep of swing 170.50 → wick to 169.20 (2c ago). "
         "Sell-side stops were absorbed; quick reclaim suggests trapped shorts. "
         "Watch for continuation lower or a failure to hold below 169.",
         "TRENDING_DOWN"),
        ("📈", "BTCUSDT", "BOS", "bullish", 105000.00,
         "Break of Structure above 105k confirms bullish continuation on 15m. "
         "HTF 4h aligned. Next liquidity pool near 107k. Momentum intact.",
         "TREND_EXPANSION"),
        ("🧲", "ETHUSDT", "FVG", "bullish", 3200.50,
         "Unmitigated FVG 3200.50-3215.00 acts as a bullish magnet. "
         "Price may retrace into this zone before continuation. Support hold critical.",
         "RANGING"),
        ("📦", "XRPUSDT", "OB", "bearish", 2.4500,
         "Bearish Order Block 2.4400-2.4600 is unmitigated. "
         "Price currently rejecting from the block top. Resistance until invalidated.",
         "CHOP"),
    ]

    for emoji, symbol, alert_type, direction, price, commentary, htf in smc_alerts:
        direction_emoji = "🟢" if direction == "bullish" else "🔴"
        msg = (
            f"{emoji} *SMC Alert — {symbol}* {direction_emoji}\n"
            f"`{alert_type}` @ `{price:.4f}`\n\n"
            f"{commentary}\n\n"
            f"_HTF: {htf} | 15m_"
        )
        import asyncio
        asyncio.run_coroutine_threadsafe(
            tg.app.bot.send_message(chat_id=tg.chat_id, text=msg, parse_mode="Markdown"),
            tg.loop,
        )
        time.sleep(0.5)

    print("\nAll test alerts sent. Check your Telegram.")
    time.sleep(5)


if __name__ == "__main__":
    main()
