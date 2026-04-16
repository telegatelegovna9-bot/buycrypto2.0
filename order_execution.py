"""
Order Execution Module.
Handles all order placement and cancellation with proper reduceOnly logic.
Interfaces with Binance Futures via CCXT.

CRITICAL RULES:
- STOP_LOSS uses STOP with closePosition=True, NO amount, NO reduceOnly
- TAKE_PROFIT uses TAKE_PROFIT with closePosition=True, NO amount, NO reduceOnly
- ONLY manual close_position uses reduceOnly=True
- NO Algo Order API calls (fapiPrivatePostAlgoOrders is forbidden)
"""
import ccxt.async_support as ccxt
import asyncio
import math
from typing import Dict, Optional, List
import logging

logger = logging.getLogger(__name__)


class OrderExecutor:
    """
    Handles all exchange interactions for order execution.
    Ensures proper use of reduceOnly to prevent position flipping.
    """
    
    def __init__(self, config):
        self.config = config
        self.exchange_config = config.exchange
        
        self.exchange = None
        self._initialized = False
        
        # Order tracking
        self.open_orders: Dict[str, List[Dict]] = {}
        self.order_history: List[Dict] = []
        
        # SL/TP order tracking per symbol
        self.sl_orders: Dict[str, Dict] = {}
        self.tp_orders: Dict[str, Dict] = {}
    
    async def initialize(self):
        """Initialize exchange connection."""
        if not self._initialized:
            base_config = {
                'apiKey': self.exchange_config.api_key,
                'secret': self.exchange_config.api_secret,
                'enableRateLimit': True,
                'options': {
                    'defaultType': 'future',
                    'adjustForTimeDifference': True,
                },
                'timeout': 30000,
                'retries': 5,
            }
            
            if self.exchange_config.proxy_url:
                base_config['proxy'] = self.exchange_config.proxy_url
            
            self.exchange = getattr(ccxt, self.exchange_config.exchange_id)(base_config)
            
            # CRITICAL: Load markets to ensure futures endpoints are available
            await self.exchange.load_markets()
            
            # Debug: Verify we're connected to futures
            logger.info(f"Exchange URLs: {self.exchange.urls}")
            logger.info(f"Default type option: {self.exchange.options.get('defaultType')}")
            
            if self.exchange_config.sandbox:
                self.exchange.set_sandbox_mode(True)
                logger.warning("Sandbox mode enabled - using testnet")
            else:
                logger.info("Live trading mode enabled - REAL MONEY")
            
            self._initialized = True
            logger.info(f"Order executor initialized on {self.exchange_config.exchange_id}")
    
    async def close(self):
        """Close exchange connection."""
        if self.exchange:
            await self.exchange.close()
            self._initialized = False
    
    async def get_position(self, symbol: str) -> Optional[Dict]:
        """Get current position for a symbol."""
        if not self._initialized:
            await self.initialize()
        
        try:
            positions = await self.exchange.fetch_positions([symbol])
            for pos in positions:
                if pos['symbol'] == symbol:
                    contracts = float(pos.get('contracts', 0))
                    if contracts != 0:
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
        leverage: int = 1,
        reduce_only: bool = False
    ) -> Optional[Dict]:
        """
        Execute a market order.
        
        Args:
            symbol: Trading pair
            side: 'buy' or 'sell'
            amount: Amount to trade
            leverage: Leverage multiplier
            reduce_only: If True, only reduce existing position
        
        Returns:
            Order result or None if failed
        """
        if not self._initialized:
            await self.initialize()
        
        try:
            # Set leverage BEFORE opening position (only if not reduce_only)
            if not reduce_only and leverage > 1:
                await self.exchange.set_leverage(leverage, symbol)
                await asyncio.sleep(0.2)
            
            # Execute order with reduceOnly parameter and positionSide
            params = {'reduceOnly': reduce_only, 'positionSide': 'BOTH'} if reduce_only else {'positionSide': 'BOTH'}
            order = await self.exchange.create_market_order(symbol, side, amount, params=params)
            
            # Debug: Log the actual endpoint used
            logger.debug(f"Order placed via: {self.exchange.urls.get('api', {}).get('futures', 'unknown')}")
            
            self.order_history.append({
                'symbol': symbol,
                'side': side,
                'amount': amount,
                'leverage': leverage,
                'type': 'market',
                'reduce_only': reduce_only,
                'timestamp': order.get('timestamp'),
                'price': order.get('average'),
                'status': order.get('status')
            })
            
            logger.info(
                f"[EXEC] {side.upper()} {symbol}: {amount} @ {order.get('average')} "
                f"(reduce_only={reduce_only}, leverage={leverage}x)"
            )
            return order
            
        except Exception as e:
            error_msg = str(e)
            if 'not valid' in error_msg.lower() or '-4028' in error_msg:
                logger.error(f"Leverage {leverage} not valid for {symbol}. Trying fallback...")
                for test_lev in [20, 10, 5, 2]:
                    try:
                        await self.exchange.set_leverage(test_lev, symbol)
                        await asyncio.sleep(0.2)
                        order = await self.exchange.create_market_order(symbol, side, amount)
                        logger.info(f"[EXEC] Fallback with leverage {test_lev}: {amount} @ {order.get('average')}")
                        return order
                    except:
                        continue
            
            logger.error(f"[ERROR] Market order failed: {e}")
            return None
    
    async def set_stop_loss(
        self, 
        symbol: str, 
        side: str, 
        stop_price: float
    ) -> Optional[Dict]:
        """
        Set stop loss order on Binance Futures.
        
        CRITICAL: Uses STOP_MARKET with closePosition=True.
        - NO amount parameter (uses closePosition=True instead)
        - NO reduceOnly parameter (not needed with closePosition)
        
        Args:
            symbol: Trading pair
            side: 'buy' or 'sell' (opposite of position direction)
            stop_price: Trigger price for stop loss
        
        Returns:
            Order dict if successful, None if failed
        """
        if not self._initialized:
            await self.initialize()
        
        try:
            # CRITICAL: Use price_to_precision instead of round()
            stop_price_formatted = float(self.exchange.price_to_precision(symbol, stop_price))
            
            # Validate price
            if stop_price_formatted <= 0 or stop_price_formatted != stop_price_formatted:  # NaN check
                logger.error(f"[SL ERROR] Invalid stop price: {stop_price} -> {stop_price_formatted}")
                await self.close_position(symbol)
                return None
            
            logger.info(f"[SL] {symbol}: Original={stop_price}, Formatted={stop_price_formatted}")
            
            # Cancel existing SL order for this symbol
            await self.cancel_sl_tp_orders(symbol, 'sl')
            
            # CRITICAL: STOP_MARKET with closePosition=True, NO amount, NO reduceOnly
            # For Binance Futures, we need to use specific params structure
            logger.info(f"[SL DEBUG] Symbol: {symbol}, Type: STOP_MARKET, Side: {side}, StopPrice: {stop_price_formatted}")
            
            order = await self.exchange.create_order(
                symbol=symbol,
                type='STOP_MARKET',
                side=side,
                amount=None,  # Must be None when using closePosition
                params={
                    'stopPrice': stop_price_formatted,
                    'closePosition': True,  # Close entire position
                    'workingType': 'MARK_PRICE',  # Use mark price to avoid wick liquidations
                    'newOrderRespType': 'RESULT',
                    'positionSide': 'BOTH',  # Required for Binance Futures one-way mode
                    'timeInForce': 'GTC'  # Good Till Cancel - required for stop orders
                }
            )
            
            logger.info(f"[SL DEBUG] Order created: ID={order.get('id')}, Status={order.get('status')}")
            
            # Track the order
            self.sl_orders[symbol] = {
                'order_id': order.get('id'),
                'side': side,
                'stop_price': stop_price_formatted,
                'type': 'STOP_MARKET'
            }
            
            logger.info(
                f"[SL SET] {symbol} {side.upper()} @ {stop_price_formatted} | Type: STOP_MARKET"
            )
            
            # VERIFY order was created
            await asyncio.sleep(0.3)
            open_orders = await self.exchange.fetch_open_orders(symbol)
            order_exists = any(o.get('id') == order.get('id') for o in open_orders)
            
            if not order_exists:
                logger.error(f"[SL VERIFY] Order not found in open orders! Closing position immediately.")
                await self.close_position(symbol)
                return None
            
            return order
            
        except Exception as e:
            error_msg = str(e)
            logger.error(f"[SL ERROR] Failed to set stop loss: {error_msg}")
            
            # EMERGENCY: If SL cannot be set, close the position immediately
            logger.error(f"[SL EMERGENCY] Cannot set SL, closing position immediately!")
            await self.close_position(symbol)
            return None
    
    def amount_to_precision(self, symbol: str, amount: float) -> float:
        """Round amount to exchange precision."""
        if not self._initialized:
            return amount
        try:
            market = self.exchange.market(symbol)
            return float(self.exchange.amount_to_precision(symbol, amount))
        except:
            return amount
    
    def price_to_precision(self, symbol: str, price: float) -> float:
        """Round price to exchange precision."""
        if not self._initialized:
            return price
        try:
            return float(self.exchange.price_to_precision(symbol, price))
        except:
            return price
    
    async def set_take_profit(
        self, 
        symbol: str, 
        side: str, 
        take_profit_price: float
    ) -> Optional[Dict]:
        """
        Set take profit order on Binance Futures.
        
        CRITICAL: Uses TAKE_PROFIT_MARKET with closePosition=True.
        - NO amount parameter (uses closePosition=True instead)
        - NO reduceOnly parameter (not needed with closePosition)
        
        Args:
            symbol: Trading pair
            side: 'buy' or 'sell' (opposite of position direction)
            take_profit_price: Trigger price for take profit
        
        Returns:
            Order dict if successful, None if failed
        """
        if not self._initialized:
            await self.initialize()
        
        try:
            # CRITICAL: Use price_to_precision instead of round()
            tp_price_formatted = float(self.exchange.price_to_precision(symbol, take_profit_price))
            
            # Validate price
            if tp_price_formatted <= 0 or tp_price_formatted != tp_price_formatted:  # NaN check
                logger.error(f"[TP ERROR] Invalid take profit price: {take_profit_price} -> {tp_price_formatted}")
                await self.close_position(symbol)
                return None
            
            logger.info(f"[TP] {symbol}: Original={take_profit_price}, Formatted={tp_price_formatted}")
            
            # Cancel existing TP order for this symbol
            await self.cancel_sl_tp_orders(symbol, 'tp')
            
            # CRITICAL: TAKE_PROFIT_MARKET with closePosition=True, NO amount, NO reduceOnly
            # For Binance Futures, we need to use specific params structure
            logger.info(f"[TP DEBUG] Symbol: {symbol}, Type: TAKE_PROFIT_MARKET, Side: {side}, StopPrice: {tp_price_formatted}")
            
            order = await self.exchange.create_order(
                symbol=symbol,
                type='TAKE_PROFIT_MARKET',
                side=side,
                amount=None,  # Must be None when using closePosition
                params={
                    'stopPrice': tp_price_formatted,
                    'closePosition': True,  # Close entire position
                    'workingType': 'MARK_PRICE',
                    'newOrderRespType': 'RESULT',
                    'positionSide': 'BOTH',  # Required for Binance Futures one-way mode
                    'timeInForce': 'GTC'  # Good Till Cancel - required for stop orders
                }
            )
            
            logger.info(f"[TP DEBUG] Order created: ID={order.get('id')}, Status={order.get('status')}")
            
            # Track the order
            self.tp_orders[symbol] = {
                'order_id': order.get('id'),
                'side': side,
                'take_profit_price': tp_price_formatted,
                'type': 'TAKE_PROFIT_MARKET'
            }
            
            logger.info(
                f"[TP SET] {symbol} {side.upper()} @ {tp_price_formatted} | Type: TAKE_PROFIT_MARKET"
            )
            
            # VERIFY order was created
            await asyncio.sleep(0.3)
            open_orders = await self.exchange.fetch_open_orders(symbol)
            order_exists = any(o.get('id') == order.get('id') for o in open_orders)
            
            if not order_exists:
                logger.error(f"[TP VERIFY] Order not found in open orders! Closing position immediately.")
                await self.close_position(symbol)
                return None
            
            return order
            
        except Exception as e:
            error_msg = str(e)
            logger.error(f"[TP ERROR] Failed to set take profit: {error_msg}")
            
            # EMERGENCY: If TP cannot be set, close the position immediately
            logger.error(f"[TP EMERGENCY] Cannot set TP, closing position immediately!")
            await self.close_position(symbol)
            return None
    
    async def cancel_sl_tp_orders(self, symbol: str, order_type: str = 'all'):
        """
        Cancel SL/TP orders for a symbol.
        
        Args:
            symbol: Trading pair
            order_type: 'sl', 'tp', or 'all'
        """
        if not self._initialized:
            await self.initialize()
        
        try:
            if order_type in ['sl', 'all']:
                if symbol in self.sl_orders:
                    order_id = self.sl_orders[symbol].get('order_id')
                    if order_id:
                        try:
                            await self.exchange.cancel_order(order_id, symbol)
                            logger.info(f"[CANCEL] SL order cancelled for {symbol}")
                        except Exception as e:
                            logger.debug(f"Cancel SL order failed (may be already filled): {e}")
                    del self.sl_orders[symbol]
            
            if order_type in ['tp', 'all']:
                if symbol in self.tp_orders:
                    order_id = self.tp_orders[symbol].get('order_id')
                    if order_id:
                        try:
                            await self.exchange.cancel_order(order_id, symbol)
                            logger.info(f"[CANCEL] TP order cancelled for {symbol}")
                        except Exception as e:
                            logger.debug(f"Cancel TP order failed (may be already filled): {e}")
                    del self.tp_orders[symbol]
                    
        except Exception as e:
            logger.error(f"[CANCEL ERROR] Failed to cancel orders: {e}")
    
    async def cancel_all_orders(self, symbol: str):
        """Cancel all open orders for a symbol including SL/TP."""
        if not self._initialized:
            await self.initialize()
        
        try:
            # Cancel all orders via exchange API
            await self.exchange.cancel_all_orders(symbol)
            
            # Clear internal tracking
            self.sl_orders.pop(symbol, None)
            self.tp_orders.pop(symbol, None)
            self.open_orders.pop(symbol, None)
            
            logger.info(f"[CANCEL] All orders cancelled for {symbol}")
        except Exception as e:
            logger.error(f"[CANCEL ERROR] Failed to cancel all orders: {e}")
    
    async def close_position(self, symbol: str) -> float:
        """
        Close existing position at market price.
        
        CRITICAL: Correctly determine close side to REDUCE position, not increase it.
        Uses reduceOnly=True to prevent position flipping.
        
        Returns:
            Close price or 0 if failed
        """
        if not self._initialized:
            await self.initialize()
        
        try:
            # Fetch fresh position data DIRECTLY from exchange
            positions = await self.exchange.fetch_positions([symbol])
            position = None
            for pos in positions:
                if pos['symbol'] == symbol:
                    contracts = float(pos.get('contracts', 0))
                    if contracts != 0:
                        position = pos
                        break
            
            if not position:
                logger.info(f"[INFO] No open position to close for {symbol}")
                return 0.0
            
            contracts = float(position['contracts'])
            side = position.get('side', 'long')
            
            # CRITICAL: To CLOSE a position, we do the OPPOSITE action
            # LONG (contracts > 0) -> SELL to close
            # SHORT (contracts < 0) -> BUY to close
            if contracts > 0 or side == 'long':
                close_side = 'sell'  # Close LONG by selling
                amount = abs(contracts)
            else:
                close_side = 'buy'  # Close SHORT by buying
                amount = abs(contracts)
            
            if amount <= 0:
                logger.warning(f"[WARN] Invalid position amount: {amount}")
                return 0.0
            
            # Cancel existing SL/TP orders first to avoid conflicts
            await self.cancel_sl_tp_orders(symbol, 'all')
            await asyncio.sleep(0.2)  # Give time for cancellation
            
            # Execute market order with reduceOnly=True
            order = None
            try:
                order = await self.execute_market_order(
                    symbol,
                    close_side,
                    amount,
                    reduce_only=True  # CRITICAL: Never flip position
                )
            except Exception as e:
                logger.warning(f"reduceOnly failed, trying standard close: {e}")
                try:
                    # Fallback without reduceOnly but with positionSide
                    order = await self.exchange.create_market_order(
                        symbol, 
                        close_side, 
                        amount,
                        params={'positionSide': 'BOTH'}
                    )
                except Exception as fallback_error:
                    logger.error(f"Fallback close also failed: {fallback_error}")
                    return 0.0
            
            if order:
                close_price = order.get('average', order.get('price', 0.0))
                if not close_price or close_price <= 0:
                    # Try to get current market price as fallback
                    ticker = await self.exchange.fetch_ticker(symbol)
                    close_price = ticker.get('last', 0.0)
                
                # Use ASCII-safe logging (no unicode symbols like ✓)
                logger.info(
                    f"[CLOSE OK] Closed {side.upper()} position on {symbol}: "
                    f"{amount} @ {close_price} ({close_side.upper()})"
                )
                
                # VERIFY position was actually closed
                await asyncio.sleep(0.5)
                verification = await self.get_position(symbol)
                if verification:
                    remaining_contracts = float(verification.get('contracts', 0))
                    if remaining_contracts != 0:
                        logger.error(f"[WARN] Position not fully closed! Remaining: {remaining_contracts}")
                        # Try to close remaining amount
                        return await self.close_position(symbol)
                
                return close_price
            
            logger.error(f"[CLOSE FAIL] Order returned None for {symbol}")
            return 0.0
            
        except Exception as e:
            logger.error(f"[CLOSE CRITICAL] Error closing position: {e}", exc_info=True)
            return 0.0
    
    async def update_stop_loss(
        self,
        symbol: str,
        side: str,
        new_stop_price: float
    ) -> bool:
        """
        Update existing stop loss order (for trailing stop).
        
        Args:
            symbol: Trading pair
            side: 'buy' or 'sell'
            new_stop_price: New stop price
        
        Returns:
            True if successful
        """
        # Cancel old SL and set new one
        await self.cancel_sl_tp_orders(symbol, 'sl')
        result = await self.set_stop_loss(symbol, side, new_stop_price)
        return result is not None
    
    async def update_take_profit(
        self,
        symbol: str,
        side: str,
        new_tp_price: float
    ) -> bool:
        """
        Update existing take profit order.
        
        Returns:
            True if successful
        """
        await self.cancel_sl_tp_orders(symbol, 'tp')
        result = await self.set_take_profit(symbol, side, new_tp_price)
        return result is not None
    
    async def partial_close(
        self,
        symbol: str,
        percentage: float
    ) -> float:
        """
        Close partial position (e.g., take 50% profits).
        
        Args:
            symbol: Trading pair
            percentage: Percentage to close (0-1)
        
        Returns:
            Close price or 0 if failed
        """
        if not self._initialized:
            await self.initialize()
        
        try:
            position = await self.get_position(symbol)
            
            if not position:
                logger.info(f"[INFO] No position to partially close for {symbol}")
                return 0.0
            
            contracts = float(position['contracts'])
            side = position.get('side', 'long')
            
            if contracts > 0 or side == 'long':
                close_side = 'sell'
                amount = abs(contracts) * percentage
            else:
                close_side = 'buy'
                amount = abs(contracts) * percentage
            
            if amount <= 0:
                logger.warning(f"[WARN] Invalid partial close amount: {amount}")
                return 0.0
            
            # Execute with reduceOnly
            order = await self.execute_market_order(
                symbol,
                close_side,
                amount,
                reduce_only=True
            )
            
            if order:
                close_price = order.get('average', order.get('price', 0.0))
                logger.info(
                    f"[PARTIAL CLOSE] {percentage*100:.0f}% of {symbol}: "
                    f"{amount} @ {close_price}"
                )
                return close_price
            
            return 0.0
            
        except Exception as e:
            logger.error(f"[PARTIAL CLOSE ERROR] {e}")
            return 0.0
    
    async def get_price_precision(self, symbol: str) -> int:
        """
        Get price precision for a symbol based on tickSize.
        Returns the number of decimal places needed for the minimum price movement.
        """
        if not self._initialized:
            await self.initialize()
        try:
            # Force fetch markets if not loaded
            if not self.exchange.markets or symbol not in self.exchange.markets:
                await self.exchange.load_markets()
            
            market = self.exchange.markets.get(symbol)
            if not market:
                logger.warning(f"Market {symbol} not found, using default precision 6")
                return 6
            
            # Priority 1: Use limits if available
            limits = market.get('limits', {})
            price_limits = limits.get('price', {})
            min_price = price_limits.get('min')
            
            if min_price is not None and min_price > 0:
                # Calculate precision from min_price (e.g., 0.000001 -> 6 decimals)
                precision = abs(int(math.floor(math.log10(min_price))))
                # Cap at reasonable value (Binance max is usually 8)
                return min(precision, 8)
            
            # Priority 2: Use precision dict
            precision_data = market.get('precision', {})
            price_precision = precision_data.get('price')
            
            if price_precision is not None:
                if isinstance(price_precision, int):
                    return min(price_precision, 8)
                elif isinstance(price_precision, float):
                    # If it's like 0.000001, convert to decimal count
                    return min(abs(int(math.floor(math.log10(price_precision)))), 8)
            
            # Priority 3: Use tickSize
            info = market.get('info', {})
            tick_size = info.get('tickSize')
            
            if tick_size:
                # Parse tickSize string like "0.000001" to determine decimals
                tick_float = float(tick_size)
                if tick_float > 0:
                    precision = abs(int(math.floor(math.log10(tick_float))))
                    return min(precision, 8)
            
            # Fallback: safe default for most altcoins
            logger.info(f"Using default precision 6 for {symbol}")
            return 6
            
        except Exception as e:
            logger.error(f"Error getting price precision for {symbol}: {e}")
            return 6  # Safe default
    
    async def get_balance(self) -> Dict:
        """Get account balance."""
        if not self._initialized:
            await self.initialize()
        
        try:
            balance = await self.exchange.fetch_balance({'type': 'future'})
            return balance.get('total', {})
        except Exception as e:
            logger.error(f"Error fetching balance: {e}")
            return {}
