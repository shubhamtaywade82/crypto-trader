import unittest
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

from crypto_trader.structure import MarketStructureAnalyzer

class TestMarketStructureAnalyzer(unittest.TestCase):
    def setUp(self):
        # Generate base synthetic data structure
        self.base_time = datetime(2026, 5, 21, 12, 0, 0)
        self.default_candles = 50

    def _create_flat_df(self, n=50, price=100.0):
        data = []
        for i in range(n):
            data.append({
                'open_time': self.base_time + timedelta(hours=i),
                'open': price,
                'high': price,
                'low': price,
                'close': price,
                'volume': 1000.0,
                'quote_volume': 100000.0
            })
        return pd.DataFrame(data)

    def test_swing_points_detection(self):
        """Verify local Swing Highs and Swing Lows are correctly identified."""
        df = self._create_flat_df(20, price=100.0)
        
        # Inject Swing High at index 8 (radius = 3)
        df.loc[5, 'high'] = 101.0
        df.loc[6, 'high'] = 102.0
        df.loc[7, 'high'] = 103.0
        df.loc[8, 'high'] = 105.0  # Peak
        df.loc[9, 'high'] = 103.0
        df.loc[10, 'high'] = 102.0
        df.loc[11, 'high'] = 101.0

        # Inject Swing Low at index 14 (radius = 3)
        df.loc[11, 'low'] = 99.0
        df.loc[12, 'low'] = 98.0
        df.loc[13, 'low'] = 97.0
        df.loc[14, 'low'] = 95.0   # Trough
        df.loc[15, 'low'] = 97.0
        df.loc[16, 'low'] = 98.0
        df.loc[17, 'low'] = 99.0

        analyzer = MarketStructureAnalyzer(df, swing_window=3)
        res = analyzer.analyze()

        self.assertEqual(res['swing_highs_count'], 1)
        self.assertEqual(res['swing_lows_count'], 1)

        # Verify swing points are matching indices
        self.assertEqual(res['active_swing_highs'][0]['index'], 8)
        self.assertEqual(res['active_swing_highs'][0]['price'], 105.0)

        self.assertEqual(res['active_swing_lows'][0]['index'], 14)
        self.assertEqual(res['active_swing_lows'][0]['price'], 95.0)

    def test_break_of_structure_bullish(self):
        """Verify Bullish BOS is triggered when close breaks swing high."""
        df = self._create_flat_df(25, price=100.0)
        
        # 1. Inject Swing High at index 8
        df.loc[8, 'high'] = 105.0
        # Surrounding highs to establish the swing
        for offset in [-3, -2, -1, 1, 2, 3]:
            df.loc[8 + offset, 'high'] = 102.0

        # Inject a bearish candle at index 7 to trigger a Bullish Order Block
        df.loc[7, 'open'] = 101.0
        df.loc[7, 'close'] = 99.0
        df.loc[7, 'high'] = 102.0
        df.loc[7, 'low'] = 98.0

        # 2. Break above Swing High at index 15
        df.loc[15, 'open'] = 104.0
        df.loc[15, 'high'] = 108.0
        df.loc[15, 'low'] = 103.0
        df.loc[15, 'close'] = 107.0  # Close > 105.0

        # 3. Elevate subsequent prices to prevent immediate mitigation of Order Block (range 98.0 - 102.0)
        # So low must be > 102.0
        for k in range(16, 25):
            df.loc[k, 'open'] = 105.0
            df.loc[k, 'high'] = 106.0
            df.loc[k, 'low'] = 104.0
            df.loc[k, 'close'] = 105.0

        analyzer = MarketStructureAnalyzer(df, swing_window=3)
        res = analyzer.analyze()

        self.assertIsNotNone(res['recent_bos'])
        self.assertEqual(res['recent_bos']['type'], 'BULLISH')
        self.assertEqual(res['recent_bos']['swing_price'], 105.0)
        self.assertEqual(res['recent_bos']['break_price'], 107.0)
        self.assertEqual(res['recent_bos']['index'], 15)

        # The swing high at index 8 should now be marked broken (i.e. not in active_swing_highs)
        has_swing_8 = any(sh['index'] == 8 for sh in res['active_swing_highs'])
        self.assertFalse(has_swing_8)

        # Verify a Bullish Order Block was created at last bearish candle before index 8
        self.assertTrue(len(res['unmitigated_order_blocks']) > 0)
        self.assertEqual(res['unmitigated_order_blocks'][0]['type'], 'BULLISH')

    def test_fair_value_gap_mitigation(self):
        """Verify FVG creation and mitigation tracking."""
        # Use a baseline of 105.0 with 25 candles so that it satisfies the len >= 20 constraint
        df = self._create_flat_df(25, price=105.0)

        # Inject Bullish FVG at index 5 (formed by 4, 5, 6)
        # Low of 6 (103.0) is higher than High of 4 (101.0)
        df.loc[4, 'high'] = 101.0
        df.loc[4, 'open'] = 105.0
        df.loc[4, 'close'] = 105.0
        df.loc[4, 'low'] = 100.0

        df.loc[5, 'open'] = 101.0
        df.loc[5, 'close'] = 104.0
        df.loc[5, 'high'] = 105.0
        df.loc[5, 'low'] = 101.0
        
        df.loc[6, 'low'] = 103.0  # FVG top = 103.0, bottom = 101.0
        df.loc[6, 'high'] = 106.0
        df.loc[6, 'open'] = 103.0
        df.loc[6, 'close'] = 105.0

        # Test unmitigated FVG
        analyzer = MarketStructureAnalyzer(df, swing_window=3)
        res = analyzer.analyze()
        
        self.assertEqual(len(res['unmitigated_fvgs']), 1)
        fvg = res['unmitigated_fvgs'][0]
        self.assertEqual(fvg['type'], 'BULLISH')
        self.assertEqual(fvg['top'], 103.0)
        self.assertEqual(fvg['bottom'], 101.0)

        # Now mitigate the FVG on candle 10 (low dips below 103.0)
        df.loc[10, 'low'] = 102.0
        analyzer2 = MarketStructureAnalyzer(df, swing_window=3)
        res2 = analyzer2.analyze()
        self.assertEqual(len(res2['unmitigated_fvgs']), 0)

    def test_liquidity_sweep(self):
        """Verify Liquidity sweeps are correctly identified."""
        df = self._create_flat_df(20, price=100.0)

        # Create Swing Low at index 6
        df.loc[6, 'low'] = 95.0
        for offset in [-3, -2, -1, 1, 2, 3]:
            df.loc[6 + offset, 'low'] = 98.0

        # Inject a sweep at index 12 (wick below 95.0, close above 95.0)
        df.loc[12, 'open'] = 97.0
        df.loc[12, 'high'] = 99.0
        df.loc[12, 'low'] = 94.0   # Swept below 95.0
        df.loc[12, 'close'] = 98.0  # Closed above 95.0

        analyzer = MarketStructureAnalyzer(df, swing_window=3)
        res = analyzer.analyze()

        self.assertIsNotNone(res['recent_sweep'])
        self.assertEqual(res['recent_sweep']['type'], 'BULLISH')
        self.assertEqual(res['recent_sweep']['swing_price'], 95.0)
        self.assertEqual(res['recent_sweep']['swept_price'], 94.0)

    def test_price_action_patterns(self):
        """Verify Pinbar and Engulfing price action detectors."""
        df = self._create_flat_df(25, price=100.0)

        # Bullish Pinbar on the last completed candle (index -2, which is index 23)
        # Open: 100.0, Close: 101.0, High: 101.2, Low: 95.0
        # Body = 1.0, lower wick = 5.0, upper wick = 0.2
        df.loc[23, 'open'] = 100.0
        df.loc[23, 'close'] = 101.0
        df.loc[23, 'high'] = 101.2
        df.loc[23, 'low'] = 95.0

        analyzer = MarketStructureAnalyzer(df, swing_window=2)
        res = analyzer.analyze()

        pa_signals = res['price_action_signals']
        self.assertTrue(len(pa_signals) >= 1)
        self.assertEqual(pa_signals[0]['pattern'], 'BULLISH_PINBAR')
        self.assertEqual(pa_signals[0]['candle'], 'completed')

if __name__ == '__main__':
    unittest.main()
