"""
Configuration settings for the crypto trading bot.
"""
from dataclasses import dataclass
from typing import List, Optional
import os


@dataclass
class ExchangeConfig:
    """Exchange connection settings."""
    exchange_id: str = "binance"
    api_key: str = os.getenv("BINANCE_API_KEY", "")
    api_secret: str = os.getenv("BINANCE_API_SECRET", "")
    sandbox: bool = False  # Use testnet - set to True for testing without real money
    futures: bool = True
    
    # Time synchronization
    # Binance requires precise time. If your PC clock is off, this helps.
    # The bot will calculate the time offset automatically on startup.
    adjust_time_difference: bool = True
    
    # Proxy support (для обхода блокировок)
    # Примеры: "http://user:pass@proxy_ip:port" или "socks5h://127.0.0.1:9050"
    proxy_url: str = os.getenv("PROXY_URL", "")
    
    # Альтернативные URL для Binance (если основной заблокирован)
    options: dict = None
    
    def __post_init__(self):
        if self.options is None:
            self.options = {}


@dataclass
class RiskConfig:
    """Risk management settings."""
    risk_per_trade: float = 0.01  # 1% of balance
    max_positions: int = 3  # Changed from 2 to 3 simultaneous positions
    min_rr_ratio: float = 2.0  # Minimum risk-reward ratio 1:2
    max_drawdown: float = 0.15  # 15% max drawdown before stopping
    stop_loss_pct: float = 0.02  # Default 2% stop loss
    take_profit_pct: float = 0.04  # Default 4% take profit


@dataclass
class LeverageConfig:
    """Dynamic leverage settings."""
    min_leverage: int = 2
    max_leverage: int = 20  # Increased from 10 to 20
    low_confidence_threshold: float = 0.4
    medium_confidence_threshold: float = 0.6
    high_confidence_threshold: float = 0.8


@dataclass
class StrategyConfig:
    """Strategy weights and thresholds."""
    min_confidence: float = 0.5
    adaptation_window: int = 25  # Number of trades for adaptation
    warmup_trades: int = 5  # Minimum trades before adaptation kicks in


@dataclass
class BacktestConfig:
    """Backtesting settings."""
    initial_balance: float = 10000.0
    commission_rate: float = 0.0004  # 0.04% per trade
    slippage_rate: float = 0.0001  # 0.01% slippage
    data_dir: str = "data"


@dataclass
class TelegramConfig:
    """Telegram notification settings."""
    enabled: bool = False
    bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")


@dataclass
class BotConfig:
    """Main bot configuration."""
    exchange: ExchangeConfig = None
    risk: RiskConfig = None
    leverage: LeverageConfig = None
    strategy: StrategyConfig = None
    backtest: BacktestConfig = None
    telegram: TelegramConfig = None
    
    # Trading pairs to monitor
    # Если оставить пустым [], бот будет сам выбирать пары через скринер
    symbols: List[str] = None
    
    # Screener settings (если symbols пуст)
    screener_min_volume_24h: float = 10_000_000  # Мин. объем за 24ч в USDT
    screener_top_n: int = 15  # Сколько лучших пар отобрать для анализа
    screener_volatility_min: float = 0.01  # Мин. волатильность (1%)
    screener_update_interval: int = 300  # Update pairs list every 300 seconds (5 minutes)
    
    # Timeframes - динамический выбор на основе волатильности
    primary_timeframe: str = "5m"  # Основной таймфрейм
    dynamic_timeframes: List[str] = None  # Доступные таймфреймы для динамического выбора
    
    def __post_init__(self):
        if self.exchange is None:
            self.exchange = ExchangeConfig()
        if self.risk is None:
            self.risk = RiskConfig()
        if self.leverage is None:
            self.leverage = LeverageConfig()
        if self.strategy is None:
            self.strategy = StrategyConfig()
        if self.backtest is None:
            self.backtest = BacktestConfig()
        if self.telegram is None:
            self.telegram = TelegramConfig()
        if self.symbols is None:
            # Пустой список = автоматический выбор пар через скринер
            self.symbols = []
        if self.dynamic_timeframes is None:
            # Доступные таймфреймы для динамического выбора
            self.dynamic_timeframes = ["1m", "3m", "5m"]


def get_default_config() -> BotConfig:
    """Return default bot configuration."""
    return BotConfig()
