"""
Trend Following and Breakout Strategy.
Identifies trends using moving averages and detects breakouts from consolidation.
"""
import pandas as pd
import numpy as np
from typing import Dict, Optional
from strategies.base_strategy import BaseStrategy, Signal
from data.data_loader import calculate_atr, MarketStructureAnalyzer
import logging

logger = logging.getLogger(__name__)


class TrendBreakoutStrategy(BaseStrategy):
    """
    Trend following strategy with breakout detection.
    
    Logic:
    - Use EMA crossover for trend direction
    - Use ATR for volatility filtering
    - Detect breakouts from recent ranges
    - Confirm with market structure (HH/HL or LL/LH)
    """
    
    def __init__(self, config: Dict = None):
        super().__init__("TrendBreakout", config)
        self.ema_fast = self.config.get('ema_fast', 9)
        self.ema_slow = self.config.get('ema_slow', 21)
        self.atr_period = self.config.get('atr_period', 14)
        self.breakout_window = self.config.get('breakout_window', 20)
        self.min_trend_strength = self.config.get('min_trend_strength', 0.3)
    
    def _calculate_ema(self, df: pd.DataFrame, period: int) -> pd.Series:
        """Calculate Exponential Moving Average."""
        return df['close'].ewm(span=period, adjust=False).mean()
    
    def _calculate_trend_strength(self, df: pd.DataFrame) -> float:
        """
        Calculate trend strength based on ADX-like metric.
        Returns value between 0 and 1.
        """
        if len(df) < 28:
            return 0.0
        
        high = df['high']
        low = df['low']
        close = df['close']
        
        # Calculate directional movement
        plus_dm = high.diff()
        minus_dm = -low.diff()
        
        plus_dm[plus_dm < 0] = 0
        minus_dm[minus_dm < 0] = 0
        
        tr = calculate_atr(df, 14)
        plus_di = 100 * (plus_dm.rolling(14).mean() / tr)
        minus_di = 100 * (minus_dm.rolling(14).mean() / tr)
        
        dx = 100 * abs(plus_di - minus_di) / (plus_di + minus_di)
        adx = dx.rolling(14).mean().iloc[-1]
        
        # Normalize to 0-1 range (ADX typically 0-100)
        return min(adx / 100, 1.0)
    
    def generate_signal(
        self, 
        df: pd.DataFrame, 
        market_data: Dict
    ) -> Signal:
        """Generate trend/breakout signal."""
        symbol = market_data.get('symbol', 'UNKNOWN')
        current_price = df['close'].iloc[-1]
        timestamp = df.index[-1]
        
        if len(df) < max(self.ema_slow * 2, self.breakout_window + 10):
            logger.debug(f"Not enough data for {symbol}")
            return Signal(
                symbol=symbol,
                direction='neutral',
                confidence=0.0,
                strategy_name=self.name,
                timestamp=timestamp
            )
        
        # Calculate indicators
        ema_fast = self._calculate_ema(df, self.ema_fast)
        ema_slow = self._calculate_ema(df, self.ema_slow)
        atr = calculate_atr(df, self.atr_period)
        
        current_atr = atr.iloc[-1]
        current_ema_fast = ema_fast.iloc[-1]
        current_ema_slow = ema_slow.iloc[-1]
        
        # Determine trend direction
        trend_direction = 'neutral'
        if current_ema_fast > current_ema_slow:
            trend_direction = 'long'
        elif current_ema_fast < current_ema_slow:
            trend_direction = 'short'
        
        # Calculate trend strength
        trend_strength = self._calculate_trend_strength(df)
        
        # Check for breakout
        is_breakout, breakout_direction = MarketStructureAnalyzer.is_breakout(
            df, self.breakout_window
        )
        
        # Check market structure
        market_structure = MarketStructureAnalyzer.identify_structure(df)
        
        # Calculate base confidence
        confidence = 0.0
        
        # Trend alignment
        if trend_strength > self.min_trend_strength:
            confidence += 0.3
        
        # Breakout confirmation
        if is_breakout:
            if breakout_direction == trend_direction:
                confidence += 0.4  # Strong confirmation
            else:
                confidence += 0.1  # Weak signal
        
        # Market structure confirmation
        if trend_direction == 'long' and market_structure == 'uptrend':
            confidence += 0.2
        elif trend_direction == 'short' and market_structure == 'downtrend':
            confidence += 0.2
        
        # Volume confirmation (if available)
        volume_ratio = market_data.get('ticker', {}).get('quoteVolume', 0)
        avg_volume = df['volume'].mean()
        if avg_volume > 0 and df['volume'].iloc[-1] > avg_volume * 1.2:
            confidence += 0.1  # Above average volume
        
        # Cap confidence at 1.0
        confidence = min(confidence, 1.0)
        
        # If no clear direction, return neutral
        if trend_direction == 'neutral' or confidence < 0.4:
            return Signal(
                symbol=symbol,
                direction='neutral',
                confidence=confidence,
                strategy_name=self.name,
                timestamp=timestamp
            )
        
        # Calculate stop loss and take profit based on ATR
        if current_atr > 0:
            if trend_direction == 'long':
                stop_loss = current_price - 2 * current_atr
                take_profit = current_price + 4 * current_atr  # 1:2 RR minimum
            else:  # short
                stop_loss = current_price + 2 * current_atr
                take_profit = current_price - 4 * current_atr
        else:
            # Fallback if ATR is zero
            sl_pct = 0.02
            tp_pct = 0.04
            if trend_direction == 'long':
                stop_loss = current_price * (1 - sl_pct)
                take_profit = current_price * (1 + tp_pct)
            else:
                stop_loss = current_price * (1 + sl_pct)
                take_profit = current_price * (1 - tp_pct)
        
        return Signal(
            symbol=symbol,
            direction=trend_direction,
            confidence=confidence,
            entry_price=current_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            strategy_name=self.name,
            timestamp=timestamp,
            metadata={
                'trend_strength': trend_strength,
                'is_breakout': is_breakout,
                'breakout_direction': breakout_direction,
                'market_structure': market_structure,
                'atr': current_atr
            }
        )
