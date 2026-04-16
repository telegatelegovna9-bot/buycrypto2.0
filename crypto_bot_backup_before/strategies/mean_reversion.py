"""
Mean Reversion Strategy.
Trades overbought/oversold conditions using RSI and Bollinger Bands.
"""
import pandas as pd
import numpy as np
from typing import Dict
from strategies.base_strategy import BaseStrategy, Signal
from data.data_loader import calculate_atr
import ta.momentum as ta_momentum
import ta.volatility as ta_volatility
import logging

logger = logging.getLogger(__name__)


class MeanReversionStrategy(BaseStrategy):
    """
    Mean reversion strategy.
    Trades extreme deviations from the mean.
    """

    def __init__(self, config: Dict = None):
        super().__init__("MeanReversion", config)
        self.rsi_period = self.config.get('rsi_period', 14)
        self.bb_window = self.config.get('bb_window', 20)
        self.bb_std = self.config.get('bb_std', 2.0)
        self.overbought_threshold = self.config.get('overbought_threshold', 70)
        self.oversold_threshold = self.config.get('oversold_threshold', 30)

    def generate_signal(
        self,
        df: pd.DataFrame,
        market_data: Dict
    ) -> Signal:
        """Generate mean reversion signal."""
        symbol = market_data.get('symbol', 'UNKNOWN')
        current_price = df['close'].iloc[-1]
        timestamp = df.index[-1]

        if len(df) < max(self.rsi_period, self.bb_window) + 5:
            return Signal(
                symbol=symbol,
                direction='neutral',
                confidence=0.0,
                strategy_name=self.name,
                timestamp=timestamp
            )

        # Calculate RSI
        rsi = ta_momentum.RSIIndicator(df['close'], window=self.rsi_period).rsi()
        current_rsi = rsi.iloc[-1]
        
        # Calculate Bollinger Bands
        bb = ta_volatility.BollingerBands(df['close'], window=self.bb_window, window_dev=self.bb_std)
        bb_upper = bb.bollinger_hband().iloc[-1]
        bb_lower = bb.bollinger_lband().iloc[-1]
        bb_middle = bb.bollinger_mavg().iloc[-1]

        direction = 'neutral'
        confidence = 0.0
        
        # Overbought condition (short signal)
        if current_rsi > self.overbought_threshold and current_price > bb_upper:
            direction = 'short'
            # Higher RSI = higher confidence
            confidence = 0.5 + min((current_rsi - self.overbought_threshold) / 100, 0.5)
        
        # Oversold condition (long signal)
        elif current_rsi < self.oversold_threshold and current_price < bb_lower:
            direction = 'long'
            # Lower RSI = higher confidence
            confidence = 0.5 + min((self.oversold_threshold - current_rsi) / 100, 0.5)

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
                take_profit = bb_middle + current_atr  # Target middle band
            else:
                stop_loss = current_price + 2 * current_atr
                take_profit = bb_middle - current_atr
        else:
            sl_pct = 0.02
            tp_pct = 0.03
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
                'rsi': current_rsi,
                'bb_upper': bb_upper,
                'bb_lower': bb_lower,
                'bb_middle': bb_middle,
                'atr': current_atr
            }
        )
