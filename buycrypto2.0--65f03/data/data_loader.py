"""
Data loading and market data management module.
Handles OHLCV, Open Interest, Funding Rates, and other market data.
"""
import ccxt.async_support as ccxt
import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta
import asyncio
import logging

logger = logging.getLogger(__name__)


class DataLoader:
    """Async data loader for cryptocurrency market data."""
    
    def __init__(
        self, 
        exchange_id: str = "binance", 
        sandbox: bool = False,
        proxy_url: str = "",
        options: dict = None,
        adjust_time_difference: bool = True
    ):
        self.exchange_id = exchange_id
        self.sandbox = sandbox
        self.proxy_url = proxy_url
        self.options = options or {}
        self.adjust_time_difference = adjust_time_difference
        self.exchange = None
        self._initialized = False
        
    async def initialize(self):
        """Initialize the exchange connection."""
        if not self._initialized:
            try:
                # Базовая конфигурация
                exchange_config = {
                    'enableRateLimit': True,
                    'options': {
                        'defaultType': 'future',
                        'adjustForTimeDifference': self.adjust_time_difference,  # Авто-коррекция времени
                    },
                    'timeout': 30000,  # Увеличенный таймаут
                    'retries': 5,      # Больше попыток
                }
                
                # Добавляем прокси если указан
                if self.proxy_url:
                    exchange_config['proxy'] = self.proxy_url
                    logger.info(f"Using proxy: {self.proxy_url}")
                
                # Добавляем кастомные опции
                if self.options:
                    exchange_config['options'].update(self.options)
                
                # Создаем exchange
                self.exchange = getattr(ccxt, self.exchange_id)(exchange_config)
                
                if self.sandbox:
                    self.exchange.set_sandbox_mode(True)
                    logger.warning("Sandbox mode enabled - using testnet")
                else:
                    logger.info("Live trading mode enabled - REAL MONEY")
                
                # Тест подключения с retry логикой
                max_retries = 3
                for attempt in range(max_retries):
                    try:
                        logger.info(f"Connecting to {self.exchange_id} (attempt {attempt + 1}/{max_retries})...")
                        await self.exchange.load_markets()
                        logger.info(f"[OK] Successfully connected to {self.exchange_id}")
                        logger.info(f"Loaded {len(self.exchange.markets)} markets")
                        break
                    except Exception as e:
                        if attempt == max_retries - 1:
                            raise
                        logger.warning(f"Connection attempt {attempt + 1} failed: {e}. Retrying...")
                        await asyncio.sleep(2 ** attempt)  # Exponential backoff
                
                self._initialized = True
                logger.info(f"DataLoader initialized successfully")
                
            except Exception as e:
                logger.error(f"Failed to initialize exchange: {e}")
                logger.error("Possible solutions:")
                logger.error("  1. Check your internet connection")
                logger.error("  2. If Binance is blocked in your region, set PROXY_URL environment variable")
                logger.error("     Example: export PROXY_URL='http://user:pass@proxy_ip:port'")
                logger.error("  3. Try using a different exchange (e.g., bybit, okx)")
                raise
    
    async def close(self):
        """Close the exchange connection."""
        if self.exchange:
            await self.exchange.close()
            self._initialized = False
    
    async def fetch_ohlcv(
        self, 
        symbol: str, 
        timeframe: str = "1h", 
        limit: int = 500
    ) -> pd.DataFrame:
        """Fetch OHLCV data for a symbol."""
        if not self._initialized:
            await self.initialize()
        
        try:
            ohlcv = await self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
            df = pd.DataFrame(
                ohlcv, 
                columns=['timestamp', 'open', 'high', 'low', 'close', 'volume']
            )
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            df.set_index('timestamp', inplace=True)
            return df
        except Exception as e:
            logger.error(f"Error fetching OHLCV for {symbol}: {e}")
            return pd.DataFrame()
    
    async def fetch_funding_rate(self, symbol: str) -> float:
        """Fetch current funding rate for a symbol."""
        if not self._initialized:
            await self.initialize()
        
        try:
            ticker = await self.exchange.fetch_funding_rate(symbol)
            return ticker.get('fundingRate', 0.0)
        except Exception as e:
            logger.error(f"Error fetching funding rate for {symbol}: {e}")
            return 0.0
    
    async def fetch_open_interest(self, symbol: str) -> float:
        """Fetch open interest for a symbol."""
        if not self._initialized:
            await self.initialize()
        
        try:
            oi = await self.exchange.fetch_open_interest(symbol)
            return oi.get('openInterest', 0.0)
        except Exception as e:
            logger.error(f"Error fetching open interest for {symbol}: {e}")
            return 0.0
    
    async def fetch_ticker(self, symbol: str) -> Dict:
        """Fetch current ticker data."""
        if not self._initialized:
            await self.initialize()
        
        try:
            return await self.exchange.fetch_ticker(symbol)
        except Exception as e:
            logger.error(f"Error fetching ticker for {symbol}: {e}")
            return {}
    
    async def get_market_data(
        self, 
        symbol: str, 
        timeframe: str = "1h"
    ) -> Dict:
        """Get comprehensive market data for a symbol."""
        tasks = [
            self.fetch_ohlcv(symbol, timeframe),
            self.fetch_funding_rate(symbol),
            self.fetch_open_interest(symbol),
            self.fetch_ticker(symbol)
        ]
        
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        ohlcv_df = results[0] if isinstance(results[0], pd.DataFrame) else pd.DataFrame()
        funding_rate = results[1] if isinstance(results[1], (int, float)) else 0.0
        open_interest = results[2] if isinstance(results[2], (int, float)) else 0.0
        ticker = results[3] if isinstance(results[3], dict) else {}
        
        # Get market info for position sizing constraints
        market_info = {}
        if symbol in self.exchange.markets:
            market = self.exchange.markets[symbol]
            if 'limits' in market and 'cost' in market['limits']:
                market_info['min_notional'] = market['limits']['cost'].get('min', 5.0)
            if 'precision' in market and 'amount' in market['precision']:
                market_info['step_size'] = market['precision']['amount']
            elif 'info' in market:
                # Try to get filters from Binance futures
                for filt in market.get('info', {}).get('filters', []):
                    if filt.get('filterType') == 'LOT_SIZE':
                        market_info['step_size'] = float(filt.get('stepSize', 0))
                    elif filt.get('filterType') == 'MIN_NOTIONAL':
                        market_info['min_notional'] = float(filt.get('notional', 5.0))

        return {
            'ohlcv': ohlcv_df,
            'funding_rate': funding_rate,
            'open_interest': open_interest,
            'ticker': ticker,
            'symbol': symbol,
            'timeframe': timeframe,
            'market_info': market_info
        }
    
    async def get_multiple_symbols_data(
        self, 
        symbols: List[str], 
        timeframe: str = "1h"
    ) -> Dict[str, Dict]:
        """Get market data for multiple symbols in parallel."""
        tasks = [self.get_market_data(symbol, timeframe) for symbol in symbols]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        data = {}
        for i, result in enumerate(results):
            if isinstance(result, dict) and 'symbol' in result:
                data[result['symbol']] = result
            else:
                logger.warning(f"Failed to get data for {symbols[i]}")
        
        return data


