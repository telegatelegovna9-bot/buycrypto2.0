"""
Main Trading Bot.
Orchestrates all components for live trading.
"""
import asyncio
import logging
import os
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from dotenv import load_dotenv
from config.settings import BotConfig, get_default_config
from data.data_loader import (
    DataLoader, 
    MarketStructureAnalyzer, 
    calculate_atr,
    screen_futures_pairs
)
from meta_controller import MetaController
from risk_manager import RiskManager
from execution_engine import ExecutionEngine
from utils.telegram_notifier import TelegramNotifier

# Load environment variables from .env file
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('logs/trading_bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


class TradingBot:
    """
    Main autonomous trading bot.
    Coordinates data loading, signal generation, risk management, and execution.
    """
    
    def __init__(self, config: BotConfig = None):
        # Override sandbox setting from environment variable
        env_sandbox = os.getenv("BINANCE_SANDBOX", "false").lower() == "true"
        
        self.config = config or get_default_config()
        # Force sandbox setting from environment
        self.config.exchange.sandbox = env_sandbox
        
        # Initialize components
        self.data_loader = DataLoader(
            exchange_id=self.config.exchange.exchange_id,
            sandbox=self.config.exchange.sandbox,
            proxy_url=self.config.exchange.proxy_url,
            options=self.config.exchange.options,
            adjust_time_difference=self.config.exchange.adjust_time_difference
        )
        self.meta_controller = MetaController(self.config)
        self.risk_manager = RiskManager(self.config)
        self.execution_engine = ExecutionEngine(self.config)
        self.telegram = TelegramNotifier(self.config)
        
        # State
        self.is_running = False
        self.last_adaptation_time = datetime.now()
        self.adaptation_interval = timedelta(hours=6)
        self.last_screener_run = datetime.now()
        
        # Tracking
        self.active_signals: Dict[str, Dict] = {}
        self.current_timeframe: str = self.config.primary_timeframe  # Dynamic timeframe
    
    async def initialize(self):
        """Initialize all components."""
        logger.info("Initializing trading bot...")
        
        await self.data_loader.initialize()
        await self.execution_engine.initialize()
        
        # Auto-select trading pairs if symbols list is empty
        if not self.config.symbols:
            logger.info("No trading symbols configured. Running pairs screener...")
            selected_pairs = await screen_futures_pairs(
                exchange=self.data_loader.exchange,
                min_volume_24h=self.config.screener_min_volume_24h,
                top_n=self.config.screener_top_n,
                volatility_min=self.config.screener_volatility_min
            )
            self.config.symbols = selected_pairs
            logger.info(f"Selected {len(selected_pairs)} pairs for trading")
        else:
            logger.info(f"Using configured symbols: {self.config.symbols}")
        
        # Sync balance from exchange in live mode
        if not self.config.exchange.sandbox:
            logger.info("Syncing balance from exchange (live mode)...")
            await self.risk_manager.sync_balance_from_exchange(self.execution_engine)
        
        # Select initial timeframe based on market volatility
        self.current_timeframe = await self.select_dynamic_timeframe()
        logger.info(f"Initial timeframe selected: {self.current_timeframe}")
        
        logger.info("Trading bot initialized successfully")
    
    async def shutdown(self):
        """Gracefully shutdown the bot."""
        logger.info("Shutting down trading bot...")
        
        self.is_running = False
        
        await self.data_loader.close()
        await self.execution_engine.close()
        
        logger.info("Trading bot shut down")
    
    async def fetch_market_data(self) -> Dict[str, Dict]:
        """Fetch market data for all configured symbols."""
        data = await self.data_loader.get_multiple_symbols_data(
            self.config.symbols,
            timeframe=self.current_timeframe  # Use dynamic timeframe
        )
        return data
    
    async def select_dynamic_timeframe(self) -> str:
        """
        Select optimal timeframe based on current market volatility.
        High volatility -> lower timeframe (1m, 3m)
        Low volatility -> higher timeframe (5m)
        """
        if not self.config.symbols:
            return self.config.primary_timeframe
        
        # Sample a few symbols to determine overall market volatility
        sample_symbols = self.config.symbols[:min(5, len(self.config.symbols))]
        avg_volatility = 0.0
        
        for symbol in sample_symbols:
            try:
                df = await self.data_loader.fetch_ohlcv(symbol, "1h", limit=24)
                if len(df) > 0:
                    # Calculate hourly volatility (high-low range / close)
                    hourly_vol = (df['high'] - df['low']) / df['close']
                    avg_volatility += hourly_vol.mean()
            except Exception:
                continue
        
        avg_volatility /= max(len(sample_symbols), 1)
        
        # Select timeframe based on average volatility
        if avg_volatility > 0.03:  # High volatility (>3% hourly)
            selected_tf = "1m"
        elif avg_volatility > 0.015:  # Medium volatility (>1.5% hourly)
            selected_tf = "3m"
        else:  # Low volatility
            selected_tf = "5m"
        
        logger.info(f"Market volatility: {avg_volatility:.2%}, selected timeframe: {selected_tf}")
        return selected_tf
    
    async def update_screener_if_needed(self):
        """Update pairs list if screener interval has passed."""
        now = datetime.now()
        if now - self.last_screener_run >= timedelta(seconds=self.config.screener_update_interval):
            logger.info("Updating pairs screener...")
            selected_pairs = await screen_futures_pairs(
                exchange=self.data_loader.exchange,
                min_volume_24h=self.config.screener_min_volume_24h,
                top_n=self.config.screener_top_n,
                volatility_min=self.config.screener_volatility_min
            )
            old_count = len(self.config.symbols)
            self.config.symbols = selected_pairs
            self.last_screener_run = now
            
            # Update timeframe based on new market conditions
            self.current_timeframe = await self.select_dynamic_timeframe()
            
            logger.info(f"Screener updated: {old_count} -> {len(selected_pairs)} pairs, timeframe: {self.current_timeframe}")
    
    async def analyze_and_trade(self):
        """Main analysis and trading loop iteration."""
        logger.info("Analyzing markets...")
        
        # Fetch market data
        market_data = await self.fetch_market_data()
        
        if not market_data:
            logger.warning("No market data available")
            return
        
        # Analyze each symbol
        for symbol, data in market_data.items():
            df = data.get('ohlcv')
            
            if df is None or len(df) < 50:
                logger.debug(f"Insufficient data for {symbol}")
                continue
            
            # Get current price from the latest candle
            current_price = df['close'].iloc[-1]
            
            # Get signals from meta-controller (new adaptive system)
            market_data = {
                'oi': data.get('open_interest', []),
                'funding_rate': data.get('funding_rate', 0.0),
                'htf_trend': 'UP'  # Can be enhanced with actual HTF analysis
            }
            
            result = self.meta_controller.aggregate_signals(df, market_data)
            
            decision = result.get('decision', {})
            regime = result.get('regime', 'UNKNOWN')
            active_strategies = result.get('active_strategies', [])
            
            logger.info(f"Market Regime: {regime}")
            logger.info(f"Active Strategies: {active_strategies}")
            
            if not decision or decision.get('direction') == "NEUTRAL":
                logger.debug(f"No actionable signal for {symbol} (Regime: {regime})")
                continue
            
            direction = decision['direction']
            confidence = decision.get('confidence', 0.0)
            source_strategy = decision.get('source', 'Unknown')
            
            # Safe logging for symbols with non-ASCII characters
            try:
                logger.info(
                    f"Signal for {symbol}: {direction} "
                    f"(confidence: {confidence:.2f}, source: {source_strategy})"
                )
            except UnicodeEncodeError:
                # Fallback for Windows console with limited encoding
                safe_symbol = symbol.encode('ascii', errors='replace').decode('ascii')
                logger.info(
                    f"Signal for {safe_symbol}: {direction} "
                    f"(confidence: {confidence:.2f}, source: {source_strategy})"
                )
            
            # Skip neutral signals - no action needed
            if direction == 'neutral':
                logger.debug(f"Skipping neutral signal for {symbol}")
                continue
            
            # Check if we should trade this signal
            if not self.risk_manager.can_open_position(symbol):
                logger.debug(f"Cannot open position for {symbol}")
                continue
            
            # Check if bot should reduce exposure
            if self.meta_controller.should_reduce_exposure():
                logger.warning("Reducing exposure due to poor strategy performance")
                continue
            
            # Execute the trade
            # Get market info for position sizing constraints
            market_info = data.get('market_info', {})
            
            # Calculate SL/TP before creating position
            if direction == 'long':
                calculated_sl = current_price * (1 - self.config.risk.stop_loss_pct)
                calculated_tp = current_price * (1 + self.config.risk.take_profit_pct)
            else:
                calculated_sl = current_price * (1 + self.config.risk.stop_loss_pct)
                calculated_tp = current_price * (1 - self.config.risk.take_profit_pct)
            
            position = self.risk_manager.create_position(
                symbol=symbol,
                direction=direction,
                entry_price=current_price,
                stop_loss=calculated_sl,
                take_profit=calculated_tp,
                confidence=confidence,
                market_info=market_info
            )
            
            if position:
                # Execute on exchange (or simulate in backtest mode)
                if not self.config.exchange.sandbox:
                    # Create signal dict with all required fields
                    signal_dict = {
                        'direction': direction,
                        'entry_price': current_price,
                        'symbol': symbol,
                        'stop_loss': position.stop_loss,
                        'take_profit': position.take_profit
                    }
                    logger.info(f"Executing signal: {direction} on {symbol}, SL={position.stop_loss:.4f}, TP={position.take_profit:.4f}")
                    success = await self.execution_engine.execute_signal(
                        signal_dict,
                        position.size,
                        position.leverage
                    )
                    
                    if success:
                        logger.info(f"Successfully executed trade on {symbol}")
                        
                        # Notify via Telegram
                        await self.telegram.notify_entry(
                            symbol=symbol,
                            direction=direction,
                            entry_price=current_price,
                            stop_loss=position.stop_loss,
                            take_profit=position.take_profit,
                            leverage=position.leverage,
                            confidence=confidence,
                            balance=self.risk_manager.balance
                        )
                
                # Track active signal
                self.active_signals[symbol] = {
                    'signal': {'direction': direction, 'entry': current_price, 'confidence': confidence},
                    'position': position,
                    'entry_time': datetime.now(),
                    'strategy': source_strategy
                }
    
    async def manage_positions(self):
        """
        Manage existing positions with FAST monitoring.
        Checks SL/TP every iteration and manages trailing stops.
        CRITICAL FIX: Separate SL/TP detection from closure to avoid race conditions.
        """
        if not self.risk_manager.positions:
            return
        
        # Fetch current prices for ALL open positions IMMEDIATELY
        prices = {}
        for symbol in self.risk_manager.positions.keys():
            try:
                ticker = await self.data_loader.fetch_ticker(symbol)
                if ticker and 'last' in ticker:
                    prices[symbol] = ticker['last']
            except Exception as e:
                logger.error(f"Error fetching price for {symbol}: {e}")
        
        if not prices:
            logger.warning("No prices fetched for position monitoring")
            return
        
        # Update positions with current prices
        self.risk_manager.update_positions(prices)
        
        # PHASE 1: Detect positions that need to be closed (SL/TP hits)
        positions_to_close = []
        for symbol, position in list(self.risk_manager.positions.items()):
            current_price = prices.get(symbol)
            if not current_price:
                continue
            
            # Check for SL/TP hit
            exit_reason = self.risk_manager.check_stop_loss_take_profit(symbol, current_price)
            
            if exit_reason:
                positions_to_close.append((symbol, current_price, exit_reason))
        
        # PHASE 2: Execute closures SEPARATELY to avoid modification during iteration
        for symbol, close_price, exit_reason in positions_to_close:
            # Сначала закрываем позицию на бирже (в live режиме)
            if not self.config.exchange.sandbox:
                close_price_exchange = await self.execution_engine.close_position(symbol)
                if close_price_exchange > 0:
                    close_price = close_price_exchange
                    logger.info(f"✓ Position closed on exchange @ {close_price_exchange}")
                else:
                    logger.error(f"✗ FAILED to close position on exchange for {symbol}")
                    continue  # Skip local close if exchange close failed
            
            # Закрываем позицию локально
            pnl = self.risk_manager.close_position(symbol, close_price, exit_reason)
            
            # Определяем, какие стратегии были задействованы
            strategies_used = []
            if symbol in self.active_signals:
                strategy_val = self.active_signals[symbol].get('strategy', [])
                if isinstance(strategy_val, list):
                    strategies_used = strategy_val
                elif strategy_val:
                    strategies_used = [strategy_val]
                
                # Обновляем производительность стратегий
                is_winner = pnl > 0
                for strategy_name in strategies_used:
                    if strategy_name:
                        self.meta_controller.update_strategy_performance(
                            strategy_name, pnl, is_winner
                        )
                
                # Удаляем из активных сигналов
                del self.active_signals[symbol]
            
            # Уведомляем через Telegram
            if self.telegram.enabled:
                if exit_reason == 'stop_loss':
                    await self.telegram.notify_stop_loss(symbol, pnl, self.risk_manager.balance)
                elif exit_reason == 'take_profit':
                    await self.telegram.notify_take_profit(symbol, pnl, self.risk_manager.balance)
        
        # PHASE 3: Manage remaining active positions (trailing stop, breakeven)
        for symbol, position in list(self.risk_manager.positions.items()):
            current_price = prices.get(symbol)
            if not current_price:
                continue
            
            # Only manage positions that are still open (not closed in phase 2)
            if position.unrealized_pnl > 0:
                # Move stop loss to breakeven when profitable
                self.risk_manager.move_stop_loss_to_breakeven(symbol, current_price)
                
                # Trail stop loss using ATR
                try:
                    df = await self.data_loader.fetch_ohlcv(symbol, self.current_timeframe, limit=50)
                    if len(df) > 14:
                        atr = calculate_atr(df, period=14)
                        current_atr = atr.iloc[-1] if hasattr(atr, 'iloc') else atr
                        self.risk_manager.trail_stop_loss(symbol, current_price, current_atr)
                        logger.debug(f"Updated trailing stop for {symbol}: SL={position.stop_loss:.4f}")
                except Exception as e:
                    logger.debug(f"Error updating trailing stop for {symbol}: {e}")
    
    async def check_adaptation(self):
        """Check if strategies should be adapted."""
        now = datetime.now()
        
        if now - self.last_adaptation_time >= self.adaptation_interval:
            logger.info("Running strategy adaptation...")
            self.meta_controller.adapt_strategy_weights()
            self.last_adaptation_time = now
    
    async def run_loop(self):
        """Main trading loop."""
        logger.info("Starting trading loop...")
        self.is_running = True
        
        iteration = 0
        
        while self.is_running:
            try:
                iteration += 1
                logger.info(f"=== Iteration {iteration} ===")
                
                # Update screener if interval has passed (every 5 minutes)
                await self.update_screener_if_needed()
                
                # Analyze markets and potentially open new positions
                await self.analyze_and_trade()
                
                # Manage existing positions
                await self.manage_positions()
                
                # Check for strategy adaptation
                await self.check_adaptation()
                
                # Log status
                summary = self.risk_manager.get_summary()
                logger.info(
                    f"Balance: ${summary['balance']:.2f}, "
                    f"Positions: {summary['open_positions']}, "
                    f"Drawdown: {summary['current_drawdown']:.2%}, "
                    f"Timeframe: {self.current_timeframe}"
                )
                
                # Wait before next iteration
                await asyncio.sleep(60)  # 1 minute for faster reaction
                
            except KeyboardInterrupt:
                logger.info("Interrupted by user")
                break
            except Exception as e:
                logger.error(f"Error in trading loop: {e}", exc_info=True)
                await asyncio.sleep(60)  # Wait before retrying
        
        logger.info("Trading loop stopped")
    
    async def run(self):
        """Run the trading bot."""
        try:
            await self.initialize()
            
            # Log trading mode prominently
            if self.config.exchange.sandbox:
                logger.warning("=" * 60)
                logger.warning("SANDBOX MODE - Using testnet (fake money)")
                logger.warning("=" * 60)
            else:
                logger.info("=" * 60)
                logger.info("LIVE TRADING MODE - REAL MONEY")
                api_key = self.config.exchange.api_key
                if api_key and len(api_key) > 15:
                    logger.info(f"API Key: {api_key[:10]}...{api_key[-5:]}")
                logger.info("=" * 60)
            
            # Send startup notification
            if self.telegram.enabled:
                mode = "TESTNET" if self.config.exchange.sandbox else "LIVE"
                await self.telegram.send_alert(
                    f"Trading Bot Started ({mode})",
                    f"Bot is now running with {len(self.config.symbols)} symbols"
                )
            
            await self.run_loop()
            
        except Exception as e:
            logger.error(f"Fatal error: {e}", exc_info=True)
            
            if self.telegram.enabled:
                await self.telegram.send_alert(
                    "Trading Bot Error",
                    f"Fatal error occurred: {str(e)}"
                )
        
        finally:
            await self.shutdown()


async def main():
    """Entry point for the trading bot."""
    config = get_default_config()
    
    # Override with environment variables if needed
    env_api_key = os.getenv("BINANCE_API_KEY", "")
    env_api_secret = os.getenv("BINANCE_API_SECRET", "")
    env_telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    env_telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    env_sandbox = os.getenv("BINANCE_SANDBOX", "false").lower() == "true"  # Default to live trading
    
    if env_api_key:
        config.exchange.api_key = env_api_key
    if env_api_secret:
        config.exchange.api_secret = env_api_secret
    config.exchange.sandbox = env_sandbox
    
    if env_telegram_token and env_telegram_chat_id:
        config.telegram.enabled = True
        config.telegram.bot_token = env_telegram_token
        config.telegram.chat_id = env_telegram_chat_id
    
    bot = TradingBot(config)
    await bot.run()


if __name__ == "__main__":
    asyncio.run(main())
