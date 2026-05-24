import sys
import os
import pandas as pd
import numpy as np
import logging

# Suppress debug/info logs during deep grid search to avoid printing thousands of margin blocks
logging.basicConfig(level=logging.WARNING)
logging.getLogger().setLevel(logging.WARNING)

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from ares_bot_toolkit import Config, Backtester, RiskManager, Direction

def main():
    print("Starting deep parameter search for SOL/USDT:USDT...")
    
    base_cfg = Config()
    base_cfg.MAX_MARGIN_RATIO = 0.40
    
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
            # 1. Update BB parameters and calculate indicators ONCE per outer combination
            tester.cfg.BB_PERIOD = period
            tester.cfg.BB_STD = std
            df_calc = tester.engine.calculate(df)
            
            for adx_th in adx_thresholds:
                # 2. Update ADX threshold
                tester.cfg.ADX_TREND_THRESHOLD = adx_th
                
                for sl in sl_pcts:
                    for tp in tp_pcts:
                        # 3. Update SL/TP percentages
                        tester.cfg.SL_PCT = sl
                        tester.cfg.TP_PCT = tp
                        
                        # Reset risk manager
                        risk = RiskManager(tester.cfg)
                        risk.cfg.MAX_MARGIN_RATIO = 0.40
                        
                        # Generate signals
                        signals = tester.engine.generate_signals(df_calc, "SOL/USDT:USDT")
                        
                        # Simulate trades
                        trades = []
                        wins = 0
                        
                        for signal in signals:
                            # Apply SL and TP multipliers dynamically based on this combination
                            signal.stop_loss = signal.entry_price * (1.0 - sl if signal.direction == Direction.LONG else 1.0 + sl)
                            signal.take_profit = signal.entry_price * (1.0 + tp if signal.direction == Direction.LONG else 1.0 - tp)
                            
                            slippage = tester.cfg.SLIPPAGE * signal.entry_price
                            actual_entry = signal.entry_price + (slippage if signal.direction == Direction.LONG else -slippage)

                            future = df_calc[df_calc.index > signal.timestamp]
                            if len(future) == 0:
                                continue

                            exit_price = None
                            exit_reason = None

                            for _, row in future.iterrows():
                                if signal.direction == Direction.LONG:
                                    if row['low'] <= signal.stop_loss:
                                        exit_price = signal.stop_loss
                                        exit_reason = 'STOP_LOSS'
                                        break
                                    if row['high'] >= signal.take_profit:
                                        exit_price = signal.take_profit
                                        exit_reason = 'TAKE_PROFIT'
                                        break
                                else:
                                    if row['high'] >= signal.stop_loss:
                                        exit_price = signal.stop_loss
                                        exit_reason = 'STOP_LOSS'
                                        break
                                    if row['low'] <= signal.take_profit:
                                        exit_price = signal.take_profit
                                        exit_reason = 'TAKE_PROFIT'
                                        break

                            if exit_price is None:
                                continue

                            pos = risk.calculate_position(signal)
                            if pos is None:
                                continue

                            if signal.direction == Direction.LONG:
                                pnl = (exit_price - actual_entry) * pos['qty']
                            else:
                                pnl = (actual_entry - exit_price) * pos['qty']

                            fees = (actual_entry * pos['qty'] * tester.cfg.MAKER_FEE + 
                                    exit_price * pos['qty'] * tester.cfg.TAKER_FEE)

                            hold_periods = 1
                            funding = actual_entry * pos['qty'] * tester.cfg.FUNDING_PER_8H * hold_periods

                            net_pnl = pnl - fees - funding
                            risk.update_capital(pnl, fees, funding)
                            
                            trades.append(net_pnl)
                            if net_pnl > 0:
                                wins += 1

                        total_return = (risk.capital - tester.cfg.INITIAL_CAPITAL) / tester.cfg.INITIAL_CAPITAL
                        win_rate = wins / len(trades) if trades else 0.0
                        
                        results.append({
                            'bb_period': period,
                            'bb_std': std,
                            'adx_threshold': adx_th,
                            'sl_pct': sl,
                            'tp_pct': tp,
                            'return': total_return,
                            'trades': len(trades),
                            'win_rate': win_rate
                        })
                        count += 1
            print(f"Progress: Completed BB Window={period}, BB Std={std}. Total tested: {count}/{total}")

    # Sort results by return
    results.sort(key=lambda x: x['return'], reverse=True)
    
    print("\n" + "="*80)
    print("TOP 15 PARAMETER CONFIGURATIONS FOR SOL/USDT:USDT")
    print("="*80)
    print(f"{'Rank':<5}{'BB Per':<8}{'BB Std':<8}{'ADX Lim':<8}{'SL%':<8}{'TP%':<8}{'Return':<10}{'Trades':<8}{'Win Rate':<8}")
    print("-"*80)
    for idx, r in enumerate(results[:15]):
        print(f"#{idx+1:<4}{r['bb_period']:<8}{r['bb_std']:<8}{r['adx_threshold']:<8}{r['sl_pct']*100:>5.1f}%{r['tp_pct']*100:>7.1f}%{r['return']*100:>8.1f}%  {r['trades']:<8}{r['win_rate']*100:>6.1f}%")
    print("="*80)

if __name__ == "__main__":
    main()
