"""
Execution Engine: Handles order execution and position management.
Uses native Binance API for exchange-side SL/TP with algo endpoint fallback logic.
"""
import ccxt.async_support as ccxt
import asyncio
from typing import Dict, Optional, List
import logging
from strategies.base_strategy import Signal
from risk_manager import Position
from binance_native_api import BinanceFuturesAPI

logger = logging.getLogger(__name__)


class ExecutionEngine:
    """
    Handles all exchange interactions for order execution.
    Supports market orders, limit orders, stop-loss, and take-profit.
    """
    
    def __init__(self, config):
        self.config = config
        self.exchange_config = config.exchange
        
        self.exchange = None
        self.binance_api = None
        self._initialized = False
        
        # Order tracking
        self.open_orders: Dict[str, List[Dict]] = {}
        self.order_history: List[Dict] = []
        
        # SL/TP order tracking per symbol
        self.sl_orders: Dict[str, Dict] = {}
        self.tp_orders: Dict[str, Dict] = {}
    
    async def initialize(self):
        """Initialize exchange connection and native Binance client."""
        if not self._initialized:
            # Базовая конфигурация с корректировкой времени
            base_config = {
                'apiKey': self.exchange_config.api_key,
                'secret': self.exchange_config.api_secret,
                'enableRateLimit': True,
                'options': {
                    'defaultType': 'future',
                    'adjustForTimeDifference': True,  # Авто-коррекция времени
                    'recvWindow': 60000,  # 60 секунд окно для timestamp
                },
                'timeout': 30000,
                'retries': 5,
            }
            
            # Передаем proxy_url если он указан
            if self.exchange_config.proxy_url:
                base_config['proxy'] = self.exchange_config.proxy_url
            
            self.exchange = getattr(ccxt, self.exchange_config.exchange_id)(base_config)
            
            # Принудительная синхронизация времени перед началом работы
            try:
                logger.info("[EXEC] Syncing time with Binance server...")
                await self.exchange.load_time_difference()
                logger.info(f"[EXEC] Time offset: {self.exchange.timeframe_offset}ms")
            except Exception as e:
                logger.warning(f"[EXEC] Time sync failed: {e}, continuing with auto-adjust")
            
            self.binance_api = BinanceFuturesAPI(
                api_key=self.exchange_config.api_key,
                secret_key=self.exchange_config.api_secret
            )
            await self.binance_api.start_session()
            
            if self.exchange_config.sandbox:
                self.exchange.set_sandbox_mode(True)
                logger.warning("Sandbox mode enabled in Execution Engine - using testnet")
            else:
                logger.info("Live trading mode enabled in Execution Engine - REAL MONEY")
            
            self._initialized = True
            logger.info(f"Execution engine initialized on {self.exchange_config.exchange_id}")
    
    async def close(self):
        """Close exchange and native API sessions."""
        if self.exchange:
            await self.exchange.close()
        if self.binance_api:
            await self.binance_api.close_session()
        self._initialized = False
    
    async def get_balance(self) -> Dict:
        """Get account balance."""
        if not self._initialized:
            await self.initialize()
        
        try:
            # Для Futures используем параметр type='future'
            balance = await self.exchange.fetch_balance({'type': 'future'})
            
            # Логирование полной структуры для отладки (только первый раз)
            if not hasattr(self, '_balance_logged'):
                logger.debug(f"Full balance response keys: {balance.keys()}")
                if 'info' in balance and 'assets' in balance['info']:
                    logger.debug(f"Found assets in info: {len(balance['info']['assets'])} items")
                    for asset in balance['info']['assets'][:3]:  # Первые 3 для примера
                        logger.debug(f"Asset sample: {asset}")
                self._balance_logged = True
            
            return balance.get('total', {})
        except Exception as e:
            logger.error(f"Error fetching balance: {e}")
            return {}
    
    async def get_position(self, symbol: str) -> Optional[Dict]:
        """Get current position for a symbol."""
        if not self._initialized:
            await self.initialize()
        
        try:
            positions = await self.exchange.fetch_positions([symbol])
            for pos in positions:
                if pos['symbol'] == symbol and float(pos['contracts']) != 0:
                    return pos
            return None
        except Exception as e:
            logger.error(f"Error fetching position for {symbol}: {e}")
            return None
    
    async def execute_market_order(
        self, 
        symbol: str, 
        side: str, 
        amount: float,
        leverage: int = 1
    ) -> Optional[Dict]:
        """
        Execute a market order.
        
        Args:
            symbol: Trading pair
            side: 'buy' or 'sell'
            amount: Amount to trade
            leverage: Leverage multiplier
        
        Returns:
            Order result or None if failed
        """
        if not self._initialized:
            await self.initialize()
        
        try:
            # Set leverage BEFORE opening position (isolated mode)
            # Binance requires leverage to be set before order in isolated mode
            await self.exchange.set_leverage(leverage, symbol)
            
            # Small delay to ensure leverage is applied
            await asyncio.sleep(0.2)
            
            # Execute order
            order = await self.exchange.create_market_order(symbol, side, amount)
            
            self.order_history.append({
                'symbol': symbol,
                'side': side,
                'amount': amount,
                'leverage': leverage,
                'type': 'market',
                'timestamp': order.get('timestamp'),
                'price': order.get('average'),
                'status': order.get('status')
            })
            
            logger.info(f"Executed {side} order for {symbol}: {amount} @ {order.get('average')}")
            return order
            
        except Exception as e:
            error_msg = str(e)
            # Check for invalid leverage error
            if 'not valid' in error_msg.lower() or '-4028' in error_msg:
                logger.error(f"Leverage {leverage} is not valid for {symbol}. Using max allowed leverage.")
                # Try with lower leverage
                for test_lev in [20, 10, 5, 2]:
                    try:
                        await self.exchange.set_leverage(test_lev, symbol)
                        await asyncio.sleep(0.2)
                        order = await self.exchange.create_market_order(symbol, side, amount)
                        logger.info(f"Executed with fallback leverage {test_lev}: {amount} @ {order.get('average')}")
                        return order
                    except:
                        continue
            logger.error(f"Error executing market order: {e}")
            return None
    
    async def execute_limit_order(
        self, 
        symbol: str, 
        side: str, 
        amount: float,
        price: float
    ) -> Optional[Dict]:
        """Execute a limit order."""
        if not self._initialized:
            await self.initialize()
        
        try:
            order = await self.exchange.create_limit_order(symbol, side, amount, price)
            
            self.order_history.append({
                'symbol': symbol,
                'side': side,
                'amount': amount,
                'price': price,
                'type': 'limit',
                'timestamp': order.get('timestamp'),
                'status': order.get('status')
            })
            
            logger.info(f"Placed {side} limit order for {symbol}: {amount} @ {price}")
            return order
            
        except Exception as e:
            logger.error(f"Error executing limit order: {e}")
            return None
    
    async def set_stop_loss(
        self, 
        symbol: str, 
        side: str, 
        stop_price: float,
        position_size: float = None
    ) -> Optional[Dict]:
        """
        Set stop loss on exchange server via native Binance API.
        
        Args:
            symbol: Trading pair (e.g., 'BLESS/USDT:USDT')
            side: 'buy' or 'sell' (opposite of position direction)
            stop_price: Trigger price for stop loss
            position_size: Amount to close (optional, will be fetched if not provided)
        
        Returns:
            Order dict if successful, None if failed
        """
        if not self._initialized:
            await self.initialize()
        
        try:
            stop_price_formatted = float(self.exchange.price_to_precision(symbol, stop_price))
            
            if stop_price_formatted <= 0 or stop_price_formatted != stop_price_formatted:
                logger.error(f"[SL ERROR] Invalid stop price: {stop_price} -> {stop_price_formatted}")
                return None
            
            await self.cancel_sl_tp_orders(symbol, order_type='sl')

            order = await self.binance_api.place_stop_loss(
                symbol=symbol,
                side=side,
                stop_price=stop_price_formatted,
                position_size=position_size
            )
            
            self.sl_orders[symbol] = {
                'id': order.get('orderId'),
                'algo_id': order.get('algoId'),
                'type': 'STOP_LOSS',
                'side': side,
                'price': stop_price_formatted
            }
            
            logger.info(f"[SL SET] {symbol} {side.upper()} @ {stop_price_formatted} | exchange-side")
            
            return order
            
        except Exception as e:
            error_msg = str(e)
            logger.error(f"[SL ERROR] Failed to set stop loss on exchange: {error_msg}")
            return None
    
    async def set_take_profit(
        self, 
        symbol: str, 
        side: str, 
        take_profit_price: float,
        position_size: float = None
    ) -> Optional[Dict]:
        """
        Set take profit on exchange server via native Binance API.
        
        Args:
            symbol: Trading pair (e.g., 'BLESS/USDT:USDT')
            side: 'buy' or 'sell' (opposite of position direction)
            take_profit_price: Trigger price for take profit
            position_size: Amount to close (optional, will be fetched if not provided)
        
        Returns:
            Order dict if successful, None if failed
        """
        if not self._initialized:
            await self.initialize()
        
        try:
            tp_price_formatted = float(self.exchange.price_to_precision(symbol, take_profit_price))
            
            if tp_price_formatted <= 0 or tp_price_formatted != tp_price_formatted:
                logger.error(f"[TP ERROR] Invalid take profit price: {take_profit_price} -> {tp_price_formatted}")
                return None
            
            await self.cancel_sl_tp_orders(symbol, order_type='tp')

            order = await self.binance_api.place_take_profit(
                symbol=symbol,
                side=side,
                tp_price=tp_price_formatted,
                position_size=position_size
            )
            
            self.tp_orders[symbol] = {
                'id': order.get('orderId'),
                'algo_id': order.get('algoId'),
                'type': 'TAKE_PROFIT',
                'side': side,
                'price': tp_price_formatted
            }
            
            logger.info(f"[TP SET] {symbol} {side.upper()} @ {tp_price_formatted} | exchange-side")
            
            return order
            
        except Exception as e:
            error_msg = str(e)
            logger.error(f"[TP ERROR] Failed to set take profit on exchange: {error_msg}")
            return None

    async def cancel_sl_tp_orders(self, symbol: str, order_type: str = 'all'):
        """Cancel tracked SL/TP orders for a symbol."""
        if not self._initialized:
            await self.initialize()

        try:
            if order_type in ['sl', 'all'] and symbol in self.sl_orders:
                sl_id = self.sl_orders[symbol].get('id')
                sl_algo_id = self.sl_orders[symbol].get('algo_id')
                if sl_algo_id and self.binance_api:
                    await self.binance_api.cancel_algo_order(symbol, sl_algo_id)
                elif sl_id:
                    try:
                        if self.binance_api:
                            await self.binance_api.cancel_order(symbol, sl_id)
                        else:
                            await self.exchange.cancel_order(sl_id, symbol)
                    except Exception as e:
                        logger.debug(f"[CANCEL SL] {symbol}: {e}")
                self.sl_orders.pop(symbol, None)

            if order_type in ['tp', 'all'] and symbol in self.tp_orders:
                tp_id = self.tp_orders[symbol].get('id')
                tp_algo_id = self.tp_orders[symbol].get('algo_id')
                if tp_algo_id and self.binance_api:
                    await self.binance_api.cancel_algo_order(symbol, tp_algo_id)
                elif tp_id:
                    try:
                        if self.binance_api:
                            await self.binance_api.cancel_order(symbol, tp_id)
                        else:
                            await self.exchange.cancel_order(tp_id, symbol)
                    except Exception as e:
                        logger.debug(f"[CANCEL TP] {symbol}: {e}")
                self.tp_orders.pop(symbol, None)
        except Exception as e:
            logger.error(f"[CANCEL SL/TP ERROR] {symbol}: {e}")

    async def update_stop_loss(self, symbol: str, side: str, new_stop_price: float) -> bool:
        """Replace current SL with a new exchange-side SL order."""
        order = await self.set_stop_loss(symbol, side, new_stop_price)
        return order is not None

    async def update_take_profit(self, symbol: str, side: str, new_tp_price: float) -> bool:
        """Replace current TP with a new exchange-side TP order."""
        order = await self.set_take_profit(symbol, side, new_tp_price)
        return order is not None

    async def get_price_precision(self, symbol: str) -> int:
        """Get price precision for a symbol."""
        if not self._initialized:
            await self.initialize()
        try:
            market = self.exchange.markets.get(symbol)
            if market:
                precision = market.get('precision', {}).get('price', 2)
                # Ensure it's an integer
                return int(precision) if precision is not None else 2
            return 2
        except Exception as e:
            logger.error(f"Error getting price precision: {e}")
            return 2

    async def get_tick_size(self, symbol: str) -> float:
        """Get minimum tick size for a symbol."""
        if not self._initialized:
            await self.initialize()
        try:
            market = self.exchange.markets.get(symbol)
            if market:
                limits = market.get('limits', {})
                price_limits = limits.get('price', {})
                min_price = price_limits.get('min', 0.01)
                # Эвристика: тик обычно равен минимальной цене или близок к ней
                return float(min_price) if min_price > 0 else 0.01
            return 0.01
        except Exception as e:
            logger.error(f"Error getting tick size: {e}")
            return 0.01
    
    async def cancel_all_orders(self, symbol: str):
        """Cancel all open orders for a symbol using CCXT and native endpoints."""
        if not self._initialized:
            await self.initialize()
        
        try:
            # Cancel via CCXT
            await self.exchange.cancel_all_orders(symbol)
            logger.info(f"[CCXT] Cancelled all orders for {symbol}")

            if self.binance_api:
                regular_ok = await self.binance_api.cancel_all_open_orders(symbol)
                logger.info(f"[NATIVE] cancel_all_open_orders={regular_ok} for {symbol}")
            
            # Clear local tracking
            self.open_orders.pop(symbol, None)
            self.sl_orders.pop(symbol, None)
            self.tp_orders.pop(symbol, None)
            
            logger.info(f"Cancelled all orders for {symbol}")
        except Exception as e:
            logger.error(f"Error cancelling orders for {symbol}: {e}")
            return None
    async def close_position(self, symbol: str) -> float:
        """
        Close existing position at market price.
        CRITICAL FIX: Correctly determine close side to REDUCE position, not increase it.
        
        Returns:
            Close price or 0 if failed
        """
        if not self._initialized:
            await self.initialize()
        
        try:
            # Fetch fresh position data
            positions = await self.exchange.fetch_positions([symbol])
            position = None
            for pos in positions:
                if pos['symbol'] == symbol:
                    contracts = float(pos.get('contracts', 0))
                    if contracts != 0:
                        position = pos
                        break
            
            if not position:
                logger.info(f"No open position to close for {symbol}")
                return 0.0
            
            contracts = abs(float(position.get('contracts', 0)))
            side = str(position.get('side', '')).lower()
            raw_amt = float(position.get('info', {}).get('positionAmt', 0) or 0)

            # Determine true direction with priority: raw signed amount -> normalized side -> contracts sign fallback
            if raw_amt < 0:
                position_direction = 'short'
            elif raw_amt > 0:
                position_direction = 'long'
            elif side in ['short', 'long']:
                position_direction = side
            else:
                # Last fallback if exchange doesn't provide both fields reliably
                position_direction = 'long' if float(position.get('contracts', 0)) > 0 else 'short'

            if position_direction == 'long':
                close_side = 'sell'
                logger.info(f"Closing LONG position: {contracts} contracts -> SELL {contracts}")
            else:
                close_side = 'buy'
                logger.info(f"Closing SHORT position: {contracts} contracts -> BUY {contracts}")

            amount = contracts
            
            if amount <= 0:
                logger.warning(f"Invalid position amount: {amount}")
                return 0.0
            
            # Cancel existing SL/TP orders first to avoid conflicts
            await self.cancel_all_orders(symbol)
            await asyncio.sleep(0.1)
            
            # Execute market order with reduceOnly if supported
            # For CCXT on Binance Futures, create_market_order with params
            try:
                order = await self.exchange.create_market_order(
                    symbol, 
                    close_side, 
                    amount,
                    params={'reduceOnly': True}  # Ensure we only reduce, never flip
                )
            except Exception as e:
                # Fallback without reduceOnly if exchange doesn't support it in this context
                logger.warning(f"reduceOnly failed, trying standard close: {e}")
                order = await self.exchange.create_market_order(symbol, close_side, amount)
            
            if order:
                close_price = order.get('average', order.get('price', 0.0))
                logger.info(f"[CLOSE OK] Closed {side.upper()} position on {symbol}: {amount} @ {close_price} ({close_side.upper()})")
                return close_price
            
            logger.error(f"Order returned None for {symbol}")
            return 0.0
            
        except Exception as e:
            logger.error(f"CRITICAL ERROR closing position: {e}", exc_info=True)
            return 0.0
    
    async def execute_signal(
        self, 
        signal: dict,
        position_size: float,
        leverage: int
    ) -> bool:
        """
        Execute a trading signal.
        
        Args:
            signal: Trading signal dict with keys: direction, entry_price, symbol, stop_loss, take_profit
            position_size: Size of position
            leverage: Leverage to use
        
        Returns:
            True if successful
        """
        try:
            direction = signal.get('direction')
            entry_price = signal.get('entry_price')
            symbol = signal.get('symbol')
            stop_loss = signal.get('stop_loss')
            take_profit = signal.get('take_profit')
            
            side = 'buy' if direction == 'long' else 'sell'
            
            # Enter position
            order = await self.execute_market_order(
                symbol, 
                side, 
                position_size,
                leverage
            )
            
            if not order:
                return False
            
            actual_entry = order.get('average', entry_price)
            logger.info(
                f"[EXEC] Position opened without exchange SL/TP orders for {symbol}. "
                f"Exit is managed by bot strategy (market close on SL/TP conditions)."
            )
            
            return True
            
        except Exception as e:
            logger.error(f"Error executing signal: {e}")
            return False
    
    async def get_exchange_positions(self) -> List[Dict]:
        """
        Получить все открытые позиции с биржи.
        
        Returns:
            Список словарей с данными о позициях
        """
        try:
            # Получаем все позиции (Binance возвращает все позиции, включая пустые)
            all_positions = await self.exchange.fetch_positions()
            
            # Фильтруем только открытые позиции (с ненулевым размером)
            open_positions = []
            for pos in all_positions:
                contracts = float(pos.get('contracts', 0) or pos.get('size', 0) or 0)
                if abs(contracts) > 0.001:  # Только если есть открытый размер
                    open_positions.append(pos)
            
            return open_positions
            
        except Exception as e:
            logger.error(f"[EXEC] Error fetching exchange positions: {e}", exc_info=True)
            return []
