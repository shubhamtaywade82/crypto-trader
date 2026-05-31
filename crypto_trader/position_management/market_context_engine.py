"""
crypto_trader.position_management.market_context_engine
========================================================
Layer 1 (market sub-component): Derives a MarketContext for any symbol by
wrapping the existing regime, structure, and data-feed infrastructure.

Computes:
  - Trend direction (EMA alignment + ADX)
  - Market regime (ExtendedRegime from RegimeClassifier)
  - ATR / realised volatility
  - VWAP distance
  - SMC structure (BOS, CHoCH, range)
  - Liquidity quality (spread + volume)
  - Momentum state (RSI-based)
  - Support / resistance proximity
  - Event risk (funding extremes, approaching expiry)
  - Data staleness detection
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import numpy as np
import pandas as pd

from .schemas import MarketContext

logger = logging.getLogger("crypto_trader.position_management.market_context_engine")

_STALE_THRESHOLD_MS = 60_000   # 60 s


class MarketContextEngine:
    """
    Builds a MarketContext for a single symbol using existing infrastructure.

    Designed to be called once per reassessment cycle per symbol.  The caller
    should pass the same DataFrame it uses for signal generation so we avoid
    re-fetching candles.
    """

    def __init__(
        self,
        data_feed=None,          # BinanceDataFeed (optional — used if df not supplied)
        ws_feed=None,            # BinanceWebSocketFeed for live bid/ask / mark price
        swing_window: int = 3,
    ):
        self.data_feed = data_feed
        self.ws_feed = ws_feed
        self.swing_window = swing_window

    def build(
        self,
        symbol: str,
        df: Optional[pd.DataFrame] = None,
        timeframe: str = "1h",
        current_price: Optional[float] = None,
    ) -> MarketContext:
        """
        Build a MarketContext.  If `df` is not provided and a data_feed is
        configured, fetches fresh candles for the symbol.
        """
        ctx = MarketContext(symbol=symbol)

        # Fetch candles if not provided
        if df is None and self.data_feed is not None:
            try:
                df = self.data_feed.get_klines(symbol, interval=timeframe, limit=200)
            except Exception as exc:
                logger.warning("[MCE] Failed to fetch klines for %s: %s", symbol, exc)
                ctx.is_stale = True
                return ctx

        if df is None or len(df) < 20:
            ctx.is_stale = True
            return ctx

        ltp = current_price
        if ltp is None and self.ws_feed is not None:
            ws_data = getattr(self.ws_feed, "latest_data", None)
            if ws_data:
                ltp = ws_data.mark_price or ws_data.ltp
        if ltp is None and len(df):
            ltp = float(df["close"].iloc[-1])

        now_ms = int(time.time() * 1000)

        # Data staleness — compare last candle close_time to now
        try:
            last_ts = int(df["close_time"].iloc[-1]) if "close_time" in df.columns else 0
            data_age_ms = now_ms - last_ts if last_ts else 0
        except Exception:
            data_age_ms = 0
        ctx.data_age_ms = data_age_ms
        ctx.is_stale = data_age_ms > _STALE_THRESHOLD_MS

        try:
            closes = df["close"].astype(float).values
            highs  = df["high"].astype(float).values
            lows   = df["low"].astype(float).values
            volumes = df["volume"].astype(float).values if "volume" in df.columns else np.ones(len(closes))

            # ATR (14-period)
            atr = _compute_atr(highs, lows, closes, period=14)
            ctx.atr = atr
            ctx.atr_pct = atr / ltp if ltp else 0.0

            # VWAP (intraday approximation using available data)
            vwap = _compute_vwap(highs, lows, closes, volumes)
            ctx.vwap = vwap
            ctx.vwap_distance_pct = (ltp - vwap) / vwap if vwap and ltp else 0.0

            # EMA trend
            ema20 = _ema(closes, 20)
            ema50 = _ema(closes, 50)
            ema200 = _ema(closes, 200) if len(closes) >= 200 else None
            ctx.trend = _classify_trend(ltp, ema20, ema50, ema200)

            # ADX-derived momentum
            adx = _compute_adx(highs, lows, closes, period=14)
            ctx.momentum = _classify_momentum(adx, closes)

            # Volatility state
            ctx.volatility_state = _classify_volatility(closes, atr)

            # Market structure via SMC analyzer
            ctx.structure = _derive_structure(df, self.swing_window)

            # Liquidity (spread + volume)
            ctx.liquidity = _classify_liquidity(volumes, self.ws_feed, symbol)

            # Funding rate + OI
            ctx.funding_rate, ctx.oi_change_pct = _get_funding_oi(
                symbol, self.data_feed
            )
            ctx.event_risk = _classify_event_risk(ctx.funding_rate)

            # Support / resistance proximity (within 0.5 * ATR)
            sr_threshold = 0.5 * atr if atr else 0.0
            swing_high, swing_low = _recent_swing_levels(highs, lows)
            ctx.near_resistance = bool(swing_high and abs(ltp - swing_high) <= sr_threshold)
            ctx.near_support    = bool(swing_low  and abs(ltp - swing_low)  <= sr_threshold)

            # Spread from book ticker
            ctx.spread_pct = _get_spread_pct(self.ws_feed, symbol)

            # Regime via RegimeClassifier
            ctx.regime = _classify_regime(df, adx)

        except Exception as exc:
            logger.error("[MCE] Error building context for %s: %s", symbol, exc, exc_info=True)

        return ctx


# ── Helper functions ─────────────────────────────────────────────────────────

def _ema(prices: np.ndarray, period: int) -> float:
    if len(prices) < period:
        return float(prices[-1]) if len(prices) else 0.0
    k = 2.0 / (period + 1)
    val = float(prices[0])
    for p in prices[1:]:
        val = float(p) * k + val * (1 - k)
    return val


def _compute_atr(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int = 14) -> float:
    if len(closes) < 2:
        return 0.0
    tr_list = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        tr_list.append(tr)
    if not tr_list:
        return 0.0
    recent = tr_list[-period:]
    return float(np.mean(recent))


def _compute_vwap(
    highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, volumes: np.ndarray
) -> float:
    typical = (highs + lows + closes) / 3.0
    cum_vol = np.sum(volumes)
    if cum_vol == 0:
        return float(closes[-1])
    return float(np.sum(typical * volumes) / cum_vol)


def _compute_adx(
    highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int = 14
) -> float:
    if len(closes) < period + 2:
        return 0.0
    plus_dm, minus_dm, tr_list = [], [], []
    for i in range(1, len(closes)):
        up   = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm.append(up   if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
        tr_list.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        ))

    def _smooth(arr, n):
        s = sum(arr[:n])
        result = [s]
        for v in arr[n:]:
            s = s - s / n + v
            result.append(s)
        return result

    tr_s   = _smooth(tr_list, period)
    pdm_s  = _smooth(plus_dm, period)
    mdm_s  = _smooth(minus_dm, period)

    dx_list = []
    for t, p, m in zip(tr_s, pdm_s, mdm_s):
        if t == 0:
            dx_list.append(0.0)
            continue
        pdi = 100 * p / t
        mdi = 100 * m / t
        denom = pdi + mdi
        dx_list.append(100 * abs(pdi - mdi) / denom if denom else 0.0)

    return float(np.mean(dx_list[-period:])) if dx_list else 0.0


def _classify_trend(ltp: float, ema20: float, ema50: float, ema200: Optional[float]) -> str:
    if ema200:
        if ltp > ema20 > ema50 > ema200:
            return "up"
        if ltp < ema20 < ema50 < ema200:
            return "down"
    elif ema20 and ema50:
        if ltp > ema20 > ema50:
            return "up"
        if ltp < ema20 < ema50:
            return "down"
    return "neutral"


def _classify_momentum(adx: float, closes: np.ndarray) -> str:
    # RSI-based direction, ADX-based strength
    rsi = _compute_rsi(closes, 14)
    if adx >= 25:
        if rsi >= 60:
            return "STRONG_UP"
        if rsi <= 40:
            return "STRONG_DOWN"
    if rsi >= 55:
        return "WEAK_UP"
    if rsi <= 45:
        return "WEAK_DOWN"
    return "NEUTRAL"


def _compute_rsi(closes: np.ndarray, period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    deltas = np.diff(closes.astype(float))
    gains  = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_g  = np.mean(gains[-period:])
    avg_l  = np.mean(losses[-period:])
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return 100.0 - (100.0 / (1 + rs))


def _classify_volatility(closes: np.ndarray, atr: float) -> str:
    if len(closes) < 20:
        return "UNKNOWN"
    pct_change = np.abs(np.diff(closes[-20:]) / closes[-19:])
    mean_vol = float(np.mean(pct_change))
    recent_vol = float(np.mean(pct_change[-5:])) if len(pct_change) >= 5 else mean_vol
    if recent_vol > mean_vol * 1.5:
        return "expanding"
    if recent_vol < mean_vol * 0.6:
        return "compressing"
    return "normal"


def _derive_structure(df: pd.DataFrame, swing_window: int) -> str:
    """Quick BOS/CHoCH/RANGE classification without full SMC pipeline."""
    try:
        from ..structure import MarketStructureAnalyzer
        analyzer = MarketStructureAnalyzer(df, swing_window=swing_window)
        result = analyzer.analyze()
        bos_events = result.get("bos_events", [])
        if bos_events:
            last = bos_events[-1]
            event = last.get("event", last.get("type", "BOS"))
            bos_type = last.get("type", "bullish")
            direction = "UP" if "bullish" in bos_type.lower() else "DOWN"
            return f"{event.upper()}_{direction}"
        return "RANGE"
    except Exception:
        return "UNKNOWN"


def _classify_liquidity(
    volumes: np.ndarray,
    ws_feed,
    symbol: str,
) -> str:
    if ws_feed is not None:
        ws_data = getattr(ws_feed, "latest_data", None)
        if ws_data:
            spread_pct = _get_spread_pct(ws_feed, symbol)
            if spread_pct < 0.001:
                return "strong"
            if spread_pct < 0.003:
                return "moderate"
            return "thin"
    # Fall back to volume relative to 20-bar average
    if len(volumes) >= 20:
        avg = float(np.mean(volumes[-20:]))
        recent = float(volumes[-1])
        ratio = recent / avg if avg else 1.0
        if ratio >= 1.2:
            return "strong"
        if ratio >= 0.7:
            return "moderate"
    return "thin"


def _get_spread_pct(ws_feed, symbol: str) -> float:
    if ws_feed is None:
        return 0.0
    try:
        ws_data = getattr(ws_feed, "latest_data", None)
        if ws_data:
            bid = getattr(ws_data, "best_bid", None) or getattr(ws_data, "bid_price", None)
            ask = getattr(ws_data, "best_ask", None) or getattr(ws_data, "ask_price", None)
            if bid and ask and bid > 0:
                return float((ask - bid) / bid)
    except Exception:
        pass
    return 0.0


def _get_funding_oi(symbol: str, data_feed) -> tuple[float, float]:
    if data_feed is None:
        return 0.0, 0.0
    try:
        funding = data_feed.get_funding_rate(symbol) or 0.0
        oi_change = 0.0
        try:
            oi_data = data_feed.get_open_interest(symbol)
            if isinstance(oi_data, list) and len(oi_data) >= 2:
                old = float(oi_data[-2].get("sumOpenInterestValue", 0) or 0)
                new = float(oi_data[-1].get("sumOpenInterestValue", 0) or 0)
                oi_change = (new - old) / old * 100 if old else 0.0
        except Exception:
            pass
        return float(funding), oi_change
    except Exception:
        return 0.0, 0.0


def _classify_event_risk(funding_rate: float) -> str:
    if abs(funding_rate) >= 0.001:   # 0.1% — extreme funding
        return "high"
    if abs(funding_rate) >= 0.0005:
        return "low"
    return "none"


def _recent_swing_levels(
    highs: np.ndarray, lows: np.ndarray, lookback: int = 20
) -> tuple[Optional[float], Optional[float]]:
    window_h = highs[-lookback:]
    window_l = lows[-lookback:]
    if len(window_h) == 0:
        return None, None
    return float(np.max(window_h)), float(np.min(window_l))


def _classify_regime(df: pd.DataFrame, adx: float) -> str:
    try:
        from ..regime import RegimeClassifier
        classifier = RegimeClassifier()
        ctx = classifier.classify(df, rvol=1.0, oi_delta=0.0, liquidation_intensity=0.0)
        return ctx.extended.value
    except Exception:
        # Fallback: simple ADX + price structure
        if adx >= 25:
            return "TREND_EXPANSION"
        if adx < 15:
            return "LOW_VOL_CHOP"
        return "ACCUMULATION"
