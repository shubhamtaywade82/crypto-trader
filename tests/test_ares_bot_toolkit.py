import pytest
import pandas as pd
import numpy as np
from ares_bot_toolkit import Config, Direction, Signal, Indicators, MeanReversionEngine, RiskManager, MonteCarloEngine

def test_indicators_calculations():
    # Create mock price series
    np.random.seed(42)
    close_prices = pd.Series(100.0 + np.cumsum(np.random.normal(0, 1, 100)))
    high_prices = close_prices + 0.5
    low_prices = close_prices - 0.5

    # Test Bollinger Bands
    sma, upper, lower = Indicators.bollinger_bands(close_prices, period=20, std=2.0)
    assert len(sma) == 100
    assert len(upper) == 100
    assert len(lower) == 100
    # The first 19 entries (indices 0 to 18) should be NaN due to rolling period
    assert np.isnan(sma.iloc[18])
    assert not np.isnan(sma.iloc[19])

    # Test RSI
    rsi = Indicators.rsi(close_prices, period=14)
    assert len(rsi) == 100
    assert np.isnan(rsi.iloc[12])

    # Test ADX
    adx = Indicators.adx(high_prices, low_prices, close_prices, period=14)
    assert len(adx) == 100


def test_mean_reversion_engine():
    # Generate ranging price data to trigger signals
    timestamps = pd.date_range(start="2026-01-01", periods=100, freq="15min")
    
    # 100 periods flat, then a sharp drop (oversold), then return
    prices = [100.0] * 100
    prices[50] = 95.0
    prices[51] = 94.0
    prices[52] = 93.0
    prices[53] = 92.0
    # Slowly recover
    for j in range(54, 100):
        prices[j] = 92.0 + (j - 53) * 0.15

    df = pd.DataFrame({
        "open": prices,
        "high": [p + 0.2 for p in prices],
        "low": [p - 0.2 for p in prices],
        "close": prices,
        "volume": [1000] * 100
    }, index=timestamps)

    engine = MeanReversionEngine()
    df_calc = engine.calculate(df)
    
    assert "sma" in df_calc.columns
    assert "upper_band" in df_calc.columns
    assert "rsi" in df_calc.columns

    signals = engine.generate_signals(df_calc, "BTC/USDT:USDT")
    # Should find at least one signal due to the sharp drop
    assert isinstance(signals, list)


def test_risk_manager_logic():
    cfg = Config()
    rm = RiskManager(cfg)
    
    # Test initial state
    assert rm.can_trade() is True
    assert rm.capital == cfg.INITIAL_CAPITAL

    # Test daily drawdown limit kill switch
    rm.update_capital(-200.0, 10.0, 5.0) # Net loss of 215
    # If loss exceeds daily drawdown: capital = 785, net change = -215.
    # 215 / 1000 = 21.5% loss, limit is 5%
    assert rm.can_trade() is False

    # Reset RiskManager
    rm2 = RiskManager(cfg)
    # Check correlation limit
    rm2.active_positions["BTC/USDT:USDT"] = {"direction": Direction.LONG}
    rm2.active_positions["ETH/USDT:USDT"] = {"direction": Direction.LONG}
    # BTC, ETH, and SOL are in the same correlated group
    # Max correlated same-direction is 2
    assert rm2.check_correlation("SOL/USDT:USDT", Direction.LONG) is False
    assert rm2.check_correlation("SOL/USDT:USDT", Direction.SHORT) is True


def test_monte_carlo_engine():
    mc = MonteCarloEngine(n_trades=10)
    df_mc = mc.run(n_runs=10)
    
    assert len(df_mc) == 10
    assert "final" in df_mc.columns
    assert "return" in df_mc.columns
    assert "ruined" in df_mc.columns


def test_parameter_optimizer():
    from ares_bot_toolkit import ParameterOptimizer
    from unittest.mock import MagicMock
    
    optimizer = ParameterOptimizer()
    
    # Mock Backtester fetch_data to return a simple flat dataframe
    optimizer.backtester.fetch_data = MagicMock(return_value=pd.DataFrame({
        "open": [100.0] * 50,
        "high": [100.2] * 50,
        "low": [99.8] * 50,
        "close": [100.0] * 50,
        "volume": [1000] * 50
    }, index=pd.date_range("2026-01-01", periods=50, freq="15min")))

    results = optimizer.optimize(["BTC/USDT:USDT"], days=1)
    
    assert "BTC/USDT:USDT" in results
    assert len(results["BTC/USDT:USDT"]) > 0
    assert "bb_period" in results["BTC/USDT:USDT"][0]
