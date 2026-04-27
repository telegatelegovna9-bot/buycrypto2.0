"""
Backtest Runner Script.
Run backtests on historical data for individual strategies or the full system.
"""
import asyncio
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import ccxt.async_support as ccxt
import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import BotConfig, get_default_config
from backtest.backtest_engine import BacktestEngine
from strategies.trend_breakout import TrendBreakoutStrategy
from strategies.range_trading import RangeTradingStrategy
from strategies.volume_oi_strategies import VolumeStrategy, OpenInterestStrategy
from strategies.volatility_breakout import VolatilityBreakoutStrategy


async def fetch_historical_data(
    symbol: str = "BTC/USDT:USDT",
    timeframe: str = "1h",
    limit: int = 1000,
    exchange_id: str = "binance"
) -> pd.DataFrame:
    """Fetch historical OHLCV data from exchange."""
    exchange = getattr(ccxt, exchange_id)({
        'enableRateLimit': True,
        'options': {'defaultType': 'future'}
    })
    
    try:
        ohlcv = await exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        
        df = pd.DataFrame(
            ohlcv,
            columns=['timestamp', 'open', 'high', 'low', 'close', 'volume']
        )
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df.set_index('timestamp', inplace=True)
        
        return df
    
    finally:
        await exchange.close()


def run_strategy_backtest(strategy, df: pd.DataFrame, config: BotConfig):
    """Run backtest for a single strategy."""
    engine = BacktestEngine(config)
    
    results = engine.run_backtest(df, strategy)
    
    return results, engine


async def main():
    """Run backtests for all strategies."""
    print("=" * 60)
    print("CRYPTO TRADING BOT - BACKTEST RUNNER")
    print("=" * 60)
    
    # Configuration
    config = get_default_config()
    config.backtest.initial_balance = 10000.0
    config.backtest.commission_rate = 0.0004
    config.backtest.slippage_rate = 0.0001
    
    # Fetch historical data
    print("\nFetching historical data...")
    df = await fetch_historical_data(
        symbol="BTC/USDT:USDT",
        timeframe="1h",
        limit=500
    )
    
    if len(df) < 100:
        print("Insufficient data for backtesting")
        return
    
    print(f"Loaded {len(df)} bars from {df.index[0]} to {df.index[-1]}")
    print(f"Price range: ${df['close'].min():.2f} - ${df['close'].max():.2f}")
    
    # Initialize strategies
    strategies = [
        TrendBreakoutStrategy({'ema_fast': 9, 'ema_slow': 21}),
        RangeTradingStrategy({'rsi_period': 14, 'range_window': 20}),
        VolumeStrategy({'volume_ma_period': 20, 'volume_spike_threshold': 2.0}),
        OpenInterestStrategy({'oi_lookback': 5}),
        VolatilityBreakoutStrategy({'bb_period': 20, 'bb_std': 2.0})
    ]
    
    # Run backtest for each strategy
    all_results = {}
    
    for strategy in strategies:
        print(f"\n{'='*60}")
        print(f"Testing Strategy: {strategy.name}")
        print(f"{'='*60}")
        
        results, engine = run_strategy_backtest(strategy, df.copy(), config)
        engine.print_results(results)
        
        all_results[strategy.name] = results
    
    # Compare strategies
    print("\n" + "=" * 60)
    print("STRATEGY COMPARISON")
    print("=" * 60)
    
    comparison_data = []
    
    for name, results in all_results.items():
        comparison_data.append({
            'Strategy': name,
            'Total Return': f"{results['total_return']*100:.2f}%",
            'Win Rate': f"{results['win_rate']*100:.1f}%",
            'Profit Factor': f"{results['profit_factor']:.2f}",
            'Max DD': f"{results['max_drawdown']*100:.1f}%",
            'Trades': results['total_trades'],
            'Sharpe': f"{results['sharpe_ratio']:.2f}"
        })
    
    comparison_df = pd.DataFrame(comparison_data)
    print(comparison_df.to_string(index=False))
    
    # Find best strategy
    best_strategy = max(all_results.keys(), key=lambda x: all_results[x]['total_return'])
    print(f"\n🏆 Best performing strategy: {best_strategy}")
    print(f"   Total Return: {all_results[best_strategy]['total_return']*100:.2f}%")
    
    # Save results
    save_results = input("\nSave detailed results to CSV? (y/n): ")
    if save_results.lower() == 'y':
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        for name, results in all_results.items():
            trades_df = pd.DataFrame(results['trades'])
            if not trades_df.empty:
                trades_df.to_csv(f"backtest/results_{name}_{timestamp}.csv", index=False)
            
            equity_df = pd.DataFrame(results['equity_curve'])
            if not equity_df.empty:
                equity_df.to_csv(f"backtest/equity_{name}_{timestamp}.csv", index=False)
        
        print("Results saved to backtest/ directory")


if __name__ == "__main__":
    asyncio.run(main())
