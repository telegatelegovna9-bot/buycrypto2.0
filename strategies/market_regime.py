import pandas as pd
import numpy as np
import ta.trend as ta_trend
import ta.volatility as ta_vol
from typing import Dict, Literal, Optional

MarketRegime = Literal["TREND_UP", "TREND_DOWN", "RANGE", "LOW_VOL", "HIGH_VOL", "ACCUMULATION", "UNKNOWN"]

class MarketRegimeDetector:
    """
    Детектор рыночного режима.
    Определяет текущее состояние рынка для выбора оптимальных стратегий.
    """
    
    def __init__(self, lookback: int = 50):
        self.lookback = lookback

    def detect(self, df: pd.DataFrame) -> Dict[str, any]:
        """
        Определяет режим рынка на основе технических индикаторов.
        
        Args:
            df: DataFrame с OHLCV данными
            
        Returns:
            Dict с режимом, уверенностью и дополнительными метриками
        """
        if len(df) < self.lookback:
            return {"regime": "UNKNOWN", "confidence": 0.0, "adx": 0.0, "atr": 0.0}

        # Создаем копию чтобы не модифицировать оригинал
        data = df.copy()
        
        # Индикаторы тренда
        data['ema_20'] = ta_trend.EMAIndicator(data['close'], window=20).ema_indicator()
        data['ema_50'] = ta_trend.EMAIndicator(data['close'], window=50).ema_indicator()
        data['adx'] = ta_trend.ADXIndicator(data['high'], data['low'], data['close'], window=14).adx()
        
        # Индикаторы волатильности
        bb = ta_vol.BollingerBands(data['close'], window=20, window_dev=2)
        data['bb_h'] = bb.bollinger_hband()
        data['bb_l'] = bb.bollinger_lband()
        data['bb_width'] = (data['bb_h'] - data['bb_l']) / data['close']
        
        # ATR
        atr = ta_vol.AverageTrueRange(data['high'], data['low'], data['close'], window=14)
        data['atr'] = atr.average_true_range()

        last = data.iloc[-1]
        
        # Скользящие средние для волатильности
        avg_bb_width = data['bb_width'].rolling(20).mean().iloc[-1]
        current_bb_width = last['bb_width']
        
        # Объемы для аккумуляции
        avg_vol = data['volume'].rolling(20).mean().iloc[-1]
        
        regime = "RANGE"
        confidence = 0.5

        # Определение тренда через ADX
        if last['adx'] > 25:
            if last['ema_20'] > last['ema_50'] and last['close'] > last['ema_20']:
                regime = "TREND_UP"
                confidence = min(1.0, last['adx'] / 50)
            elif last['ema_20'] < last['ema_50'] and last['close'] < last['ema_20']:
                regime = "TREND_DOWN"
                confidence = min(1.0, last['adx'] / 50)
        else:
            # Боковое движение - определяем подтип
            if current_bb_width < avg_bb_width * 0.7:
                regime = "LOW_VOL"
                confidence = 0.8
            elif current_bb_width > avg_bb_width * 1.5:
                regime = "HIGH_VOL"
                confidence = 0.6
            else:
                # Проверка на аккумуляцию
                if last['volume'] > avg_vol * 1.5 and abs(last['close'] - last['ema_20']) / last['close'] < 0.01:
                    regime = "ACCUMULATION"
                    confidence = 0.7
                else:
                    regime = "RANGE"
                    confidence = 0.5

        return {
            "regime": regime,
            "confidence": confidence,
            "adx": float(last['adx']),
            "atr": float(last['atr']),
            "bb_width": float(current_bb_width)
        }
