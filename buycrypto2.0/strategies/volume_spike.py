"""
Volume Spike Strategy.
Detects sudden volume increases and trades in the direction of the spike.
"""
import pandas as pd
import numpy as np
from typing import Dict
from strategies.base_strategy import BaseStrategy, Signal
from data.data_loader import calculate_atr
import logging

logger = logging.getLogger(__name__)


class VolumeSpikeStrategy(BaseStrategy):
    """
    Volume spike strategy.
    Trades based on unusual volume activity.
    """

    def __init__(self, config: Dict = None):
        super().__init__("VolumeSpike", config)
        self.volume_ma_period = self.config.get('volume_ma_period', 20)
        self.spike_threshold = self.config.get('spike_threshold', 3.0)  # Increased from 2.5 to 3.0
        self.min_price_change = self.config.get('min_price_change', 0.015)  # Increased from 0.01 to 0.015 (1.5%)
        self.require_close_confirmation = self.config.get('require_close_confirmation', True)

    def generate_signal(
        self,
        df: pd.DataFrame,
        market_data: Dict
    ) -> Signal:
        """Generate volume spike signal."""
        symbol = market_data.get('symbol', 'UNKNOWN')
        current_price = df['close'].iloc[-1]
        timestamp = df.index[-1]

        if len(df) < self.volume_ma_period + 5:
            return Signal(
                symbol=symbol,
                direction='neutral',
                confidence=0.0,
                strategy_name=self.name,
                timestamp=timestamp
            )

        # Calculate volume MA
        volume_ma = df['volume'].rolling(self.volume_ma_period).mean()
        avg_volume = volume_ma.iloc[-1]
        current_volume = df['volume'].iloc[-1]
        
        if avg_volume == 0:
            return Signal(
                symbol=symbol,
                direction='neutral',
                confidence=0.0,
                strategy_name=self.name,
                timestamp=timestamp
            )

        volume_ratio = current_volume / avg_volume
        
        # Check for spike
        if volume_ratio < self.spike_threshold:
            return Signal(
                symbol=symbol,
                direction='neutral',
                confidence=0.0,
                strategy_name=self.name,
                timestamp=timestamp
            )

        # Price change confirmation - stricter requirements
        price_change = (current_price - df['close'].iloc[-2]) / df['close'].iloc[-2]
        
        # Additional confirmation: check if candle closed in direction of move
        close_confirmed = True
        if self.require_close_confirmation:
            if price_change > 0:
                # For long: close should be near high of candle
                candle_range = last_candle['high'] - last_candle['low']
                close_position = (last_candle['close'] - last_candle['low']) / candle_range if candle_range > 0 else 0.5
                close_confirmed = close_position > 0.6  # Close in upper 40% of candle
            else:
                # For short: close should be near low of candle
                candle_range = last_candle['high'] - last_candle['low']
                close_position = (last_candle['close'] - last_candle['low']) / candle_range if candle_range > 0 else 0.5
                close_confirmed = close_position < 0.4  # Close in lower 40% of candle
        
        direction = 'neutral'
        confidence = 0.0
        
        if abs(price_change) >= self.min_price_change and close_confirmed:
            if price_change > 0:
                direction = 'long'
                confidence = 0.65 + min((volume_ratio - self.spike_threshold) * 0.1, 0.35)
            else:
                direction = 'short'
                confidence = 0.65 + min((volume_ratio - self.spike_threshold) * 0.1, 0.35)

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
                stop_loss = current_price - 2.5 * current_atr  # Increased from 2x to reduce premature SL hits
                take_profit = current_price + 5 * current_atr  # Maintained 1:2 RR
            else:
                stop_loss = current_price + 2.5 * current_atr
                take_profit = current_price - 5 * current_atr
        else:
            sl_pct = 0.02
            tp_pct = 0.04
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
                'volume_ratio': volume_ratio,
                'price_change': price_change,
                'atr': current_atr
            }
        )
