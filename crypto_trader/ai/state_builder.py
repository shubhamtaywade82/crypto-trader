"""crypto_trader.ai.state_builder — Converts indicators and DataFrames into normalized market state."""

import pandas as pd
from typing import Optional

from crypto_trader.ai.schemas import MarketStatePayload
from crypto_trader.structure import MarketStructureAnalyzer


class StateBuilder:
    @staticmethod
    def build(
        symbol: str,
        timeframe: str,
        mode: str,
        df_ltf: pd.DataFrame,
        df_htf: pd.DataFrame,
        mark_price: float,
        funding_rate: float,
        oi_delta: float,
        risk_budget: float,
    ) -> MarketStatePayload:
        # 1. Analyze market structure using local indicator rule layers
        analyzer = MarketStructureAnalyzer(df_ltf, swing_window=3)
        struct = analyzer.analyze()

        # 2. HTF Trend identification
        ema200 = df_htf["close"].ewm(span=200, adjust=False).mean().iloc[-1]
        htf_trend = "bullish" if mark_price > ema200 else "bearish"

        # 3. Structure string — derive from the latest break, distinguishing a
        #    Change of Character (reversal) from a Break of Structure (continuation).
        recent_bos = struct.get("recent_bos")
        market_structure = "RANGE"
        if recent_bos:
            bos_type = recent_bos.get("type", "BULLISH")     # BULLISH or BEARISH
            is_choch = recent_bos.get("event") == "CHOCH"
            if bos_type == "BULLISH":
                market_structure = "CHOCH_UP" if is_choch else "BOS_UP"
            else:
                market_structure = "CHOCH_DOWN" if is_choch else "BOS_DOWN"

        # 4. Volume anomaly
        vol_std = df_ltf["volume"].rolling(20).std().iloc[-1]
        vol_mean = df_ltf["volume"].rolling(20).mean().iloc[-1]
        volume_anomaly = bool(df_ltf["volume"].iloc[-1] > (vol_mean + 2 * vol_std))

        # 5. Liquidity sweep
        liquidity_sweep = struct.get("recent_sweep") is not None

        # 6. Derive volatility_regime from ATR (no "regime" key in struct)
        #    ATR = rolling mean of True Range over 14 periods
        high = df_ltf["high"]
        low = df_ltf["low"]
        close = df_ltf["close"]
        prev_close = close.shift(1)
        tr = pd.concat(
            [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
        ).max(axis=1)
        atr = tr.rolling(14).mean()

        if len(atr) >= 28:
            atr_current = atr.iloc[-1]
            atr_avg = atr.iloc[-28:-14].mean()  # Prior 14-period window
            if atr_current > atr_avg * 1.2:
                volatility_regime = "expanding"
            elif atr_current < atr_avg * 0.8:
                volatility_regime = "compressed"
            else:
                volatility_regime = "chop"
        else:
            volatility_regime = "chop"

        return MarketStatePayload(
            symbol=symbol,
            timeframe=timeframe,
            mode=mode,
            price=mark_price,
            htf_trend=htf_trend,
            market_structure=market_structure,
            volatility_regime=volatility_regime,
            funding_rate=funding_rate,
            open_interest_change=oi_delta,
            volume_anomaly=volume_anomaly,
            liquidity_sweep=liquidity_sweep,
            risk_budget_pct=risk_budget,
        )
