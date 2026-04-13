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
        self.spike_threshold = self.config.get('spike_threshold', 2.5)
        self.min_price_change = self.config.get('min_price_change', 0.01)

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

        # Price change confirmation
        price_change = (current_price - df['close'].iloc[-2]) / df['close'].iloc[-2]
        
        direction = 'neutral'
        confidence = 0.0
        
        if abs(price_change) > self.min_price_change:
            if price_change > 0:
                direction = 'long'
                confidence = 0.6 + min((volume_ratio - self.spike_threshold) * 0.1, 0.4)
            else:
                direction = 'short'
                confidence = 0.6 + min((volume_ratio - self.spike_threshold) * 0.1, 0.4)

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
                stop_loss = current_price - 2 * current_atr
                take_profit = current_price + 4 * current_atr
            else:
                stop_loss = current_price + 2 * current_atr
                take_profit = current_price - 4 * current_atr
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
