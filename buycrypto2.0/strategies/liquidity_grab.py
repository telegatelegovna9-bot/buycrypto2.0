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
        self.min_shadow_ratio = self.config.get('min_shadow_ratio', 2.5)  # Increased from 2.0 to 2.5
        self.confirmation_candles = self.config.get('confirmation_candles', 2)  # Increased from 1 to 2
        self.require_volume_confirmation = self.config.get('require_volume_confirmation', True)
        self.min_volume_ratio = self.config.get('min_volume_ratio', 1.3)  # Volume spike confirmation

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
        
        # Volume confirmation check
        volume_confirmed = True
        if self.require_volume_confirmation:
            volume_ma = df['volume'].rolling(20).mean().iloc[-1]
            current_volume = df['volume'].iloc[-1]
            volume_confirmed = current_volume > volume_ma * self.min_volume_ratio
        
        direction = 'neutral'
        confidence = 0.0
        
        # Bullish liquidity grab (stop hunt below support) - stricter conditions
        # Require: sweep of low + long lower shadow + close back above level + volume confirmation
        if last_candle['low'] < prev_low and lower_shadow > body * self.min_shadow_ratio:
            if last_candle['close'] > prev_low and volume_confirmed:
                direction = 'long'
                # Higher confidence with stronger shadow and volume
                shadow_ratio = lower_shadow / body if body > 0 else self.min_shadow_ratio
                confidence = 0.65 + min((shadow_ratio - self.min_shadow_ratio) * 0.15, 0.35)
        
        # Bearish liquidity grab (stop hunt above resistance) - stricter conditions
        # Require: sweep of high + long upper shadow + close back below level + volume confirmation
        elif last_candle['high'] > prev_high and upper_shadow > body * self.min_shadow_ratio:
            if last_candle['close'] < prev_high and volume_confirmed:
                direction = 'short'
                shadow_ratio = upper_shadow / body if body > 0 else self.min_shadow_ratio
                confidence = 0.65 + min((shadow_ratio - self.min_shadow_ratio) * 0.15, 0.35)

        if direction == 'neutral' or confidence < 0.5:
            return Signal(
                symbol=symbol,
                direction='neutral',
                confidence=0.0,
                strategy_name=self.name,
                timestamp=timestamp
            )

        # Calculate SL/TP - INCREASED SL to 2.5x ATR
        atr = calculate_atr(df, 14)
        current_atr = atr.iloc[-1]
        
        if current_atr > 0:
            if direction == 'long':
                stop_loss = last_candle['low'] - 2.5 * current_atr  # Increased from 0.5x to 2.5x ATR
                take_profit = current_price + 3 * current_atr
            else:
                stop_loss = last_candle['high'] + 2.5 * current_atr  # Increased from 0.5x to 2.5x ATR
                take_profit = current_price - 3 * current_atr
        else:
            sl_pct = 0.025  # Increased from 1.5% to 2.5%
            tp_pct = 0.075  # 1:3 RR
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
