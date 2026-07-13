"""
ARES MEAN REVERSION BOT - Complete Toolkit
============================================
Includes:
  1. Monte Carlo Risk Engine
  2. Historical Backtester (Bollinger + RSI + ADX Regime Filter)
  3. Live Execution Framework (CCXT)
  4. Risk Manager with Circuit Breakers

Requirements:
  pip install ccxt pandas numpy matplotlib

Usage:
  python3 ares_bot_toolkit.py --mode backtest --symbol BTC/USDT:USDT --days 90
  python3 ares_bot_toolkit.py --mode montecarlo --runs 5000
  python3 ares_bot_toolkit.py --mode live --symbol BTC/USDT:USDT --sandbox True
"""

import argparse
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple
from enum import Enum
from datetime import datetime, timedelta
import ccxt
import time
import json
import logging
import os

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(message)s')
logger = logging.getLogger(__name__)

# ============================================================
# CONFIGURATION
# ============================================================

class Config:
    # Risk Parameters
    INITIAL_CAPITAL = 1000.0
    RISK_PER_TRADE = 0.02          # 2% of capital
    MAX_LEVERAGE = 5.0             # 5x max
    MAX_DAILY_DRAWDOWN = 0.05      # 5% kill switch
    MAX_CORRELATED_POS = 2         # Max 2 same-direction in correlated group
    MAX_MARGIN_RATIO = 0.45        # 45% max margin allocation per position
    SL_PCT = 0.015                 # 1.5% stop loss
    TP_PCT = 0.035                 # 3.5% take profit

    # Strategy Parameters
    BB_PERIOD = 20
    BB_STD = 2.0
    RSI_PERIOD = 14
    RSI_OVERBOUGHT = 70
    RSI_OVERSOLD = 30
    ADX_PERIOD = 14
    ADX_TREND_THRESHOLD = 25       # Skip if ADX > 25 (trending)
    TIMEFRAME = '15m'

    # Execution
    MAKER_FEE = 0.0002             # 0.02%
    TAKER_FEE = 0.0005             # 0.05%
    SLIPPAGE = 0.0005              # 0.05% estimated slippage
    FUNDING_PER_8H = 0.0006        # ~0.06% per 8h at 5x

    # Correlated Groups
    CORRELATED_ASSETS = [
        ['BTC/USDT:USDT', 'ETH/USDT:USDT', 'SOL/USDT:USDT'],
        ['DOGE/USDT:USDT', 'SHIB/USDT:USDT'],
    ]

    # Symbol-specific parameter maps
    SYMBOL_PARAMS = {
        'BTC/USDT:USDT': {
            'BB_PERIOD': 15,
            'BB_STD': 2.5,
            'ADX_TREND_THRESHOLD': 20,
            'SL_PCT': 0.015,
            'TP_PCT': 0.035
        },
        'ETH/USDT:USDT': {
            'BB_PERIOD': 20,
            'BB_STD': 1.5,
            'ADX_TREND_THRESHOLD': 30,
            'SL_PCT': 0.015,
            'TP_PCT': 0.035
        },
        'SOL/USDT:USDT': {
            'BB_PERIOD': 20,
            'BB_STD': 3.0,
            'ADX_TREND_THRESHOLD': 30,
            'SL_PCT': 0.025,
            'TP_PCT': 0.08
        },
        'XRP/USDT:USDT': {
            'BB_PERIOD': 25,
            'BB_STD': 2.0,
            'ADX_TREND_THRESHOLD': 20,
            'SL_PCT': 0.015,
            'TP_PCT': 0.035
        }
    }

# ============================================================
# ENUMS & DATA CLASSES
# ============================================================

class Direction(Enum):
    LONG = 1
    SHORT = -1
    FLAT = 0

@dataclass
class Signal:
    symbol: str
    direction: Direction
    entry_price: float
    stop_loss: float
    take_profit: float
    confidence: float
    timestamp: pd.Timestamp
    regime: str = "unknown"

