"""
Momentum Strategy.
Trades strong directional moves with volume confirmation.
"""
import pandas as pd
import numpy as np
from typing import Dict
from strategies.base_strategy import BaseStrategy, Signal
from data.data_loader import calculate_atr
import logging

logger = logging.getLogger(__name__)


class MomentumStrategy(BaseStrategy):
    """
    Momentum strategy.
    Trades strong impulsive moves with volume confirmation.
    """

    def __init__(self, config: Dict = None):
        super().__init__("Momentum", config)
        self.momentum_period = self.config.get('momentum_period', 5)
        self.min_momentum = self.config.get('min_momentum', 0.02)
        self.volume_confirmation = self.config.get('volume_confirmation', True)
        self.volume_ma_period = self.config.get('volume_ma_period', 10)

    def generate_signal(
        self,
        df: pd.DataFrame,
        market_data: Dict
    ) -> Signal:
        """Generate momentum signal."""
        symbol = market_data.get('symbol', 'UNKNOWN')
        current_price = df['close'].iloc[-1]
        timestamp = df.index[-1]

        if len(df) < self.momentum_period + 10:
            return Signal(
                symbol=symbol,
                direction='neutral',
                confidence=0.0,
                strategy_name=self.name,
                timestamp=timestamp
            )

        # Calculate momentum
        momentum = (current_price - df['close'].iloc[-self.momentum_period]) / df['close'].iloc[-self.momentum_period]
        
        # Volume confirmation
        volume_confirmed = True
        if self.volume_confirmation:
            volume_ma = df['volume'].rolling(self.volume_ma_period).mean().iloc[-1]
            current_volume = df['volume'].iloc[-1]
            volume_confirmed = current_volume > volume_ma * 1.2

        direction = 'neutral'
        confidence = 0.0
        
        if abs(momentum) > self.min_momentum:
            if momentum > 0 and volume_confirmed:
                direction = 'long'
                confidence = 0.6 + min(abs(momentum) * 5, 0.4)
            elif momentum < 0 and volume_confirmed:
                direction = 'short'
                confidence = 0.6 + min(abs(momentum) * 5, 0.4)

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
                'momentum': momentum,
                'volume_confirmed': volume_confirmed,
                'atr': current_atr
            }
        )
