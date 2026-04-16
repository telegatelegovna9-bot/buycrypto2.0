"""
Volume-based Trading Strategy.
Analyzes volume patterns to detect accumulation, distribution, and breakouts.
"""
import pandas as pd
import numpy as np
from typing import Dict, List, Tuple
from strategies.base_strategy import BaseStrategy, Signal
from data.data_loader import calculate_atr
import logging

logger = logging.getLogger(__name__)


class VolumeStrategy(BaseStrategy):
    """
    Volume-based strategy detecting smart money movements.
    
    Logic:
    - Detect volume spikes (unusual activity)
    - Identify accumulation/distribution patterns
    - Confirm price moves with volume
    - Trade volume breakouts
    """
    
    def __init__(self, config: Dict = None):
        super().__init__("VolumeStrategy", config)
        self.volume_ma_period = self.config.get('volume_ma_period', 20)
        self.volume_spike_threshold = self.config.get('volume_spike_threshold', 2.0)
        self.obv_period = self.config.get('obv_period', 14)
        self.vwap_period = self.config.get('vwap_period', 20)
    
    def _calculate_obv(self, df: pd.DataFrame) -> pd.Series:
        """Calculate On-Balance Volume."""
        obv = [0]
        for i in range(1, len(df)):
            if df['close'].iloc[i] > df['close'].iloc[i-1]:
                obv.append(obv[-1] + df['volume'].iloc[i])
            elif df['close'].iloc[i] < df['close'].iloc[i-1]:
                obv.append(obv[-1] - df['volume'].iloc[i])
            else:
                obv.append(obv[-1])
        return pd.Series(obv, index=df.index)
    
    def _calculate_vwap(self, df: pd.DataFrame, period: int = 20) -> pd.Series:
        """Calculate Volume Weighted Average Price."""
        typical_price = (df['high'] + df['low'] + df['close']) / 3
        vwap = (typical_price * df['volume']).rolling(window=period).sum() / df['volume'].rolling(window=period).sum()
        return vwap
    
    def _detect_volume_spike(self, df: pd.DataFrame) -> Tuple[bool, float]:
        """
        Detect volume spike.
        Returns: (is_spike, volume_ratio)
        """
        volume_ma = df['volume'].rolling(window=self.volume_ma_period).mean().iloc[-1]
        current_volume = df['volume'].iloc[-1]
        
        if volume_ma == 0:
            return False, 1.0
        
        volume_ratio = current_volume / volume_ma
        is_spike = volume_ratio >= self.volume_spike_threshold
        
        return is_spike, volume_ratio
    
    def _detect_accumulation_distribution(
        self, 
        df: pd.DataFrame
    ) -> str:
        """
        Detect accumulation or distribution pattern.
        Returns: 'accumulation', 'distribution', or 'neutral'
        """
        if len(df) < self.obv_period + 10:
            return 'neutral'
        
        obv = self._calculate_obv(df)
        
        # Check OBV trend vs price trend
        recent_obv = obv.tail(10)
        older_obv = obv.iloc[-25:-15]
        
        recent_price = df['close'].tail(10)
        older_price = df['close'].iloc[-25:-15]
        
        obv_uptrend = recent_obv.mean() > older_obv.mean()
        obv_downtrend = recent_obv.mean() < older_obv.mean()
        
        price_uptrend = recent_price.mean() > older_price.mean()
        price_downtrend = recent_price.mean() < older_price.mean()
        
        # Accumulation: OBV rising while price flat or down
        if obv_uptrend and not price_uptrend:
            return 'accumulation'
        
        # Distribution: OBV falling while price flat or up
        if obv_downtrend and not price_downtrend:
            return 'distribution'
        
        return 'neutral'
    
    def generate_signal(
        self, 
        df: pd.DataFrame, 
        market_data: Dict
    ) -> Signal:
        """Generate volume-based signal."""
        symbol = market_data.get('symbol', 'UNKNOWN')
        current_price = df['close'].iloc[-1]
        timestamp = df.index[-1]
        
        if len(df) < max(self.volume_ma_period + 10, self.obv_period + 15):
            logger.debug(f"Not enough data for {symbol}")
            return Signal(
                symbol=symbol,
                direction='neutral',
                confidence=0.0,
                strategy_name=self.name,
                timestamp=timestamp
            )
        
        # Calculate indicators
        volume_spike, volume_ratio = self._detect_volume_spike(df)
        acc_dist = self._detect_accumulation_distribution(df)
        vwap = self._calculate_vwap(df, self.vwap_period).iloc[-1]
        atr = calculate_atr(df, 14).iloc[-1]
        
        # Determine direction based on volume analysis
        direction = 'neutral'
        confidence = 0.0
        
        # Volume spike breakout
        if volume_spike:
            # Check price direction with volume
            price_change = df['close'].iloc[-1] - df['close'].iloc[-3]
            
            if price_change > 0 and current_price > vwap:
                direction = 'long'
                confidence = 0.4 + min((volume_ratio - 1) / 5, 0.4)  # More volume = more confidence
            elif price_change < 0 and current_price < vwap:
                direction = 'short'
                confidence = 0.4 + min((volume_ratio - 1) / 5, 0.4)
        
        # Accumulation/Distribution signals
        if acc_dist == 'accumulation' and current_price >= vwap:
            direction = 'long'
            confidence = max(confidence, 0.5)
        elif acc_dist == 'distribution' and current_price <= vwap:
            direction = 'short'
            confidence = max(confidence, 0.5)
        
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
                    'volume_ratio': volume_ratio,
                    'acc_dist': acc_dist,
                    'vwap': vwap
                }
            )
        
        # Calculate stop loss and take profit - INCREASED SL to 2.5x ATR
        if direction == 'long':
            stop_loss = current_price - 2.5 * atr  # Increased from 1.5x to reduce premature SL hits
            take_profit = current_price + 3 * atr  # 1:2 RR
        else:  # short
            stop_loss = current_price + 2.5 * atr  # Increased from 1.5x
            take_profit = current_price - 3 * atr  # 1:2 RR
        
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
                'volume_ratio': volume_ratio,
                'acc_dist': acc_dist,
                'vwap': vwap,
                'volume_spike': volume_spike
            }
        )