class MarketStructureAnalyzer:
    """Analyze market structure (HH, HL, LL, LH)."""
    
    @staticmethod
    def find_pivots(df: pd.DataFrame, window: int = 5) -> Tuple[List[int], List[int]]:
        """Find pivot highs and lows in price data."""
        highs = []
        lows = []
        
        for i in range(window, len(df) - window):
            # Pivot high
            if df['high'].iloc[i] == df['high'].iloc[i-window:i+window+1].max():
                highs.append(i)
            # Pivot low
            if df['low'].iloc[i] == df['low'].iloc[i-window:i+window+1].min():
                lows.append(i)
        
        return highs, lows
    
    @staticmethod
    def identify_structure(df: pd.DataFrame, window: int = 5) -> str:
        """
        Identify market structure: Uptrend, Downtrend, or Range.
        Returns: 'uptrend', 'downtrend', or 'range'
        """
        highs, lows = MarketStructureAnalyzer.find_pivots(df, window)
        
        if len(highs) < 2 or len(lows) < 2:
            return 'range'
        
        # Check last few pivots
        recent_highs = highs[-3:]
        recent_lows = lows[-3:]
        
        if len(recent_highs) >= 2 and len(recent_lows) >= 2:
            # Uptrend: HH and HL
            hh = df['high'].iloc[recent_highs[-1]] > df['high'].iloc[recent_highs[-2]]
            hl = df['low'].iloc[recent_lows[-1]] > df['low'].iloc[recent_lows[-2]]
            
            # Downtrend: LL and LH
            ll = df['low'].iloc[recent_lows[-1]] < df['low'].iloc[recent_lows[-2]]
            lh = df['high'].iloc[recent_highs[-1]] < df['high'].iloc[recent_highs[-2]]
            
            if hh and hl:
                return 'uptrend'
            elif ll and lh:
                return 'downtrend'
        
        return 'range'
    
    @staticmethod
    def is_breakout(df: pd.DataFrame, window: int = 20) -> Tuple[bool, str]:
        """
        Detect if price is breaking out of a range.
        Returns: (is_breakout, direction: 'up'/'down'/None)
        """
        if len(df) < window:
            return False, None
        
        recent = df.tail(window)
        resistance = recent['high'].max()
        support = recent['low'].min()
        
        current_price = df['close'].iloc[-1]
        
        # Check for breakout above resistance
        if current_price > resistance * 1.002:  # 0.2% above
            return True, 'up'
        
        # Check for breakdown below support
        if current_price < support * 0.998:  # 0.2% below
            return True, 'down'
        
        return False, None


