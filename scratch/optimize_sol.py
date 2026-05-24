import sys
import os
import pandas as pd
import numpy as np

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from ares_bot_toolkit import Config, Backtester, RiskManager

def main():
    print("Starting deep parameter search for SOL/USDT:USDT...")
    
    base_cfg = Config()
    base_cfg.MAX_MARGIN_RATIO = 0.40
    
    # Instantiate Backtester exactly ONCE to avoid expensive CCXT exchange instantiation overhead
    tester = Backtester(base_cfg)
    
    try:
        df = tester.fetch_data("SOL/USDT:USDT", days=90)
    except Exception as e:
        print(f"Error fetching data: {e}")
        return

    print(f"Loaded {len(df)} candles for SOL.")
    
    # Search grid
    bb_periods = [15, 20, 25, 30]
    bb_stds = [1.5, 2.0, 2.5, 3.0]
    adx_thresholds = [15, 20, 25, 30]
    sl_pcts = [0.01, 0.015, 0.02, 0.025, 0.03]
    tp_pcts = [0.02, 0.03, 0.04, 0.05, 0.06, 0.08]
    
    results = []
    
    # Grid search
    count = 0
    total = len(bb_periods) * len(bb_stds) * len(adx_thresholds) * len(sl_pcts) * len(tp_pcts)
    print(f"Testing {total} parameter combinations...")
    
    for period in bb_periods:
        for std in bb_stds:
            for adx_th in adx_thresholds:
                for sl in sl_pcts:
                    for tp in tp_pcts:
                        # Re-assign configuration values dynamically on the reused objects
                        tester.cfg.BB_PERIOD = period
                        tester.cfg.BB_STD = std
                        tester.cfg.ADX_TREND_THRESHOLD = adx_th
                        tester.cfg.SL_PCT = sl
                        tester.cfg.TP_PCT = tp
                        
                        res = tester.run_with_data(df, "SOL/USDT:USDT")
                        
                        results.append({
                            'bb_period': period,
                            'bb_std': std,
                            'adx_threshold': adx_th,
                            'sl_pct': sl,
                            'tp_pct': tp,
                            'return': res.total_return,
                            'sharpe': res.sharpe,
                            'drawdown': res.max_drawdown,
                            'trades': res.num_trades,
                            'win_rate': res.win_rate
                        })
                        count += 1
                        if count % 500 == 0:
                            print(f"Progress: {count}/{total} combinations tested...")

    # Sort results by return and then Sharpe ratio
    results.sort(key=lambda x: (x['return'], x['sharpe']), reverse=True)
    
    print("\n" + "="*80)
    print("TOP 10 PARAMETER CONFIGURATIONS FOR SOL/USDT:USDT")
    print("="*80)
    print(f"{'Rank':<5}{'BB Per':<8}{'BB Std':<8}{'ADX Lim':<8}{'SL%':<8}{'TP%':<8}{'Return':<10}{'Sharpe':<8}{'Trades':<8}{'Win Rate':<8}")
    print("-"*80)
    for idx, r in enumerate(results[:10]):
        print(f"#{idx+1:<4}{r['bb_period']:<8}{r['bb_std']:<8}{r['adx_threshold']:<8}{r['sl_pct']*100:>5.1f}%{r['tp_pct']*100:>7.1f}%{r['return']*100:>8.1f}%  {r['sharpe']:>6.2f}  {r['trades']:<8}{r['win_rate']*100:>6.1f}%")
    print("="*80)

if __name__ == "__main__":
    main()
