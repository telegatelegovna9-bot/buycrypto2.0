"""
Liquidity Grab / Stop Hunt Strategy.
Detects false breakouts and liquidity sweeps for counter-trend entries.
"""
import pandas as pd
import numpy as np
from typing import Dict
from strategies.base_strategy import BaseStrategy, Signal
from data.data_loader import calculate_atr
import logging

logger = logging.getLogger(__name__)


class LiquidityGrabStrategy(BaseStrategy):
    """
    Liquidity grab strategy.
    Trades false breakouts and stop hunts.
    """

    def __init__(self, config: Dict = None):
        super().__init__("LiquidityGrab", config)
        self.lookback_period = self.config.get('lookback_period', 10)
        self.min_shadow_ratio = self.config.get('min_shadow_ratio', 2.0)
        self.confirmation_candles = self.config.get('confirmation_candles', 1)

    def _find_local_extremes(self, df: pd.DataFrame) -> tuple:
        """Find local highs and lows."""
        local_highs = df['high'].rolling(self.lookback_period).max().shift(1)
        local_lows = df['low'].rolling(self.lookback_period).min().shift(1)
        return local_highs, local_lows

    def generate_signal(
        self,
        df: pd.DataFrame,
        market_data: Dict
    ) -> Signal:
        """Generate liquidity grab signal."""
        symbol = market_data.get('symbol', 'UNKNOWN')
        current_price = df['close'].iloc[-1]
        timestamp = df.index[-1]

        if len(df) < self.lookback_period + 5:
            return Signal(
                symbol=symbol,
                direction='neutral',
                confidence=0.0,
                strategy_name=self.name,
                timestamp=timestamp
            )

        local_highs, local_lows = self._find_local_extremes(df)
        
        prev_high = local_highs.iloc[-1]
        prev_low = local_lows.iloc[-1]
        
        last_candle = df.iloc[-1]
        prev_candle = df.iloc[-2]
        
        # Calculate body and shadows
        body = abs(last_candle['close'] - last_candle['open'])
        upper_shadow = last_candle['high'] - max(last_candle['open'], last_candle['close'])
        lower_shadow = min(last_candle['open'], last_candle['close']) - last_candle['low']
        
        direction = 'neutral'
        confidence = 0.0
        
        # Bullish liquidity grab (stop hunt below support)
        if last_candle['low'] < prev_low and upper_shadow > body * self.min_shadow_ratio:
            # Price swept low but closed back above
            if last_candle['close'] > prev_low:
                direction = 'long'
                confidence = 0.65 + min((upper_shadow / body - self.min_shadow_ratio) * 0.1, 0.35)
        
        # Bearish liquidity grab (stop hunt above resistance)
        elif last_candle['high'] > prev_high and lower_shadow > body * self.min_shadow_ratio:
            # Price swept high but closed back below
            if last_candle['close'] < prev_high:
                direction = 'short'
                confidence = 0.65 + min((lower_shadow / body - self.min_shadow_ratio) * 0.1, 0.35)

        if direction == 'neutral' or confidence < 0.5:
            return Signal(
                symbol=symbol,
                direction='neutral',
                confidence=0.0,
                strategy_name=self.name,
                timestamp=timestamp
            )

        # Calculate SL/TP
        atr = calculate_atr(df, 14)
        current_atr = atr.iloc[-1]
        
        if current_atr > 0:
            if direction == 'long':
                stop_loss = last_candle['low'] - 0.5 * current_atr
                take_profit = current_price + 3 * current_atr
            else:
                stop_loss = last_candle['high'] + 0.5 * current_atr
                take_profit = current_price - 3 * current_atr
        else:
            sl_pct = 0.015
            tp_pct = 0.045
            if direction == 'long':
                stop_loss = current_price * (1 - sl_pct)
                take_profit = current_price * (1 + tp_pct)
            else:
                stop_loss = current_price * (1 + sl_pct)
                take_profit = current_price * (1 - tp_pct)

        return Signal(
            symbol=symbol,
            direction=direction,
            confidence=min(confidence, 1.0),
            entry_price=current_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            strategy_name=self.name,
            timestamp=timestamp,
            metadata={
                'upper_shadow': upper_shadow,
                'lower_shadow': lower_shadow,
                'body': body,
                'swept_level': prev_low if direction == 'long' else prev_high,
                'atr': current_atr
            }
        )
