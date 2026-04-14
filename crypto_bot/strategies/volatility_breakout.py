"""
Volatility Breakout Strategy.
Detects volatility compression and trades explosive breakouts.
"""
import pandas as pd
import numpy as np
from typing import Dict, Tuple
from strategies.base_strategy import BaseStrategy, Signal
from data.data_loader import calculate_atr
import logging

logger = logging.getLogger(__name__)


class VolatilityBreakoutStrategy(BaseStrategy):
    """
    Volatility breakout strategy based on Bollinger Band squeeze and ATR compression.
    
    Logic:
    - Detect volatility compression (low ATR relative to recent history)
    - Identify Bollinger Band squeeze (narrow bands)
    - Trade breakout from compression with momentum confirmation
    - Use ATR expansion for confirmation
    """
    
    def __init__(self, config: Dict = None):
        super().__init__("VolatilityBreakout", config)
        self.bb_period = self.config.get('bb_period', 20)
        self.bb_std = self.config.get('bb_std', 2.0)
        self.atr_period = self.config.get('atr_period', 14)
        self.volatility_lookback = self.config.get('volatility_lookback', 50)
        self.squeeze_threshold = self.config.get('squeeze_threshold', 0.5)  # Bottom 50% of vol
        self.breakout_multiplier = self.config.get('breakout_multiplier', 1.5)
    
    def _calculate_bollinger_bands(
        self, 
        df: pd.DataFrame
    ) -> Tuple[pd.Series, pd.Series, pd.Series]:
        """Calculate Bollinger Bands."""
        middle = df['close'].rolling(window=self.bb_period).mean()
        std = df['close'].rolling(window=self.bb_period).std()
        upper = middle + (self.bb_std * std)
        lower = middle - (self.bb_std * std)
        return upper, middle, lower
    
    def _calculate_bandwidth(self, upper: pd.Series, lower: pd.Series, middle: pd.Series) -> pd.Series:
        """Calculate Bollinger Band width (normalized)."""
        bandwidth = (upper - lower) / middle
        return bandwidth
    
    def _is_volatility_squeeze(self, df: pd.DataFrame) -> bool:
        """Check if volatility is compressed."""
        if len(df) < self.volatility_lookback + self.bb_period:
            return False
        
        upper, middle, lower = self._calculate_bollinger_bands(df)
        bandwidth = self._calculate_bandwidth(upper, lower, middle)
        
        current_bandwidth = bandwidth.iloc[-1]
        historical_bandwidth = bandwidth.iloc[-self.volatility_lookback:-1]
        
        if len(historical_bandwidth) == 0 or current_bandwidth == 0:
            return False
        
        # Check if current bandwidth is in bottom percentile
        percentile = (historical_bandwidth < current_bandwidth).mean()
        
        return percentile <= self.squeeze_threshold
    
    def _detect_breakout(
        self, 
        df: pd.DataFrame,
        upper: pd.Series,
        lower: pd.Series
    ) -> Tuple[bool, str]:
        """
        Detect breakout direction.
        Returns: (is_breakout, direction: 'up'/'down'/None)
        """
        current_close = df['close'].iloc[-1]
        prev_close = df['close'].iloc[-2]
        current_upper = upper.iloc[-1]
        current_lower = lower.iloc[-1]
        
        # Check for upward breakout
        if current_close > current_upper and prev_close <= current_upper:
            return True, 'up'
        
        # Check for downward breakout
        if current_close < current_lower and prev_close >= current_lower:
            return True, 'down'
        
        return False, None
    
    def _calculate_momentum(self, df: pd.DataFrame, period: int = 10) -> pd.Series:
        """Calculate price momentum."""
        return df['close'].pct_change(periods=period)
    
    def generate_signal(
        self, 
        df: pd.DataFrame, 
        market_data: Dict
    ) -> Signal:
        """Generate volatility breakout signal."""
        symbol = market_data.get('symbol', 'UNKNOWN')
        current_price = df['close'].iloc[-1]
        timestamp = df.index[-1]
        
        min_bars = self.volatility_lookback + self.bb_period + self.atr_period
        if len(df) < min_bars:
            logger.debug(f"Not enough data for {symbol}")
            return Signal(
                symbol=symbol,
                direction='neutral',
                confidence=0.0,
                strategy_name=self.name,
                timestamp=timestamp
            )
        
        # Calculate indicators
        upper, middle, lower = self._calculate_bollinger_bands(df)
        atr = calculate_atr(df, self.atr_period)
        momentum = self._calculate_momentum(df, 10)
        
        current_atr = atr.iloc[-1]
        avg_atr = atr.iloc[-self.atr_period:-1].mean() if len(atr) > self.atr_period else current_atr
        
        # Check for squeeze
        is_squeeze = self._is_volatility_squeeze(df)
        
        # Check for breakout
        is_breakout, breakout_dir = self._detect_breakout(df, upper, lower)
        
        # Determine signal
        direction = 'neutral'
        confidence = 0.0
        
        # High confidence setup: squeeze + breakout + momentum confirmation
        if is_squeeze and is_breakout:
            if breakout_dir == 'up' and momentum.iloc[-1] > 0.02:  # 2% momentum
                direction = 'long'
                confidence = 0.6
                
                # Extra confidence if ATR expanding
                if current_atr > avg_atr * 1.2:
                    confidence += 0.2
                    
            elif breakout_dir == 'down' and momentum.iloc[-1] < -0.02:
                direction = 'short'
                confidence = 0.6
                
                if current_atr > avg_atr * 1.2:
                    confidence += 0.2
        
        # Medium confidence: strong breakout without squeeze
        elif is_breakout:
            momentum_abs = abs(momentum.iloc[-1])
            if momentum_abs > 0.03:  # 3% move
                direction = breakout_dir if breakout_dir == 'up' else 'short'
                if breakout_dir == 'down':
                    direction = 'short'
                confidence = 0.4 + min(momentum_abs / 0.05, 0.3)
        
        confidence = min(confidence, 1.0)
        
        if direction == 'neutral' or confidence < 0.4:
            return Signal(
                symbol=symbol,
                direction='neutral',
                confidence=confidence,
                strategy_name=self.name,
                timestamp=timestamp,
                metadata={
                    'is_squeeze': is_squeeze,
                    'is_breakout': is_breakout,
                    'bandwidth': self._calculate_bandwidth(upper, lower, middle).iloc[-1],
                    'atr': current_atr,
                    'momentum': momentum.iloc[-1]
                }
            )
        
        # Calculate SL/TP based on ATR and Bollinger Bands
        atr_multiplier = 2.0
        if direction == 'long':
            stop_loss = max(lower.iloc[-1], current_price - atr_multiplier * current_atr)
            take_profit = current_price + (current_price - stop_loss) * 2.5  # 1:2.5 RR
        else:  # short
            stop_loss = min(upper.iloc[-1], current_price + atr_multiplier * current_atr)
            take_profit = current_price - (stop_loss - current_price) * 2.5
        
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
                'is_squeeze': is_squeeze,
                'is_breakout': is_breakout,
                'breakout_direction': breakout_dir,
                'bandwidth': self._calculate_bandwidth(upper, lower, middle).iloc[-1],
                'atr': current_atr,
                'momentum': momentum.iloc[-1],
                'upper_band': upper.iloc[-1],
                'lower_band': lower.iloc[-1]
            }
        )