@dataclass
class Trade:
    symbol: str
    direction: str
    entry_price: float
    exit_price: float
    quantity: float
    pnl: float
    fees: float
    funding: float
    exit_reason: str
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    max_drawdown: float = 0.0

@dataclass
class BacktestResult:
    final_capital: float
    total_return: float
    max_drawdown: float
    win_rate: float
    profit_factor: float
    sharpe: float
    num_trades: int
    trades: List[Trade]
    equity_curve: pd.Series

# ============================================================
# INDICATORS
# ============================================================

class Indicators:
    @staticmethod
    def bollinger_bands(close: pd.Series, period: int = 20, std: float = 2.0) -> Tuple[pd.Series, pd.Series, pd.Series]:
        sma = close.rolling(period).mean()
        sigma = close.rolling(period).std()
        upper = sma + sigma * std
        lower = sma - sigma * std
        return sma, upper, lower

    @staticmethod
    def rsi(close: pd.Series, period: int = 14) -> pd.Series:
        delta = close.diff()
        gain = delta.where(delta > 0, 0).rolling(period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(period).mean()
        rs = gain / loss
        return 100 - (100 / (1 + rs))

    @staticmethod
    def adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
        plus_dm = high.diff().clip(lower=0)
        minus_dm = (-low.diff()).clip(lower=0)

        tr1 = high - low
        tr2 = (high - close.shift()).abs()
        tr3 = (low - close.shift()).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

        atr = tr.rolling(period).mean()
        plus_di = 100 * (plus_dm.rolling(period).mean() / atr)
        minus_di = 100 * (minus_dm.rolling(period).mean() / atr)
        dx = ( (plus_di - minus_di).abs() / (plus_di + minus_di) ) * 100
        return dx.rolling(period).mean()

# ============================================================
# STRATEGY ENGINE
# ============================================================

class MeanReversionEngine:
    def __init__(self, config: Config = Config()):
        self.cfg = config
        self.indicators = Indicators()

    def get_symbol_config(self, symbol: str):
        cfg = self.cfg
        if not symbol:
            return cfg
        
        def get_base_asset(s: str) -> str:
            s = s.upper()
            if "/" in s:
                return s.split("/")[0]
            if s.endswith("USDT"):
                return s[:-4]
            return s
            
        base_asset = get_base_asset(symbol)
        
        if hasattr(cfg, 'SYMBOL_PARAMS'):
            for sym_key, params in cfg.SYMBOL_PARAMS.items():
                if get_base_asset(sym_key) == base_asset:
                    class SymbolConfig:
                        def __init__(self, base_cfg, sym_params):
                            for attr in dir(base_cfg):
                                if not attr.startswith('__') and not callable(getattr(base_cfg, attr)):
                                    setattr(self, attr, getattr(base_cfg, attr))
                            for k, v in sym_params.items():
                                setattr(self, k, v)
                    return SymbolConfig(cfg, params)
        return cfg

    def calculate(self, df: pd.DataFrame, symbol: str = None) -> pd.DataFrame:
        df = df.copy()
        cfg = self.get_symbol_config(symbol) if symbol else self.cfg
        df['sma'], df['upper_band'], df['lower_band'] = self.indicators.bollinger_bands(
            df['close'], cfg.BB_PERIOD, cfg.BB_STD
        )
        df['rsi'] = self.indicators.rsi(df['close'], cfg.RSI_PERIOD)
        df['adx'] = self.indicators.adx(df['high'], df['low'], df['close'], cfg.ADX_PERIOD)
        return df

    def generate_signals(self, df: pd.DataFrame, symbol: str) -> List[Signal]:
        signals = []
        cfg = self.get_symbol_config(symbol)
        if len(df) < cfg.BB_PERIOD + 5:
            return signals

        # Speed up by using numpy arrays to bypass slow pandas series indexing
        closes = df['close'].values
        lows = df['low'].values
        highs = df['high'].values
        lower_bands = df['lower_band'].values
        upper_bands = df['upper_band'].values
        rsis = df['rsi'].values
        adxs = df['adx'].values
        timestamps = df.index

        for i in range(cfg.BB_PERIOD + 1, len(df)):
            adx_val = adxs[i]
            if adx_val > cfg.ADX_TREND_THRESHOLD:
                continue

            regime = "ranging" if adx_val < 20 else "neutral"

            prev_close = closes[i-1]
            prev_lower = lower_bands[i-1]
            prev_upper = upper_bands[i-1]
            
            close_val = closes[i]
            low_val = lows[i]
            high_val = highs[i]
            lower_band = lower_bands[i]
            upper_band = upper_bands[i]
            rsi_val = rsis[i]

            # LONG: Price touches lower band + RSI oversold
            if low_val <= lower_band and rsi_val < cfg.RSI_OVERSOLD:
                if prev_close > prev_lower:
                    entry = close_val
                    sl = entry * (1.0 - cfg.SL_PCT)
                    tp = entry * (1.0 + cfg.TP_PCT)
                    signals.append(Signal(
                        symbol=symbol, direction=Direction.LONG,
                        entry_price=round(entry, 2), stop_loss=round(sl, 2),
                        take_profit=round(tp, 2),
                        confidence=(cfg.RSI_OVERSOLD - rsi_val) / cfg.RSI_OVERSOLD,
                        timestamp=timestamps[i], regime=regime
                    ))

            # SHORT: Price touches upper band + RSI overbought
            elif high_val >= upper_band and rsi_val > cfg.RSI_OVERBOUGHT:
                if prev_close < prev_upper:
                    entry = close_val
                    sl = entry * (1.0 + cfg.SL_PCT)
                    tp = entry * (1.0 - cfg.TP_PCT)
                    signals.append(Signal(
                        symbol=symbol, direction=Direction.SHORT,
                        entry_price=round(entry, 2), stop_loss=round(sl, 2),
                        take_profit=round(tp, 2),
                        confidence=(rsi_val - cfg.RSI_OVERBOUGHT) / (100 - cfg.RSI_OVERBOUGHT),
                        timestamp=timestamps[i], regime=regime
                    ))

        return signals

# ============================================================
# RISK MANAGER
# ============================================================

class RiskManager:
    def __init__(self, config: Config = Config()):
        self.cfg = config
        self.capital = config.INITIAL_CAPITAL
        self.initial = config.INITIAL_CAPITAL
        self.daily_pnl = 0.0
        self.last_reset = pd.Timestamp.now().floor('D')
        self.active_positions: Dict[str, dict] = {}
        self.trade_history: List[Trade] = []

    def can_trade(self) -> bool:
        today = pd.Timestamp.now().floor('D')
        if today > self.last_reset:
            self.daily_pnl = 0.0
            self.last_reset = today
            logger.info(f"[DAILY RESET] Capital: ${self.capital:.2f}")

        if self.daily_pnl <= -self.initial * self.cfg.MAX_DAILY_DRAWDOWN:
            logger.warning(f"[KILL SWITCH] Daily limit hit: ${self.daily_pnl:.2f}")
            return False
        return True

    def check_correlation(self, symbol: str, direction: Direction) -> bool:
        for group in self.cfg.CORRELATED_ASSETS:
            if symbol in group:
                same_dir = sum(1 for sym, pos in self.active_positions.items()
                              if sym in group and pos['direction'] == direction)
                if same_dir >= self.cfg.MAX_CORRELATED_POS:
                    logger.info(f"[CORRELATION BLOCK] {symbol} {direction.name}")
                    return False
        return True

    def calculate_position(self, signal: Signal) -> Optional[dict]:
        risk_amount = self.capital * self.cfg.RISK_PER_TRADE
        price_risk = abs(signal.entry_price - signal.stop_loss)
        if price_risk == 0:
            return None

        notional = risk_amount / (price_risk / signal.entry_price)
        margin = notional / self.cfg.MAX_LEVERAGE

        if margin > self.capital * self.cfg.MAX_MARGIN_RATIO:
            logger.info(f"[MARGIN BLOCK] ${margin:.2f} required (limit: {self.cfg.MAX_MARGIN_RATIO * 100:.0f}%)")
            return None

        qty = notional / signal.entry_price
        return {
            'symbol': signal.symbol,
            'direction': signal.direction,
            'qty': qty,
            'margin': margin,
            'entry': signal.entry_price,
            'sl': signal.stop_loss,
            'tp': signal.take_profit,
            'risk_amount': risk_amount
        }

    def update_capital(self, pnl: float, fees: float, funding: float = 0):
        net = pnl - fees - funding
        self.capital += net
        self.daily_pnl += net

# ============================================================
# BACKTESTER
# ============================================================

class Backtester:
    def __init__(self, config: Config = Config()):
        self.cfg = config
        self.engine = MeanReversionEngine(config)
        self.risk = RiskManager(config)
        self.exchange = ccxt.binance({'enableRateLimit': True, 'options': {'defaultType': 'swap'}})

    def fetch_data(self, symbol: str, days: int = 90) -> pd.DataFrame:
        since = self.exchange.parse8601((datetime.utcnow() - timedelta(days=days)).isoformat())
        ohlcv = self.exchange.fetch_ohlcv(symbol, self.cfg.TIMEFRAME, since=since, limit=1000)

        while len(ohlcv) > 0:
            last_time = ohlcv[-1][0]
            new_ohlcv = self.exchange.fetch_ohlcv(symbol, self.cfg.TIMEFRAME, since=last_time + 1, limit=1000)
            if len(new_ohlcv) <= 1:
                break
            ohlcv.extend(new_ohlcv[1:])

        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df.set_index('timestamp', inplace=True)
        return df

    def run(self, symbol: str, days: int = 90, use_fees: bool = True) -> BacktestResult:
        logger.info(f"[BACKTEST] Fetching {days} days of {symbol}...")
        df = self.fetch_data(symbol, days)
        logger.info(f"[BACKTEST] {len(df)} candles loaded")
        return self.run_with_data(df, symbol)

    def run_with_data(self, df: pd.DataFrame, symbol: str) -> BacktestResult:
        self.risk = RiskManager(self.cfg)
        df = self.engine.calculate(df, symbol)
        signals = self.engine.generate_signals(df, symbol)

        trades = []
        equity = [self.cfg.INITIAL_CAPITAL]

        for signal in signals:
            slippage = self.cfg.SLIPPAGE * signal.entry_price
            actual_entry = signal.entry_price + (slippage if signal.direction == Direction.LONG else -slippage)

            future = df[df.index > signal.timestamp]
            if len(future) == 0:
                continue

            exit_price = None
            exit_reason = None
            max_dd = 0.0

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
                    dd = (actual_entry - row['low']) / actual_entry
                    if dd > max_dd:
                        max_dd = dd
                else:
                    if row['high'] >= signal.stop_loss:
                        exit_price = signal.stop_loss
                        exit_reason = 'STOP_LOSS'
                        break
                    if row['low'] <= signal.take_profit:
                        exit_price = signal.take_profit
                        exit_reason = 'TAKE_PROFIT'
                        break
                    dd = (row['high'] - actual_entry) / actual_entry
                    if dd > max_dd:
                        max_dd = dd

            if exit_price is None:
                continue

            pos = self.risk.calculate_position(signal)
            if pos is None:
                continue

            if signal.direction == Direction.LONG:
                pnl = (exit_price - actual_entry) * pos['qty']
            else:
                pnl = (actual_entry - exit_price) * pos['qty']

            fees = (actual_entry * pos['qty'] * self.cfg.MAKER_FEE + 
                    exit_price * pos['qty'] * self.cfg.TAKER_FEE)

            hold_periods = 1
            funding = actual_entry * pos['qty'] * self.cfg.FUNDING_PER_8H * hold_periods

            net_pnl = pnl - fees - funding
            self.risk.update_capital(pnl, fees, funding)

            trades.append(Trade(
                symbol=symbol, direction=signal.direction.name,
                entry_price=actual_entry, exit_price=exit_price,
                quantity=pos['qty'], pnl=pnl, fees=fees, funding=funding,
                exit_reason=exit_reason, entry_time=signal.timestamp,
                exit_time=future.index[0] if len(future) > 0 else signal.timestamp,
                max_drawdown=max_dd
            ))

            equity.append(self.risk.capital)

        equity_series = pd.Series(equity)
        returns = equity_series.pct_change().dropna()

        wins = [t for t in trades if t.pnl > 0]
        losses = [t for t in trades if t.pnl <= 0]

        total_profit = sum(t.pnl for t in wins) if wins else 0
        total_loss = abs(sum(t.pnl for t in losses)) if losses else 1e-9

        result = BacktestResult(
            final_capital=self.risk.capital,
            total_return=(self.risk.capital - self.cfg.INITIAL_CAPITAL) / self.cfg.INITIAL_CAPITAL,
            max_drawdown=self._calc_max_dd(equity_series),
            win_rate=len(wins) / len(trades) if trades else 0,
            profit_factor=total_profit / total_loss,
            sharpe=returns.mean() / returns.std() * np.sqrt(96) if len(returns) > 1 and returns.std() > 0 else 0,
            num_trades=len(trades),
            trades=trades,
            equity_curve=equity_series
        )

        return result

    @staticmethod
    def _calc_max_dd(equity: pd.Series) -> float:
        peak = equity.expanding().max()
        dd = (equity - peak) / peak
        return abs(dd.min())

    def report(self, result: BacktestResult):
        print("\n" + "=" * 50)
        print("BACKTEST REPORT")
        print("=" * 50)
        print(f"Final Capital:      ${result.final_capital:,.2f}")
        print(f"Total Return:       {result.total_return*100:.1f}%")
        print(f"Max Drawdown:       {result.max_drawdown*100:.1f}%")
        print(f"Win Rate:           {result.win_rate*100:.1f}%")
        print(f"Profit Factor:      {result.profit_factor:.2f}")
        print(f"Sharpe (daily):     {result.sharpe:.2f}")
        print(f"Total Trades:       {result.num_trades}")
        print(f"Avg Trade:          ${sum(t.pnl for t in result.trades)/result.num_trades:.2f}" if result.num_trades > 0 else "N/A")
        print("=" * 50)

# ============================================================
# MONTE CARLO ENGINE (Standalone)
# ============================================================

class MonteCarloEngine:
    def __init__(self, initial: float = 1000, risk: float = 0.02, rr: float = 2.3,
                 win_rate: float = 0.55, n_trades: int = 200, 
                 kill_switch: float = 0.05, tpd: float = 2.0):
        self.initial = initial
        self.risk = risk
        self.rr = rr
        self.win_rate = win_rate
        self.n_trades = n_trades
        self.kill = kill_switch
        self.tpd = tpd

    def run(self, n_runs: int = 1000):
        results = []
        for _ in range(n_runs):
            cap = self.initial
            equity = [cap]
            daily_pnl = 0
            trades_today = 0

            for i in range(self.n_trades):
                if trades_today >= self.tpd:
                    daily_pnl = 0
                    trades_today = 0
                if daily_pnl <= -self.initial * self.kill:
                    break

                won = np.random.random() < self.win_rate
                risk_amt = cap * self.risk
                pnl = risk_amt * self.rr if won else -risk_amt
                cap += pnl
                daily_pnl += pnl
                trades_today += 1
                equity.append(cap)
                if cap <= 0:
                    break

            results.append({
                'final': cap,
                'return': (cap - self.initial) / self.initial,
                'ruined': cap <= 0
            })

        df = pd.DataFrame(results)
        print(f"\nMonte Carlo ({n_runs} runs, {self.n_trades} trades)")
        print(f"Win Rate: {self.win_rate*100:.0f}% | R:R: 1:{self.rr}")
        print(f"Median Final: ${df['final'].median():,.0f}")
        print(f"Mean Final:   ${df['final'].mean():,.0f}")
        print(f"Ruin Rate:    {df['ruined'].mean()*100:.1f}%")
        print(f"Prob >$2000:  {(df['final'] > 2000).mean()*100:.1f}%")
        return df

# ============================================================
# LIVE EXECUTION ENGINE
# ============================================================

class ExecutionManager:
    def __init__(self, config: Config = Config(), sandbox: bool = True):
        self.cfg = config
        self.engine = MeanReversionEngine(config)
        self.risk = RiskManager(config)
        
        # Load API keys from env variables
        api_key = os.getenv("BINANCE_API_KEY", "")
        api_secret = os.getenv("BINANCE_API_SECRET", "")
        
        if not api_key or not api_secret:
            raise ValueError(
                "\n" + "!" * 80 + "\n"
                "❌ [LIVE] MISSING API CREDENTIALS!\n"
                "Please configure 'BINANCE_API_KEY' and 'BINANCE_API_SECRET' in your environment or '.env' file.\n"
                "For Sandbox mode, obtain testnet API keys from: https://testnet.binancefuture.com/\n" +
                "!" * 80 + "\n"
            )
        
        options = {
            'enableRateLimit': True,
            'options': {
                'defaultType': 'swap'  # Linear USD-M Futures
            },
            'apiKey': api_key,
            'secret': api_secret
        }
            
        self.exchange = ccxt.binance(options)
        
        if sandbox:
            self.exchange.set_sandbox_mode(True)
            logger.info("[LIVE] Sandbox mode activated.")
        else:
            logger.warning("⚠️ [LIVE] REAL PRODUCTION TRADING ACTIVE!")

    def run_live(self, symbol: str):
        logger.info(f"[LIVE] Initializing live engine loop for {symbol}...")
        
        try:
            self.exchange.set_leverage(int(self.cfg.MAX_LEVERAGE), symbol)
            logger.info(f"[LIVE] Successfully set leverage to {self.cfg.MAX_LEVERAGE}x")
        except Exception as e:
            logger.warning(f"[LIVE] Leverage setup ignored or failed: {e}")

        try:
            balance = self.exchange.fetch_balance()
            free_margin = balance['free'].get('USDT', 0.0)
            self.risk.capital = free_margin
            self.risk.initial = free_margin
            logger.info(f"[LIVE] Current Wallet Free Margin: ${free_margin:.2f} USDT")
        except Exception as e:
            logger.error(f"[LIVE] Authentication/Connection check failed: {e}")
            return

        logger.info("[LIVE] Listening for ticker data updates...")
        while True:
            try:
                # Circuit Breaker check
                if not self.risk.can_trade():
                    logger.warning("[LIVE] Risk engine kill-switch triggered. Trading halted.")
                    time.sleep(60)
                    continue
                
                # Fetch recent candles
                ohlcv = self.exchange.fetch_ohlcv(symbol, self.cfg.TIMEFRAME, limit=100)
                df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
                df.set_index('timestamp', inplace=True)
                
                # Calculate indicators & look for entry signals (exclude current forming candle)
                df = self.engine.calculate(df, symbol)
                signals = self.engine.generate_signals(df.iloc[:-1], symbol)
                
                if signals:
                    latest_signal = signals[-1]
                    logger.info(f"[LIVE] New signal detected: {latest_signal.direction.name} @ {latest_signal.entry_price}")
                    
                    # Verify if position is already active
                    positions = self.exchange.fetch_positions([symbol])
                    has_position = any(float(pos['contracts']) > 0 for pos in positions if pos['symbol'] == symbol)
                    
                    if has_position:
                        logger.info(f"[LIVE] Position already exists for {symbol}. Skipping signal.")
                    else:
                        # Check correlation & calculate position size
                        if self.risk.check_correlation(symbol, latest_signal.direction):
                            pos_config = self.risk.calculate_position(latest_signal)
                            if pos_config:
                                self.execute_orders(latest_signal, pos_config)
                
                # Sleep interval
                time.sleep(30)
                
            except Exception as e:
                logger.error(f"[LIVE] Run Error in loops: {e}")
                time.sleep(10)

    def execute_orders(self, signal: Signal, pos: dict):
        symbol = signal.symbol
        side = 'BUY' if signal.direction == Direction.LONG else 'SELL'
        opposite_side = 'SELL' if signal.direction == Direction.LONG else 'BUY'
        qty = pos['qty']
        
        try:
            logger.info(f"[LIVE] Executing {side} Entry Market Order...")
            entry_order = self.exchange.create_market_order(symbol, side.lower(), qty)
            fill_price = float(entry_order.get('price') or entry_order.get('average') or signal.entry_price)
            logger.info(f"[LIVE] Entry filled. Price: {fill_price}")
            
            # Place resting Stop Loss
            logger.info(f"[LIVE] Placing Stop Loss Order @ {signal.stop_loss}")
            params = {
                'stopPrice': signal.stop_loss,
                'reduceOnly': True
            }
            sl_order = self.exchange.create_order(
                symbol, 'STOP_MARKET', opposite_side.lower(), qty, params=params
            )
            logger.info(f"[LIVE] Stop Loss active. ID: {sl_order['id']}")
            
            # Place resting Take Profit
            logger.info(f"[LIVE] Placing Take Profit Limit Order @ {signal.take_profit}")
            tp_order = self.exchange.create_limit_order(
                symbol, opposite_side.lower(), qty, signal.take_profit, params={'reduceOnly': True}
            )
            logger.info(f"[LIVE] Take Profit active. ID: {tp_order['id']}")
            
            # Record active position state
            self.risk.active_positions[symbol] = {
                'direction': signal.direction,
                'qty': qty,
                'entry': fill_price,
                'sl_id': sl_order['id'],
                'tp_id': tp_order['id']
            }
            
        except Exception as e:
            logger.critical(f"[LIVE] CRITICAL: Failed order execution chain: {e}")

# ============================================================
# PARAMETER OPTIMIZER
# ============================================================

class ParameterOptimizer:
    def __init__(self, config: Config = Config()):
        self.cfg = config
        self.backtester = Backtester(config)

    def optimize(self, symbols: List[str], days: int = 90):
        # Define search grid
        bb_periods = [15, 20, 25]
        bb_stds = [1.5, 2.0, 2.5]
        adx_thresholds = [20, 25, 30]

        best_results = {}

        for symbol in symbols:
            logger.info(f"\n[OPTIMIZER] Starting optimization for {symbol} over {days} days...")
            try:
                df = self.backtester.fetch_data(symbol, days)
            except Exception as e:
                logger.error(f"[OPTIMIZER] Failed to fetch data for {symbol}: {e}")
                continue

            logger.info(f"[OPTIMIZER] {len(df)} candles loaded. Running grid search...")
            
            results = []

            for period in bb_periods:
                for std in bb_stds:
                    for adx_th in adx_thresholds:
                        cfg_combo = Config()
                        cfg_combo.BB_PERIOD = period
                        cfg_combo.BB_STD = std
                        cfg_combo.ADX_TREND_THRESHOLD = adx_th
                        cfg_combo.MAX_MARGIN_RATIO = self.cfg.MAX_MARGIN_RATIO
                        cfg_combo.INITIAL_CAPITAL = self.cfg.INITIAL_CAPITAL
                        cfg_combo.RISK_PER_TRADE = self.cfg.RISK_PER_TRADE
                        cfg_combo.MAX_LEVERAGE = self.cfg.MAX_LEVERAGE

                        bt = Backtester(cfg_combo)
                        res = bt.run_with_data(df, symbol)

                        results.append({
                            'bb_period': period,
                            'bb_std': std,
                            'adx_threshold': adx_th,
                            'return': res.total_return,
                            'sharpe': res.sharpe,
                            'max_drawdown': res.max_drawdown,
                            'trades': res.num_trades,
                            'win_rate': res.win_rate
                        })

            # Rank by total return (and Sharpe ratio if returns are identical)
            results.sort(key=lambda x: (x['return'], x['sharpe']), reverse=True)
            best_results[symbol] = results[:5]

            # Print report for symbol
            print("\n" + "=" * 70)
            print(f"OPTIMIZATION RESULTS FOR {symbol}")
            print("=" * 70)
            print(f"{'Rank':<5}{'BB Period':<10}{'BB Std':<8}{'ADX Limit':<10}{'Return':<10}{'Sharpe':<8}{'Trades':<8}{'Win Rate':<8}")
            print("-" * 70)
            for idx, r in enumerate(results[:5]):
                print(f"#{idx+1:<4}{r['bb_period']:<10}{r['bb_std']:<8}{r['adx_threshold']:<10}{r['return']*100:>6.1f}%  {r['sharpe']:>6.2f}  {r['trades']:<8}{r['win_rate']*100:>6.1f}%")
            print("=" * 70)

        return best_results

# ============================================================
# MAIN CLI
# ============================================================

def main():
    parser = argparse.ArgumentParser(description='Ares Mean Reversion Bot')
    parser.add_argument('--mode', choices=['backtest', 'montecarlo', 'live', 'optimize'], required=True)
    parser.add_argument('--symbol', default='BTC/USDT:USDT')
    parser.add_argument('--days', type=int, default=90)
    parser.add_argument('--runs', type=int, default=2000)
    parser.add_argument('--sandbox', type=bool, default=True)
    args = parser.parse_args()

    if args.mode == 'backtest':
        bt = Backtester()
        result = bt.run(args.symbol, args.days)
        bt.report(result)

        # Plot
        fig, axes = plt.subplots(2, 1, figsize=(12, 8))
        result.equity_curve.plot(ax=axes[0], title='Equity Curve', grid=True)
        axes[0].axhline(Config.INITIAL_CAPITAL, color='red', linestyle='--', alpha=0.5)

        if result.trades:
            reasons = pd.Series([t.exit_reason for t in result.trades]).value_counts()
            reasons.plot(kind='bar', ax=axes[1], title='Exit Reasons', color=['green', 'red'])
        else:
            axes[1].text(0.5, 0.5, 'No Trades Executed', ha='center', va='center')
            axes[1].set_title('Exit Reasons')

        plt.tight_layout()
        plt.savefig('backtest_result.png')
        print("[SAVED] backtest_result.png")

    elif args.mode == 'montecarlo':
        mc = MonteCarloEngine()
        mc.run(args.runs)

    elif args.mode == 'live':
        # Instantiates and runs the completed live execution flow
        executor = ExecutionManager(sandbox=args.sandbox)
        executor.run_live(args.symbol)

    elif args.mode == 'optimize':
        # Map simple or comma-separated symbols (e.g. BTCUSDT,ETHUSDT)
        raw_symbols = args.symbol.split(",")
        symbols = []
        for sym in raw_symbols:
            sym = sym.strip()
            if "/" not in sym and sym.endswith("USDT"):
                base = sym.replace("USDT", "")
                mapped = f"{base}/USDT:USDT"
                symbols.append(mapped)
            else:
                symbols.append(sym)

        optimizer = ParameterOptimizer()
        optimizer.optimize(symbols, args.days)

if __name__ == "__main__":
    main()