def calculate_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Calculate Average True Range."""
    high = df['high']
    low = df['low']
    close = df['close']
    
    tr1 = high - low
    tr2 = abs(high - close.shift())
    tr3 = abs(low - close.shift())
    
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.rolling(window=period).mean()
    
    return atr


def calculate_rsi(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Calculate RSI."""
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    
    return rsi


def calculate_volume_profile(df: pd.DataFrame) -> Dict:
    """Calculate volume profile metrics."""
    total_volume = df['volume'].sum()
    avg_volume = df['volume'].mean()
    current_volume = df['volume'].iloc[-1]
    
    volume_ratio = current_volume / avg_volume if avg_volume > 0 else 1.0
    
    return {
        'total_volume': total_volume,
        'avg_volume': avg_volume,
        'current_volume': current_volume,
        'volume_ratio': volume_ratio
    }


async def screen_futures_pairs(
    exchange,
    min_volume_24h: float = 10_000_000,
    top_n: int = 15,
    volatility_min: float = 0.01
) -> List[str]:
    """
    Скринер фьючерсных пар: отбирает наиболее перспективные для торговли.
    
    Критерии:
    - Объем за 24ч >= min_volume_24h (отсеиваем неликвид)
    - Волатильность >= volatility_min (ищем движение)
    - Возвращает top_n лучших пар
    
    Returns: Список символов в формате "BTC/USDT:USDT"
    """
    logger.info(f"Starting futures pairs screener...")
    logger.info(f"Criteria: volume >= ${min_volume_24h:,.0f}, volatility >= {volatility_min*100:.1f}%, top {top_n}")
    
    try:
        # Получаем все рынки (если еще не загружены)
        if not exchange.markets:
            await exchange.load_markets()
        
        # Фильтруем только фьючерсные USDT пары
        futures_usdt_pairs = []
        for symbol, market in exchange.markets.items():
            if (
                market.get('swap', False) and  # Это фьючерс (swap)
                market.get('quote', '') == 'USDT' and  # Котируется в USDT
                market.get('active', True) and  # Активный рынок
                market.get('linear', False)  # Линейный фьючерс (USDT-margined)
            ):
                futures_usdt_pairs.append(symbol)
        
        logger.info(f"Found {len(futures_usdt_pairs)} active USDT-margined futures pairs")
        
        # Получаем тикеры для всех пар (пакетно)
        tickers = await exchange.fetch_tickers(futures_usdt_pairs)
        
        # Анализируем каждую пару
        scored_pairs = []
        for symbol, ticker in tickers.items():
            if ticker is None:
                continue
            
            # Объем за 24ч в quote currency (USDT)
            volume_24h = ticker.get('quoteVolume', 0) or 0
            
            # Волатильность: (high - low) / close за 24ч
            high_24h = ticker.get('high', 0) or 0
            low_24h = ticker.get('low', 0) or 0
            close_price = ticker.get('last', 0) or ticker.get('close', 1)
            
            if close_price <= 0:
                continue
            
            volatility_24h = (high_24h - low_24h) / close_price
            
            # Спред (ликвидность)
            bid = ticker.get('bid', 0) or 0
            ask = ticker.get('ask', 0) or 0
            spread_pct = (ask - bid) / close_price if (ask > 0 and bid > 0) else 0.0001
            
            # Фильтрация по критериям
            if volume_24h < min_volume_24h:
                continue
            if volatility_24h < volatility_min:
                continue
            
            # ФИЛЬТР: Исключаем пары с не-ASCII символами (например, китайские иероглифы)
            # Это предотвращает UnicodeEncodeError в Windows консоли
            try:
                symbol.encode('ascii')
            except UnicodeEncodeError:
                logger.debug(f"Skipping non-ASCII symbol: {symbol}")
                continue
            
            # Убрали проверку на спред - она была слишком строгой
            
            # Скоринг: комбинация объема и волатильности
            # Чем выше объем и волатильность - тем лучше score
            score = (volume_24h / 1_000_000_000) * 0.6 + (volatility_24h * 100) * 0.4
            
            scored_pairs.append({
                'symbol': symbol,
                'volume_24h': volume_24h,
                'volatility_24h': volatility_24h,
                'spread_pct': spread_pct,
                'score': score,
                'price': close_price
            })
        
        # Сортируем по score и берем top_n
        scored_pairs.sort(key=lambda x: x['score'], reverse=True)
        top_pairs = scored_pairs[:top_n]
        
        # Логгируем результаты
        logger.info(f"\n{'='*70}")
        logger.info(f"TOP {len(top_pairs)} FUTURES PAIRS FOR TRADING:")
        logger.info(f"{'='*70}")
        for i, pair in enumerate(top_pairs, 1):
            logger.info(
                f"{i:2}. {pair['symbol']:20} | "
                f"Vol: ${pair['volume_24h']:>12,.0f} | "
                f"Volat: {pair['volatility_24h']*100:>5.2f}% | "
                f"Score: {pair['score']:.3f}"
            )
        logger.info(f"{'='*70}\n")
        
        result_symbols = [p['symbol'] for p in top_pairs]
        
        if not result_symbols:
            logger.warning("No pairs matched the screening criteria. Using fallback list...")
            # Fallback: возвращаем основные пары вручную
            fallback_pairs = [
                "BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT",
                "BNB/USDT:USDT", "XRP/USDT:USDT", "ADA/USDT:USDT",
                "AVAX/USDT:USDT", "DOGE/USDT:USDT", "DOT/USDT:USDT",
                "MATIC/USDT:USDT", "LINK/USDT:USDT", "UNI/USDT:USDT",
                "ATOM/USDT:USDT", "LTC/USDT:USDT", "ETC/USDT:USDT"
            ]
            logger.info(f"Using {len(fallback_pairs)} fallback pairs")
            return fallback_pairs
        
        return result_symbols
        
    except Exception as e:
        logger.error(f"Error during pairs screening: {e}")
        # Fallback: возвращаем основные пары вручную
        fallback_pairs = [
            "BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT",
            "BNB/USDT:USDT", "XRP/USDT:USDT", "ADA/USDT:USDT",
            "AVAX/USDT:USDT", "DOGE/USDT:USDT", "DOT/USDT:USDT",
            "MATIC/USDT:USDT", "LINK/USDT:USDT", "UNI/USDT:USDT",
            "ATOM/USDT:USDT", "LTC/USDT:USDT", "ETC/USDT:USDT"
        ]
        logger.warning(f"Using fallback pairs: {fallback_pairs}")
        return fallback_pairs
