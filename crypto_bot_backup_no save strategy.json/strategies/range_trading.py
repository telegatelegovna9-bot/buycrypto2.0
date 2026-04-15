"""
Range Trading Strategy.
Identifies sideways markets and trades bounces from support/resistance levels.
"""
import pandas as pd
import numpy as np
from typing import Dict, Tuple
from strategies.base_strategy import BaseStrategy, Signal
from data.data_loader import calculate_atr, calculate_rsi
import logging

logger = logging.getLogger(__name__)


class RangeTradingStrategy(BaseStrategy):
    """
    Mean reversion strategy for range-bound markets.
    
    Logic:
    - Identify range using Donchian channels or recent high/low
    - Use RSI to detect overbought/oversold conditions
    - Enter on bounces from support/resistance
    - Exit at opposite boundary or middle of range
    """
    
    def __init__(self, config: Dict = None):
        super().__init__("RangeTrading", config)
        self.rsi_period = self.config.get('rsi_period', 14)
        self.range_window = self.config.get('range_window', 20)
        self.rsi_overbought = self.config.get('rsi_overbought', 70)
        self.rsi_oversold = self.config.get('rsi_oversold', 30)
        self.min_range_width = self.config.get('min_range_width', 0.02)  # 2%
    
    def _identify_range(
        self, 
        df: pd.DataFrame
    ) -> Tuple[float, float, float]:
        """
        Identify current trading range.
        Returns: (support, resistance, range_width_pct)
        """
        recent = df.tail(self.range_window)
        
        resistance = recent['high'].max()
        support = recent['low'].min()
        current_price = df['close'].iloc[-1]
        
        range_width = (resistance - support) / support if support > 0 else 0
        
        return support, resistance, range_width
    
    def _is_ranging_market(self, df: pd.DataFrame) -> bool:
        """
        Check if market is in a ranging state (not trending).
        Uses ADX-like metric and range width.
        """
        if len(df) < 28:
            return False
        
        # Simple trend check: are we making new highs/lows consistently?
        recent_highs = df['high'].tail(10).max()
        recent_lows = df['low'].tail(10).min()
        older_highs = df['high'].iloc[-25:-15].max()
        older_lows = df['low'].iloc[-25:-15].min()
        
        # If making consistent HH/HL or LL/LH, it's trending
        is_uptrend = recent_highs > older_highs * 1.01 and recent_lows > older_lows * 1.01
        is_downtrend = recent_lows < older_lows * 0.99 and recent_highs < older_highs * 0.99
        
        # Check range width
        _, _, range_width = self._identify_range(df)
        
        # Market is ranging if not clearly trending and has decent range width
        return (not is_uptrend and not is_downtrend) and range_width >= self.min_range_width
    
    def generate_signal(
        self, 
        df: pd.DataFrame, 
        market_data: Dict
    ) -> Signal:
        """Generate range trading signal."""
        symbol = market_data.get('symbol', 'UNKNOWN')
        current_price = df['close'].iloc[-1]
        timestamp = df.index[-1]
        
        if len(df) < max(self.range_window + 10, self.rsi_period + 5):
            logger.debug(f"Not enough data for {symbol}")
            return Signal(
                symbol=symbol,
                direction='neutral',
                confidence=0.0,
                strategy_name=self.name,
                timestamp=timestamp
            )
        
        # Check if market is ranging
        if not self._is_ranging_market(df):
            return Signal(
                symbol=symbol,
                direction='neutral',
                confidence=0.0,
                strategy_name=self.name,
                timestamp=timestamp,
                metadata={'reason': 'trending_market'}
            )
        
        # Calculate indicators
        rsi = calculate_rsi(df, self.rsi_period).iloc[-1]
        support, resistance, range_width = self._identify_range(df)
        atr = calculate_atr(df, 14).iloc[-1]
        
        # Calculate range position (0 = at support, 1 = at resistance)
        if resistance > support:
            range_position = (current_price - support) / (resistance - support)
        else:
            range_position = 0.5
        
        # Determine signal
        direction = 'neutral'
        confidence = 0.0
        
        # Long setup: price near support + RSI oversold
        if range_position < 0.2 and rsi < self.rsi_oversold:
            direction = 'long'
            # Higher confidence if closer to support and more oversold
            distance_from_support = (current_price - support) / support if support > 0 else 0
            oversold_degree = (self.rsi_oversold - rsi) / self.rsi_oversold
            
            confidence = 0.4 + (0.3 * (1 - distance_from_support / 0.02)) + (0.3 * oversold_degree)
            
        # Short setup: price near resistance + RSI overbought
        elif range_position > 0.8 and rsi > self.rsi_overbought:
            direction = 'short'
            # Higher confidence if closer to resistance and more overbought
            distance_from_resistance = (resistance - current_price) / resistance if resistance > 0 else 0
            overbought_degree = (rsi - self.rsi_overbought) / (100 - self.rsi_overbought)
            
            confidence = 0.4 + (0.3 * (1 - distance_from_resistance / 0.02)) + (0.3 * overbought_degree)
        
        # Cap confidence
        confidence = min(confidence, 1.0)
        
        if direction == 'neutral' or confidence < 0.4:
            return Signal(
                symbol=symbol,
                direction='neutral',
                confidence=confidence,
                strategy_name=self.name,
                timestamp=timestamp,
                metadata={
                    'rsi': rsi,
                    'range_position': range_position,
                    'support': support,
                    'resistance': resistance
                }
            )
        
        # Calculate stop loss and take profit
        # For long: SL below support, TP at resistance or middle
        # For short: SL above resistance, TP at support or middle
        range_middle = (support + resistance) / 2
        
        if direction == 'long':
            stop_loss = support - atr  # Below support
            take_profit = min(resistance, range_middle + (range_middle - support))  # At least 1:2 RR
        else:  # short
            stop_loss = resistance + atr  # Above resistance
            take_profit = max(support, range_middle - (resistance - range_middle))  # At least 1:2 RR
        
        # Ensure minimum RR ratio
        risk = abs(entry_price := current_price - stop_loss) if direction == 'long' else abs(stop_loss - current_price)
        reward = abs(take_profit - current_price)
        
        if reward / risk < 2.0:
            # Adjust take profit to ensure 1:2 RR
            if direction == 'long':
                take_profit = current_price + 2 * risk
            else:
                take_profit = current_price - 2 * risk
        
        return Signal(
            symbol=symbol,
            direction=direction,
            confidence=confidence,
            entry_price=current_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            strategy_name=self.name,
            timestamp=timestamp,
            metadata={
                'rsi': rsi,
                'range_position': range_position,
                'support': support,
                'resistance': resistance,
                'range_width': range_width
            }
        )