class OpenInterestStrategy(BaseStrategy):
    """
    Open Interest based strategy.
    Uses OI changes to detect market sentiment and potential reversals.
    
    Logic:
    - Rising OI + Rising Price = Strong uptrend (new longs)
    - Rising OI + Falling Price = Strong downtrend (new shorts)
    - Falling OI + Rising Price = Short covering (weak uptrend)
    - Falling OI + Falling Price = Long liquidation (weak downtrend)
    """
    
    def __init__(self, config: Dict = None):
        super().__init__("OpenInterest", config)
        self.oi_lookback = self.config.get('oi_lookback', 5)
        self.min_oi_change = self.config.get('min_oi_change', 0.05)  # 5%
    
    def generate_signal(
        self, 
        df: pd.DataFrame, 
        market_data: Dict
    ) -> Signal:
        """Generate OI-based signal."""
        symbol = market_data.get('symbol', 'UNKNOWN')
        current_price = df['close'].iloc[-1]
        timestamp = df.index[-1]
        
        current_oi = market_data.get('open_interest', 0)
        funding_rate = market_data.get('funding_rate', 0)
        
        if len(df) < 20 or current_oi == 0:
            return Signal(
                symbol=symbol,
                direction='neutral',
                confidence=0.0,
                strategy_name=self.name,
                timestamp=timestamp
            )
        
        # Calculate price change
        price_change_pct = (df['close'].iloc[-1] - df['close'].iloc[-self.oi_lookback]) / df['close'].iloc[-self.oi_lookback]
        
        # We need historical OI data for proper analysis
        # For now, use funding rate as sentiment indicator
        confidence = 0.0
        direction = 'neutral'
        
        # Extreme funding rates can signal reversals
        if funding_rate > 0.01:  # Very positive funding (crowded longs)
            if price_change_pct > 0.05:  # Price already up 5%
                direction = 'short'
                confidence = 0.5 + min(funding_rate / 0.02, 0.3)
        elif funding_rate < -0.01:  # Very negative funding (crowded shorts)
            if price_change_pct < -0.05:  # Price already down 5%
                direction = 'long'
                confidence = 0.5 + min(abs(funding_rate) / 0.02, 0.3)
        
        # Trend confirmation with moderate funding
        if 0 < funding_rate <= 0.01 and price_change_pct > 0.02:
            direction = 'long'
            confidence = 0.4
        elif -0.01 <= funding_rate < 0 and price_change_pct < -0.02:
            direction = 'short'
            confidence = 0.4
        
        confidence = min(confidence, 1.0)
        
        if direction == 'neutral' or confidence < 0.4:
            return Signal(
                symbol=symbol,
                direction='neutral',
                confidence=confidence,
                strategy_name=self.name,
                timestamp=timestamp,
                metadata={
                    'funding_rate': funding_rate,
                    'open_interest': current_oi
                }
            )
        
        # Calculate SL/TP - INCREASED SL to 2.5x ATR
        atr = calculate_atr(df, 14).iloc[-1]
        
        if direction == 'long':
            stop_loss = current_price - 2.5 * atr  # Increased from 2x to reduce premature SL hits
            take_profit = current_price + 4 * atr
        else:  # short
            stop_loss = current_price + 2.5 * atr  # Increased from 2x
            take_profit = current_price - 4 * atr
        
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
                'funding_rate': funding_rate,
                'open_interest': current_oi,
                'price_change_pct': price_change_pct
            }
        )
