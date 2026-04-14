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
from position_monitor import PositionMonitor
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
        
        # Initialize Position Monitor for advanced position management
        self.position_monitor = None  # Will be initialized after components
        
        # State
        self.is_running = False
        self.last_adaptation_time = datetime.now()
        self.adaptation_interval = timedelta(hours=6)
        self.last_screener_run = datetime.now()
        self.last_analysis_time = datetime.min
        
        # Tracking
        self.active_signals: Dict[str, Dict] = {}
        self.current_timeframe: str = self.config.primary_timeframe  # Dynamic timeframe
    
    async def initialize(self):
        """Initialize all components."""
        logger.info("Initializing trading bot...")
        
        await self.data_loader.initialize()
        await self.execution_engine.initialize()
        
        # Initialize Position Monitor after execution engine is ready
        self.position_monitor = PositionMonitor(
            risk_manager=self.risk_manager,
            order_executor=self.execution_engine,
            data_loader=self.data_loader,
            config=self.config
        )
        await self.position_monitor.start_monitoring()
        logger.info("Position Monitor initialized and started")
        
        # CRITICAL: Sync positions from exchange to recover open positions after restart
        await self.sync_positions_from_exchange()
        
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
    
    async def sync_positions_from_exchange(self):
        """
        CRITICAL: Sync open positions from exchange to local state.
        
        This solves two major problems:
        1. Bot restart: If bot was restarted with open positions, it will recover them
        2. Manual close: If position was closed on exchange but not in bot, it will be removed
        
        This ensures the bot always has accurate position data.
        """
        logger.info("[SYNC] Checking for open positions on exchange...")
        
        try:
            # Fetch all positions from exchange
            all_positions = await self.execution_engine.exchange.fetch_positions()
            
            recovered_count = 0
            removed_count = 0
            
            # Create a set of symbols that have open positions on exchange
            exchange_positions = {}
            
            for pos in all_positions:
                contracts = float(pos.get('contracts', 0))
                symbol = pos.get('symbol')
                
                # Skip if no position or zero contracts
                if contracts == 0:
                    continue
                
                # Determine position direction
                side = str(pos.get('side', '')).lower()
                raw_amt = float(pos.get('info', {}).get('positionAmt', 0) or 0)
                
                if raw_amt > 0 or side == 'long':
                    direction = 'long'
                elif raw_amt < 0 or side == 'short':
                    direction = 'short'
                else:
                    continue  # Skip unknown direction
                
                # Get current price
                entry_price = float(pos.get('entryPrice', 0))
                current_price = float(pos.get('markPrice', entry_price))
                
                exchange_positions[symbol] = {
                    'direction': direction,
                    'contracts': abs(contracts),
                    'entry_price': entry_price,
                    'current_price': current_price,
                    'leverage': int(pos.get('leverage', 1)),
                    'exchange_data': pos
                }
            
            # Check each exchange position against local state
            for symbol, ex_pos in exchange_positions.items():
                if symbol not in self.risk_manager.positions:
                    # Position exists on exchange but not locally - RECOVER IT
                    logger.warning(
                        f"[SYNC RECOVER] Found open {ex_pos['direction']} position on exchange "
                        f"for {symbol} ({ex_pos['contracts']} contracts @ {ex_pos['entry_price']:.4f}) "
                        f"that was not tracked locally. Recovering..."
                    )
                    
                    # Calculate approximate SL/TP (will be refined by PositionMonitor)
                    if ex_pos['direction'] == 'long':
                        sl = ex_pos['entry_price'] * 0.98  # Approximate 2% SL
                        tp = ex_pos['entry_price'] * 1.04  # Approximate 4% TP
                    else:
                        sl = ex_pos['entry_price'] * 1.02
                        tp = ex_pos['entry_price'] * 0.96
                    
                    # Create position object
                    position = self.risk_manager.create_position(
                        symbol=symbol,
                        direction=ex_pos['direction'],
                        entry_price=ex_pos['entry_price'],
                        stop_loss=sl,
                        take_profit=tp,
                        confidence=0.5,  # Unknown confidence
                        market_info={}
                    )
                    
                    if position:
                        # Adjust size to match exchange
                        position.size = ex_pos['contracts']
                        position.leverage = ex_pos['leverage']
                        
                        # Update PnL with current price
                        position.update_unrealized_pnl(ex_pos['current_price'])
                        
                        # Add to active signals for tracking
                        self.active_signals[symbol] = {
                            'signal': {
                                'direction': ex_pos['direction'],
                                'entry': ex_pos['entry_price'],
                                'confidence': 0.5
                            },
                            'position': position,
                            'entry_time': datetime.now(),
                            'strategy': 'Recovered'
                        }
                        
                        recovered_count += 1
                        logger.info(f"[SYNC OK] Recovered position: {symbol}")
                else:
                    # Position exists both locally and on exchange - verify consistency
                    local_pos = self.risk_manager.positions[symbol]
                    if abs(local_pos.size - ex_pos['contracts']) > 0.01:
                        logger.warning(
                            f"[SYNC MISMATCH] {symbol}: Local size={local_pos.size}, "
                            f"Exchange size={ex_pos['contracts']}. Updating local..."
                        )
                        local_pos.size = ex_pos['contracts']
            
            # Check for positions that exist locally but NOT on exchange (manually closed or liquidated)
            symbols_to_remove = []
            for symbol in list(self.risk_manager.positions.keys()):
                if symbol not in exchange_positions:
                    logger.warning(
                        f"[SYNC REMOVE] Position {symbol} exists locally but NOT on exchange. "
                        f"It was likely closed manually or liquidated. Removing from local state..."
                    )
                    symbols_to_remove.append(symbol)
                    removed_count += 1
            
            # Remove stale positions
            for symbol in symbols_to_remove:
                # Remove from risk manager
                self.risk_manager.positions.pop(symbol, None)
                # Remove from active signals
                self.active_signals.pop(symbol, None)
                # Remove from position monitor states
                if self.position_monitor and symbol in self.position_monitor.position_states:
                    self.position_monitor.position_states.pop(symbol, None)
            
            # Summary
            if recovered_count > 0:
                logger.critical(f"[SYNC COMPLETE] Recovered {recovered_count} open positions from exchange")
            if removed_count > 0:
                logger.critical(f"[SYNC COMPLETE] Removed {removed_count} stale positions (not on exchange)")
            if recovered_count == 0 and removed_count == 0:
                logger.info("[SYNC COMPLETE] All positions synchronized correctly")
            
            # Sync balance after position recovery
            if not self.config.exchange.sandbox:
                logger.info("[SYNC] Updating balance after position sync...")
                await self.risk_manager.sync_balance_from_exchange(self.execution_engine)
            
        except Exception as e:
            logger.error(f"[SYNC ERROR] Failed to sync positions from exchange: {e}", exc_info=True)
            logger.warning("[SYNC WARNING] Continuing with local position state (may be outdated)")
    
    async def shutdown(self):
        """Gracefully shutdown the bot."""
        logger.info("Shutting down trading bot...")
        
        self.is_running = False
        
        # Stop position monitor
        if self.position_monitor:
            await self.position_monitor.stop_monitoring()
            logger.info("Position Monitor stopped")
        
        # Save strategy stats before shutting down
        if hasattr(self.meta_controller, '_save_strategy_stats'):
            self.meta_controller._save_strategy_stats()
            logger.info("Strategy stats saved successfully")
        
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
            # FIX: Получаем список стратегий из правильного ключа 'source_strategies'
            source_strategies = decision.get('source_strategies', [])
            # Если список пуст, пробуем старый ключ 'source' для совместимости
            if not source_strategies:
                single_source = decision.get('source', None)
                if single_source and single_source != 'Unknown':
                    source_strategies = [single_source]
            
            # Для логирования берем первую стратегию или "Multi" если их несколько
            if len(source_strategies) == 1:
                source_strategy = source_strategies[0]
            elif len(source_strategies) > 1:
                source_strategy = f"Multi({len(source_strategies)})"
            else:
                source_strategy = 'Unknown'
            
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
                    else:
                        logger.error(f"Execution failed for {symbol}, removing local position to keep state consistent")
                        self.risk_manager.positions.pop(symbol, None)
                        continue
                
                # Track active signal with FULL strategy list
                self.active_signals[symbol] = {
                    'signal': {'direction': direction, 'entry': current_price, 'confidence': confidence},
                    'position': position,
                    'entry_time': datetime.now(),
                    'strategy': source_strategies if len(source_strategies) > 1 else (source_strategies[0] if source_strategies else 'Unknown')
                }
    
    async def manage_positions(self):
        """
        Manage existing positions with FAST monitoring.
        DELEGATES to PositionMonitor for advanced management:
        - Real-time SL/TP checks every 1 second
        - Trailing stops with ATR
        - Breakeven moves
        - Dynamic TP adjustment based on indicators
        - Early exit on indicator signals (RSI, MACD divergence)
        
        Basic SL/TP check remains as fallback safety net.
        """
        if not self.risk_manager.positions:
            return
        
        # PositionMonitor handles ALL advanced management in background (1s cadence)
        # This method now serves as a safety net and status reporter
        
        # Fetch current prices for status logging
        prices = {}
        for symbol in self.risk_manager.positions.keys():
            try:
                ticker = await self.data_loader.fetch_ticker(symbol)
                if ticker and 'last' in ticker:
                    prices[symbol] = ticker['last']
            except Exception as e:
                logger.error(f"Error fetching price for {symbol}: {e}")
        
        if not prices:
            logger.debug("[MANAGE] No prices fetched for status check")
            return
        
        # Update positions with current prices
        self.risk_manager.update_positions(prices)
        
        # Log position status (PositionMonitor handles actual management)
        for symbol, position in list(self.risk_manager.positions.items()):
            current_price = prices.get(symbol)
            if not current_price:
                continue
            
            pnl_pct = position.get_pnl_pct(current_price)
            distance_to_sl = abs(current_price - position.stop_loss) / current_price * 100
            distance_to_tp = abs(position.take_profit - current_price) / current_price * 100
            
            logger.debug(
                f"[POSITION STATUS] {symbol} | {position.direction.upper()} | "
                f"PnL: {pnl_pct:+.2%} | Price: {current_price:.4f} | "
                f"SL: {position.stop_loss:.4f} (-{distance_to_sl:.2f}%) | "
                f"TP: {position.take_profit:.4f} (+{distance_to_tp:.2f}%) | "
                f"Monitored by PositionMonitor"
            )
        
        # FALLBACK SAFETY NET: Check for critical SL/TP hits
        # (PositionMonitor should handle this, but this is a backup)
        positions_to_close = []
        for symbol, position in list(self.risk_manager.positions.items()):
            current_price = prices.get(symbol)
            if not current_price:
                continue
            
            # Check for SL/TP hit
            exit_reason = self.risk_manager.check_stop_loss_take_profit(symbol, current_price)
            
            if exit_reason:
                logger.warning(f"[FALLBACK] {symbol} triggered {exit_reason} - PositionMonitor may have missed it")
                positions_to_close.append((symbol, current_price, exit_reason))
        
        # Execute closures for positions that hit SL/TP
        for symbol, close_price, exit_reason in positions_to_close:
            await self._execute_position_closure(symbol, close_price, exit_reason)
        
    async def _execute_position_closure(self, symbol: str, close_price: float, exit_reason: str):
        """
        Execute position closure on exchange and locally.
        Shared method for fallback and emergency closures.
        """
        # First close on exchange (in live mode)
        if not self.config.exchange.sandbox:
            close_price_exchange = await self.execution_engine.close_position(symbol)
            if close_price_exchange > 0:
                close_price = close_price_exchange
                logger.info(f"[CLOSE OK] Position closed on exchange @ {close_price_exchange}")
            else:
                logger.error(f"[CLOSE FAIL] Failed to close position on exchange for {symbol}")
                return  # Skip local close if exchange close failed
        
        # Close position locally
        pnl = self.risk_manager.close_position(symbol, close_price, exit_reason)
        
        # Determine which strategies were used - IMPROVED VERSION
        strategies_used = []
        if symbol in self.active_signals:
            strategy_val = self.active_signals[symbol].get('strategy', None)
            
            # FIX: Handle all variants: list, string, None
            if strategy_val:
                if isinstance(strategy_val, list):
                    # Filter 'Unknown' from list
                    strategies_used = [s for s in strategy_val if s and s != 'Unknown']
                elif isinstance(strategy_val, str) and strategy_val != 'Unknown':
                    strategies_used = [strategy_val]
            
            # If still empty, try to get from position object
            if not strategies_used:
                position_obj = self.active_signals[symbol].get('position')
                if position_obj and hasattr(position_obj, 'strategy'):
                    strat = position_obj.strategy
                    if strat:
                        if isinstance(strat, list):
                            strategies_used = [s for s in strat if s and s != 'Unknown']
                        elif isinstance(strat, str) and strat != 'Unknown':
                            strategies_used = [strat]
            
            # Update strategy performance
            is_winner = pnl > 0
            if strategies_used:
                for strategy_name in strategies_used:
                    if strategy_name and strategy_name != 'Unknown':
                        self.meta_controller.update_strategy_performance(
                            strategy_name, pnl, is_winner, exit_reason
                        )
                        logger.info(f"[STATS UPDATED] {strategy_name}: PnL={pnl:.2f}, Win={is_winner}, Reason={exit_reason}")
            else:
                # If strategy not found, use default name based on direction
                position_obj = self.risk_manager.positions.get(symbol) if symbol in self.risk_manager.positions else None
                if position_obj:
                    default_strategy = f"Default_{position_obj.direction}"
                    self.meta_controller.update_strategy_performance(
                        default_strategy, pnl, is_winner, exit_reason
                    )
                    logger.warning(f"[STATS WARNING] No strategy found for {symbol}, using {default_strategy}: PnL={pnl:.2f}, Win={is_winner}")
                else:
                    logger.error(f"[STATS ERROR] Cannot determine strategy for {symbol}, stats NOT updated")

            # Remove from active signals
            del self.active_signals[symbol]
        else:
            logger.warning(f"[STATS WARNING] No active signal found for closed position {symbol}")

        
        # Notify via Telegram (always send final trade summary)
        if self.telegram.enabled:
            last_trade = self.risk_manager.closed_trades[-1] if self.risk_manager.closed_trades else None
            if last_trade:
                entry_price = last_trade.get('entry_price', 0.0)
                exit_price = last_trade.get('close_price', close_price)
                direction = last_trade.get('direction', 'long')
                pnl_pct = ((exit_price - entry_price) / entry_price) if direction == 'long' and entry_price > 0 else (
                    (entry_price - exit_price) / entry_price if entry_price > 0 else 0.0
                )
                await self.telegram.notify_exit(
                    symbol=symbol,
                    direction=direction,
                    entry_price=entry_price,
                    exit_price=exit_price,
                    pnl=pnl,
                    pnl_pct=pnl_pct,
                    reason=exit_reason,
                    balance=self.risk_manager.balance
                )
            if exit_reason == 'stop_loss':
                await self.telegram.notify_stop_loss(symbol, pnl, self.risk_manager.balance)
            elif exit_reason == 'take_profit':
                await self.telegram.notify_take_profit(symbol, pnl, self.risk_manager.balance)
        
        # NOTE: PositionMonitor handles advanced exit management (trailing, breakeven, dynamic TP)
        # The local _apply_local_exit_strategy is kept as a secondary backup
        # PositionMonitor runs every 1s with full indicator analysis (RSI, MACD, Volume)
    
    async def check_adaptation(self):
        """Check if strategies should be adapted."""
        now = datetime.now()
        
        if now - self.last_adaptation_time >= self.adaptation_interval:
            logger.info("Running strategy adaptation...")
            self.meta_controller.adapt_strategy_weights()
            self.last_adaptation_time = now
            
            # Save stats after adaptation
            self.meta_controller._save_strategy_stats()
            logger.info("Strategy stats saved after adaptation")
    
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
                
                # Analyze markets and potentially open new positions every 60s
                now = datetime.now()
                if (now - self.last_analysis_time).total_seconds() >= 60:
                    await self.analyze_and_trade()
                    self.last_analysis_time = now
                
                # If there are open positions: manage each second.
                # If no positions: manage only together with 60s analysis cadence.
                if self.risk_manager.positions:
                    await self.manage_positions()
                elif (now - self.last_analysis_time).total_seconds() < 2:
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
                await asyncio.sleep(1)
                
            except KeyboardInterrupt:
                logger.info("Interrupted by user")
                break
            except Exception as e:
                logger.error(f"Error in trading loop: {e}", exc_info=True)
                await asyncio.sleep(1)  # Wait before retrying
        
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
