"""
crypto_trader.structure — Smart Money Concepts (SMC) & Price Action Calculations
=============================================================================
Provides deterministic, standard-library-backed quantitative analysis of:
    - Swing Highs / Swing Lows (peaks and troughs)
    - Break of Structure (BOS) / Change of Character (CHoCH)
    - Fair Value Gaps (FVG) and mitigation tracking
    - Order Blocks (OB) and mitigation tracking
    - Liquidity Sweeps (wick penetrations of prior swings)
    - Price Action signals (Pinbars and Engulfing candles)
"""

import logging
from typing import Dict, List, Optional
import pandas as pd
import numpy as np

logger = logging.getLogger("crypto_trader.structure")


class MarketStructureAnalyzer:
    """Computes programmatically derived SMC and Price Action elements from a DataFrame."""

    def __init__(self, df: pd.DataFrame, swing_window: int = 3):
        """
        Args:
            df: DataFrame containing at least ['open', 'high', 'low', 'close', 'open_time'] columns.
            swing_window: Candle radius to confirm a swing high/low (e.g., 3-5 candles left/right).
        """
        self.df = df.copy() if df is not None else pd.DataFrame()
        self.swing_window = swing_window
        self.has_sufficient_data = len(self.df) >= max(20, 2 * swing_window + 1)

    def analyze(self) -> Dict:
        """
        Performs full market structure and Price Action calculations.
        Returns a rich structured dictionary of features.
        """
        if not self.has_sufficient_data:
            return self._empty_result()

        # Ensure index is standard sequential 0 to N-1
        self.df = self.df.reset_index(drop=True)
        n_candles = len(self.df)

        swing_highs = []    # Dicts: {index, price, time, broken, break_index}
        swing_lows = []     # Dicts: {index, price, time, broken, break_index}
        bos_events = []     # Dicts: {index, type, swing_index, swing_price, break_price, time}
        order_blocks = []   # Dicts: {index, type, high, low, open, close, time, mitigated, mitigate_index}
        sweep_events = []   # Dicts: {index, type, swing_index, swing_price, swept_price, time}
        fvgs = []           # Dicts: {index, type, top, bottom, time, mitigated, mitigate_index}

        # ── 1. Chronological Scanner for Swing Points, BOS, sweeps, OBs and FVGs ──
        # ── 1. Chronological Scanner for Swing Points, BOS, sweeps, OBs and FVGs ──
        for i in range(n_candles):
            # A. Check for Swing Point confirmations (confirmed at target_idx = i - swing_window)
            if i >= 2 * self.swing_window:
                target_idx = i - self.swing_window
                
                # Swing High Check
                high_window = self.df['high'].iloc[target_idx - self.swing_window : target_idx + self.swing_window + 1]
                if self.df['high'].iloc[target_idx] == high_window.max() and high_window.min() != high_window.max():
                    # Uniqueness check
                    if not any(sh['index'] == target_idx for sh in swing_highs):
                        swing_highs.append({
                            'index': target_idx,
                            'price': self.df['high'].iloc[target_idx],
                            'time': self.df['open_time'].iloc[target_idx],
                            'broken': False,
                            'break_index': None
                        })

                # Swing Low Check
                low_window = self.df['low'].iloc[target_idx - self.swing_window : target_idx + self.swing_window + 1]
                if self.df['low'].iloc[target_idx] == low_window.min() and low_window.min() != low_window.max():
                    if not any(sl['index'] == target_idx for sl in swing_lows):
                        swing_lows.append({
                            'index': target_idx,
                            'price': self.df['low'].iloc[target_idx],
                            'time': self.df['open_time'].iloc[target_idx],
                            'broken': False,
                            'break_index': None
                        })

            # B. Get current candle attributes
            close_val = self.df['close'].iloc[i]
            open_val = self.df['open'].iloc[i]
            high_val = self.df['high'].iloc[i]
            low_val = self.df['low'].iloc[i]

            # C. Check for Liquidity Sweeps before breaking structure
            for sl in swing_lows:
                if not sl['broken'] and i > sl['index']:
                    # Wick went below swing low, but candle closed above it
                    if low_val < sl['price'] and close_val > sl['price']:
                        sweep_events.append({
                            'index': i,
                            'type': 'BULLISH',  # Sweeping sell-side liquidity is a bullish signal
                            'swing_index': sl['index'],
                            'swing_price': sl['price'],
                            'swept_price': low_val,
                            'time': self.df['open_time'].iloc[i]
                        })

            for sh in swing_highs:
                if not sh['broken'] and i > sh['index']:
                    # Wick went above swing high, but candle closed below it
                    if high_val > sh['price'] and close_val < sh['price']:
                        sweep_events.append({
                            'index': i,
                            'type': 'BEARISH',  # Sweeping buy-side liquidity is a bearish signal
                            'swing_index': sh['index'],
                            'swing_price': sh['price'],
                            'swept_price': high_val,
                            'time': self.df['open_time'].iloc[i]
                        })

            # D. Check for Break of Structure (BOS) / structural breakouts (requires close beyond swing)
            for sh in swing_highs:
                if not sh['broken'] and close_val > sh['price'] and i > sh['index']:
                    sh['broken'] = True
                    sh['break_index'] = i
                    bos_events.append({
                        'index': i,
                        'type': 'BULLISH',
                        'swing_index': sh['index'],
                        'swing_price': sh['price'],
                        'break_price': close_val,
                        'time': self.df['open_time'].iloc[i]
                    })
                    # Bullish Order Block: last bearish candle prior to the breakout swing point index
                    ob_idx = None
                    for k in range(sh['index'], -1, -1):
                        if self.df['close'].iloc[k] < self.df['open'].iloc[k]:
                            ob_idx = k
                            break
                    if ob_idx is not None:
                        order_blocks.append({
                            'index': ob_idx,
                            'formed_index': i,
                            'type': 'BULLISH',
                            'high': self.df['high'].iloc[ob_idx],
                            'low': self.df['low'].iloc[ob_idx],
                            'open': self.df['open'].iloc[ob_idx],
                            'close': self.df['close'].iloc[ob_idx],
                            'time': self.df['open_time'].iloc[ob_idx],
                            'mitigated': False,
                            'mitigate_index': None
                        })

            for sl in swing_lows:
                if not sl['broken'] and close_val < sl['price'] and i > sl['index']:
                    sl['broken'] = True
                    sl['break_index'] = i
                    bos_events.append({
                        'index': i,
                        'type': 'BEARISH',
                        'swing_index': sl['index'],
                        'swing_price': sl['price'],
                        'break_price': close_val,
                        'time': self.df['open_time'].iloc[i]
                    })
                    # Bearish Order Block: last bullish candle prior to the breakout swing point index
                    ob_idx = None
                    for k in range(sl['index'], -1, -1):
                        if self.df['close'].iloc[k] > self.df['open'].iloc[k]:
                            ob_idx = k
                            break
                    if ob_idx is not None:
                        order_blocks.append({
                            'index': ob_idx,
                            'formed_index': i,
                            'type': 'BEARISH',
                            'high': self.df['high'].iloc[ob_idx],
                            'low': self.df['low'].iloc[ob_idx],
                            'open': self.df['open'].iloc[ob_idx],
                            'close': self.df['close'].iloc[ob_idx],
                            'time': self.df['open_time'].iloc[ob_idx],
                            'mitigated': False,
                            'mitigate_index': None
                        })

            # E. Check for Order Block Mitigation by current price
            for ob in order_blocks:
                if not ob['mitigated'] and i > ob['formed_index']:
                    if ob['type'] == 'BULLISH':
                        if low_val <= ob['high']:
                            ob['mitigated'] = True
                            ob['mitigate_index'] = i
                    elif ob['type'] == 'BEARISH':
                        if high_val >= ob['low']:
                            ob['mitigated'] = True
                            ob['mitigate_index'] = i

            # F. Detect Fair Value Gaps (FVG) formed by candles i-2, i-1, i
            if i >= 2:
                # Bullish FVG: Low of current candle is higher than High of candle i-2
                prev2_high = self.df['high'].iloc[i - 2]
                curr_low = self.df['low'].iloc[i]
                if curr_low > prev2_high:
                    fvgs.append({
                        'index': i - 1,  # FVG is centered on the middle candle
                        'type': 'BULLISH',
                        'top': curr_low,
                        'bottom': prev2_high,
                        'time': self.df['open_time'].iloc[i - 1],
                        'mitigated': False,
                        'mitigate_index': None
                    })

                # Bearish FVG: High of current candle is lower than Low of candle i-2
                prev2_low = self.df['low'].iloc[i - 2]
                curr_high = self.df['high'].iloc[i]
                if curr_high < prev2_low:
                    fvgs.append({
                        'index': i - 1,
                        'type': 'BEARISH',
                        'top': prev2_low,
                        'bottom': curr_high,
                        'time': self.df['open_time'].iloc[i - 1],
                        'mitigated': False,
                        'mitigate_index': None
                    })

            # G. Check for FVG Mitigation by current price
            for fvg in fvgs:
                if not fvg['mitigated'] and i > fvg['index'] + 1:
                    if fvg['type'] == 'BULLISH':
                        # Mitigated when subsequent price enters the gap from above
                        if low_val <= fvg['top']:
                            fvg['mitigated'] = True
                            fvg['mitigate_index'] = i
                    elif fvg['type'] == 'BEARISH':
                        # Mitigated when subsequent price enters the gap from below
                        if high_val >= fvg['bottom']:
                            fvg['mitigated'] = True
                            fvg['mitigate_index'] = i

        # ── 2. Identify Candlestick Price Action Signals ──
        # Check both the last forming candle (index -1) and the last completed candle (index -2)
        price_action_signals = []
        for offset in [-2, -1]:
            if n_candles >= abs(offset):
                idx = n_candles + offset
                row = self.df.iloc[idx]
                pin = self._detect_pinbar(row)
                if pin:
                    price_action_signals.append({
                        'candle': 'forming' if offset == -1 else 'completed',
                        'pattern': f'{pin}_PINBAR',
                        'price': row['close'],
                        'time': row['open_time']
                    })

                if idx > 0:
                    row_prev = self.df.iloc[idx - 1]
                    eng = self._detect_engulfing(row_prev, row)
                    if eng:
                        price_action_signals.append({
                            'candle': 'forming' if offset == -1 else 'completed',
                            'pattern': f'{eng}_ENGULFING',
                            'price': row['close'],
                            'time': row['open_time']
                        })

        # ── 3. Compile Unmitigated & Recent Structural Elements ──
        active_sh = [sh for sh in swing_highs if not sh['broken']]
        active_sl = [sl for sl in swing_lows if not sl['broken']]
        unmitigated_ob = [ob for ob in order_blocks if not ob['mitigated']]
        unmitigated_fvg = [f for f in fvgs if not f['mitigated']]

        recent_bos = bos_events[-1] if bos_events else None
        recent_sweep = sweep_events[-1] if sweep_events else None

        # Add candle distance context
        if recent_bos:
            recent_bos['candles_ago'] = n_candles - 1 - recent_bos['index']
        if recent_sweep:
            recent_sweep['candles_ago'] = n_candles - 1 - recent_sweep['index']

        return {
            'swing_highs_count': len(swing_highs),
            'swing_lows_count': len(swing_lows),
            'active_swing_highs': active_sh[-3:],  # Last 3 active swing highs
            'active_swing_lows': active_sl[-3:],   # Last 3 active swing lows
            'recent_bos': recent_bos,
            'recent_sweep': recent_sweep,
            'unmitigated_order_blocks': unmitigated_ob[-3:],  # Last 3 unmitigated OBs
            'unmitigated_fvgs': unmitigated_fvg[-3:],          # Last 3 unmitigated FVGs
            'price_action_signals': price_action_signals
        }

    def _detect_pinbar(self, row: pd.Series) -> Optional[str]:
        """Detect pinbar rejection pattern on a single candle."""
        o, h, l, c = row['open'], row['high'], row['low'], row['close']
        body = abs(c - o)
        rng = h - l
        if rng == 0:
            return None

        lower_wick = min(o, c) - l
        upper_wick = h - max(o, c)

        # Bullish Pinbar: long lower wick, tiny upper wick, body in upper half
        is_bullish = (
            lower_wick >= 1.5 * body and 
            upper_wick <= 0.5 * lower_wick and 
            lower_wick >= 0.4 * rng and
            min(o, c) >= (l + 0.4 * rng)
        )

        # Bearish Pinbar: long upper wick, tiny lower wick, body in lower half
        is_bearish = (
            upper_wick >= 1.5 * body and 
            lower_wick <= 0.5 * upper_wick and 
            upper_wick >= 0.4 * rng and
            max(o, c) <= (h - 0.4 * rng)
        )

        if is_bullish:
            return 'BULLISH'
        if is_bearish:
            return 'BEARISH'
        return None

    def _detect_engulfing(self, prev: pd.Series, curr: pd.Series) -> Optional[str]:
        """Detect engulfing pattern on a pair of consecutive candles."""
        op, cp = prev['open'], prev['close']
        oc, cc = curr['open'], curr['close']

        prev_bearish = cp < op
        prev_bullish = cp > op
        curr_bearish = cc < oc
        curr_bullish = cc > oc

        if prev_bearish and curr_bullish:
            # Bullish Engulfing: Close is above previous Open, and Open is below/at previous Close
            if cc > op and oc <= cp:
                return 'BULLISH'

        if prev_bullish and curr_bearish:
            # Bearish Engulfing: Close is below previous Open, and Open is above/at previous Close
            if cc < op and oc >= cp:
                return 'BEARISH'

        return None

    def _empty_result(self) -> Dict:
        """Returns empty/default dictionary in case of insufficient data."""
        return {
            'swing_highs_count': 0,
            'swing_lows_count': 0,
            'active_swing_highs': [],
            'active_swing_lows': [],
            'recent_bos': None,
            'recent_sweep': None,
            'unmitigated_order_blocks': [],
            'unmitigated_fvgs': [],
            'price_action_signals': []
        }
