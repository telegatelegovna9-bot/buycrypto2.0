"""
Backtesting Engine.
Tests strategies on historical data with realistic assumptions.
"""
import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Tuple
from datetime import datetime
import logging

from config.settings import BotConfig
from risk_manager import RiskManager, Position
from strategies.base_strategy import Signal

logger = logging.getLogger(__name__)


class BacktestEngine:
    """
    Backtesting engine for testing strategies on historical data.
    Includes realistic commission, slippage, and execution simulation.
    """
    
    def __init__(self, config: BotConfig):
        self.config = config
        self.backtest_config = config.backtest
        
        self.initial_balance = self.backtest_config.initial_balance
        self.commission_rate = self.backtest_config.commission_rate
        self.slippage_rate = self.backtest_config.slippage_rate
        
        # State variables
        self.balance = self.initial_balance
        self.positions: Dict[str, Position] = {}
        self.trades = []
        self.equity_curve = []
        
        # Metrics
        self.total_trades = 0
        self.winning_trades = 0
        self.losing_trades = 0
        self.total_pnl = 0.0
        self.max_drawdown = 0.0
        self.peak_balance = self.initial_balance
    
    def reset(self):
        """Reset backtest state."""
        self.balance = self.initial_balance
        self.positions = {}
        self.trades = []
        self.equity_curve = []
        self.total_trades = 0
        self.winning_trades = 0
        self.losing_trades = 0
        self.total_pnl = 0.0
        self.max_drawdown = 0.0
        self.peak_balance = self.initial_balance
    
    def _apply_slippage(self, price: float, direction: str) -> float:
        """Apply slippage to execution price."""
        slippage = price * self.slippage_rate
        if direction == 'long':
            return price + slippage  # Buy slightly higher
        else:
            return price - slippage  # Sell slightly lower
    
    def _calculate_commission(self, value: float) -> float:
        """Calculate commission for a trade."""
        return value * self.commission_rate
    
    def execute_signal(
        self, 
        signal: Signal, 
        df: pd.DataFrame,
        position_size: float,
        leverage: int = 1
    ) -> bool:
        """Execute a signal in backtest environment."""
        symbol = signal.symbol
        timestamp = signal.timestamp or df.index[-1]
        
        # Get current bar
        current_bar = df.loc[timestamp] if timestamp in df.index else df.iloc[-1]
        
        # Apply slippage to entry
        entry_price = self._apply_slippage(signal.entry_price, signal.direction)
        
        # Calculate position value
        position_value = entry_price * position_size * leverage
        
        # Check if we have enough balance
        if position_value > self.balance:
            logger.debug(f"Insufficient balance for {symbol}")
            return False
        
        # Create position
        position = Position(
            symbol=symbol,
            direction=signal.direction,
            entry_price=entry_price,
            size=position_size,
            leverage=leverage,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit
        )
        
        self.positions[symbol] = position
        
        # Record commission
        commission = self._calculate_commission(position_value)
        self.balance -= commission
        
        logger.debug(f"Opened {signal.direction} position on {symbol} @ {entry_price:.4f}")
        
        return True
    
    def check_exit_conditions(
        self, 
        symbol: str, 
        df: pd.DataFrame,
        current_idx: int
    ) -> Optional[Tuple[str, float]]:
        """
        Check if position should be closed.
        
        Returns:
            (reason, exit_price) or None
        """
        if symbol not in self.positions:
            return None
        
        position = self.positions[symbol]
        
        # Get current bar data
        current_bar = df.iloc[current_idx]
        high = current_bar['high']
        low = current_bar['low']
        close = current_bar['close']
        
        exit_reason = None
        exit_price = None
        
        if position.direction == 'long':
            # Check stop loss
            if low <= position.stop_loss:
                exit_reason = 'stop_loss'
                exit_price = position.stop_loss
            
            # Check take profit
            elif high >= position.take_profit:
                exit_reason = 'take_profit'
                exit_price = position.take_profit
        
        else:  # short
            # Check stop loss
            if high >= position.stop_loss:
                exit_reason = 'stop_loss'
                exit_price = position.stop_loss
            
            # Check take profit
            elif low <= position.take_profit:
                exit_reason = 'take_profit'
                exit_price = position.take_profit
        
        # Apply slippage to exit
        if exit_price:
            exit_price = self._apply_slippage(exit_price, 'sell' if position.direction == 'long' else 'buy')
        
        if exit_reason:
            return (exit_reason, exit_price)
        
        return None
    
    def close_position(
        self, 
        symbol: str, 
        exit_price: float, 
        reason: str,
        timestamp=None
    ) -> float:
        """Close a position and record the trade."""
        if symbol not in self.positions:
            return 0.0
        
        position = self.positions.pop(symbol)
        
        # Calculate PnL
        if position.direction == 'long':
            pnl = (exit_price - position.entry_price) * position.size
        else:
            pnl = (position.entry_price - exit_price) * position.size
        
        # Subtract commission
        exit_value = exit_price * position.size
        commission = self._calculate_commission(exit_value)
        pnl -= commission
        
        # Update balance
        self.balance += pnl
        self.total_pnl += pnl
        
        # Track win/loss
        self.total_trades += 1
        if pnl > 0:
            self.winning_trades += 1
        else:
            self.losing_trades += 1
        
        # Update peak and drawdown
        if self.balance > self.peak_balance:
            self.peak_balance = self.balance
        
        current_drawdown = (self.peak_balance - self.balance) / self.peak_balance
        if current_drawdown > self.max_drawdown:
            self.max_drawdown = current_drawdown
        
        # Record trade
        trade = {
            'symbol': symbol,
            'direction': position.direction,
            'entry_price': position.entry_price,
            'exit_price': exit_price,
            'entry_time': timestamp,
            'exit_time': timestamp,
            'pnl': pnl,
            'pnl_pct': pnl / (position.entry_price * position.size) if position.entry_price > 0 else 0,
            'reason': reason,
            'leverage': position.leverage,
            'balance_after': self.balance
        }
        self.trades.append(trade)
        
        logger.debug(f"Closed {symbol} {reason}: PnL={pnl:.2f}, Balance={self.balance:.2f}")
        
        return pnl
    
    def update_equity_curve(self, df: pd.DataFrame, current_idx: int):
        """Update equity curve with current portfolio value."""
        timestamp = df.index[current_idx]
        current_price = df['close'].iloc[current_idx]
        
        # Calculate total equity (balance + unrealized PnL)
        equity = self.balance
        
        for position in self.positions.values():
            if position.direction == 'long':
                unrealized = (current_price - position.entry_price) * position.size
            else:
                unrealized = (position.entry_price - current_price) * position.size
            equity += unrealized
        
        self.equity_curve.append({
            'timestamp': timestamp,
            'equity': equity,
            'balance': self.balance,
            'open_positions': len(self.positions)
        })
    
    def run_backtest(
        self, 
        df: pd.DataFrame, 
        strategy,
        market_data_provider=None
    ) -> Dict:
        """
        Run backtest on historical data.
        
        Args:
            df: OHLCV DataFrame
            strategy: Strategy object with generate_signal method
            market_data_provider: Optional function to provide additional market data
        
        Returns:
            Dictionary with backtest results
        """
        self.reset()
        
        logger.info(f"Starting backtest on {len(df)} bars")
        
        # Iterate through data
        for i in range(len(df) - 1):
            # Get data up to current point (no lookahead bias)
            historical_df = df.iloc[:i+1].copy()
            
            # Generate market data dict
            market_data = {
                'symbol': 'BACKTEST',
                'ohlcv': historical_df,
                'funding_rate': 0.0,
                'open_interest': 0.0,
                'ticker': {}
            }
            
            if market_data_provider:
                market_data.update(market_data_provider(i))
            
            # Generate signal
            try:
                signal = strategy.generate_signal(historical_df, market_data)
            except Exception as e:
                logger.error(f"Error generating signal at bar {i}: {e}")
                continue
            
            # If no position and valid signal, open position
            if signal.is_valid() and signal.symbol not in self.positions:
                # Calculate position size (simplified for backtest)
                risk_amount = self.balance * 0.01  # 1% risk
                risk_per_unit = abs(signal.entry_price - signal.stop_loss)
                
                if risk_per_unit > 0:
                    position_size = risk_amount / risk_per_unit
                    leverage = 3  # Default leverage for backtest
                    
                    self.execute_signal(signal, historical_df, position_size, leverage)
            
            # Check exits for existing positions
            positions_to_close = list(self.positions.keys())
            for symbol in positions_to_close:
                exit_info = self.check_exit_conditions(symbol, df, i)
                if exit_info:
                    reason, exit_price = exit_info
                    self.close_position(symbol, exit_price, reason, df.index[i])
            
            # Update equity curve
            self.update_equity_curve(df, i)
        
        # Close any remaining positions at last price
        last_price = df['close'].iloc[-1]
        for symbol in list(self.positions.keys()):
            self.close_position(symbol, last_price, 'end_of_data', df.index[-1])
        
        return self.get_results()
    
    def get_results(self) -> Dict:
        """Get backtest results summary."""
        win_rate = self.winning_trades / self.total_trades if self.total_trades > 0 else 0
        
        # Calculate profit factor
        gross_profit = sum(t['pnl'] for t in self.trades if t['pnl'] > 0)
        gross_loss = abs(sum(t['pnl'] for t in self.trades if t['pnl'] <= 0))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')
        
        # Calculate max consecutive wins/losses
        max_consecutive_wins = 0
        max_consecutive_losses = 0
        current_wins = 0
        current_losses = 0
        
        for trade in self.trades:
            if trade['pnl'] > 0:
                current_wins += 1
                current_losses = 0
                max_consecutive_wins = max(max_consecutive_wins, current_wins)
            else:
                current_losses += 1
                current_wins = 0
                max_consecutive_losses = max(max_consecutive_losses, current_losses)
        
        # Calculate Sharpe ratio (simplified)
        if len(self.equity_curve) > 1:
            returns = pd.Series([e['equity'] for e in self.equity_curve]).pct_change().dropna()
            sharpe = (returns.mean() / returns.std()) * np.sqrt(252) if returns.std() > 0 else 0
        else:
            sharpe = 0
        
        return {
            'initial_balance': self.initial_balance,
            'final_balance': self.balance,
            'total_pnl': self.total_pnl,
            'total_return': (self.balance - self.initial_balance) / self.initial_balance,
            'total_trades': self.total_trades,
            'winning_trades': self.winning_trades,
            'losing_trades': self.losing_trades,
            'win_rate': win_rate,
            'profit_factor': profit_factor,
            'max_drawdown': self.max_drawdown,
            'sharpe_ratio': sharpe,
            'max_consecutive_wins': max_consecutive_wins,
            'max_consecutive_losses': max_consecutive_losses,
            'trades': self.trades,
            'equity_curve': self.equity_curve
        }
    
    def print_results(self, results: Dict):
        """Print backtest results in formatted way."""
        print("\n" + "="*60)
        print("BACKTEST RESULTS")
        print("="*60)
        print(f"Initial Balance:    ${results['initial_balance']:,.2f}")
        print(f"Final Balance:      ${results['final_balance']:,.2f}")
        print(f"Total PnL:          ${results['total_pnl']:,.2f}")
        print(f"Total Return:       {results['total_return']*100:.2f}%")
        print("-"*60)
        print(f"Total Trades:       {results['total_trades']}")
        print(f"Winning Trades:     {results['winning_trades']}")
        print(f"Losing Trades:      {results['losing_trades']}")
        print(f"Win Rate:           {results['win_rate']*100:.2f}%")
        print(f"Profit Factor:      {results['profit_factor']:.2f}")
        print(f"Max Drawdown:       {results['max_drawdown']*100:.2f}%")
        print(f"Sharpe Ratio:       {results['sharpe_ratio']:.2f}")
        print(f"Max Consecutive W:  {results['max_consecutive_wins']}")
        print(f"Max Consecutive L:  {results['max_consecutive_losses']}")
        print("="*60 + "\n")
