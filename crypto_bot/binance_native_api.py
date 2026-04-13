"""
Native Binance Futures API Client for Risk Management.
Uses direct HTTP requests to fapi.binance.com for Algo Orders (SL/TP).
Reference: https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api/New-Algo-Order
"""
import hmac
import hashlib
import time
import aiohttp
from urllib.parse import urlencode
from typing import Optional, Dict, Any
import logging

logger = logging.getLogger(__name__)


class BinanceFuturesAPI:
    """Direct client for Binance Futures API (fapi) using Algo Orders for SL/TP"""
    
    def __init__(self, api_key: str, secret_key: str):
        self.api_key = api_key
        self.secret_key = secret_key
        self.base_url = "https://fapi.binance.com"
        self.session: Optional[aiohttp.ClientSession] = None
        
    async def start_session(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
            
    async def close_session(self):
        if self.session and not self.session.closed:
            await self.session.close()
            
    def _generate_signature(self, query_string: str) -> str:
        return hmac.new(
            self.secret_key.encode('utf-8'),
            query_string.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        
    def _get_timestamp(self) -> int:
        return int(time.time() * 1000)
        
    async def _request(self, method: str, path: str, params: Dict[str, Any] = None, signed: bool = False) -> Dict:
        if self.session is None:
            await self.start_session()
            
        url = f"{self.base_url}{path}"
        
        if params is None:
            params = {}
            
        if signed:
            params['timestamp'] = self._get_timestamp()
            query_string = urlencode(params)
            params['signature'] = self._generate_signature(query_string)
            
        headers = {
            'X-MBX-APIKEY': self.api_key,
            'Content-Type': 'application/x-www-form-urlencoded'
        }
        
        try:
            async with self.session.request(method, url, params=params if method == 'GET' else None, data=params if method == 'POST' else None, headers=headers) as response:
                data = await response.json()
                
                if response.status != 200:
                    logger.error(f"Binance API Error {response.status}: {data}")
                    raise Exception(f"Binance API Error: {data}")
                    
                return data
        except Exception as e:
            logger.error(f"Request failed: {e}")
            raise

    async def _normalize_symbol(self, symbol: str) -> str:
        """
        Normalize symbol for Binance Futures API.
        Converts 'BLESS/USDT:USDT' or 'BLESS/USDT' to 'BLESSUSDT'
        """
        normalized = symbol.replace('/', '')
        if ':USDT' in normalized:
            normalized = normalized.replace(':USDT', '')
        return normalized

    async def _get_position_size(self, symbol: str) -> tuple:
        """
        Get current position size and side for a symbol.
        Returns (abs_amount, side) where side is 'BUY' or 'SELL'
        """
        binance_symbol = await self._normalize_symbol(symbol)
        params = {'symbol': binance_symbol}
        
        try:
            positions = await self._request('GET', '/fapi/v2/positionRisk', params=params, signed=True)
            for pos in positions:
                if pos['symbol'] == binance_symbol:
                    amount = float(pos['positionAmt'])
                    if amount > 0:
                        return abs(amount), 'BUY'  # Long position
                    elif amount < 0:
                        return abs(amount), 'SELL'  # Short position
            return 0, None
        except Exception as e:
            logger.error(f"Failed to get position size for {symbol}: {e}")
            return 0, None

    async def place_stop_loss(self, symbol: str, side: str, stop_price: float, position_size: float = None) -> Dict:
        """
        Place a STOP_LOSS algo order using /fapi/v1/algo/order endpoint.
        Uses SUB_ORDER_TYPE = STOP_MARKET with quantity.
        
        Reference: https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api/New-Algo-Order
        """
        binance_symbol = await self._normalize_symbol(symbol)
        
        # Get position size if not provided
        if position_size is None or position_size == 0:
            position_size, pos_side = await self._get_position_size(symbol)
            if position_size == 0:
                raise Exception(f"No open position found for {symbol}")
            logger.info(f"[SL] Detected position size: {position_size} for {symbol}")
        
        # Format quantity according to exchange precision (use stepSize from LOT_SIZE filter)
        # Most perpetual futures use integer quantities, but some like BTC allow decimals
        # We'll format with up to 3 decimal places and strip trailing zeros
        qty_str = f"{position_size:.3f}".rstrip('0').rstrip('.')
        
        # For STOP_LOSS algo order:
        # - algoType: STOP_LOSS
        # - algoSubType: STOP_MARKET (for market order when triggered)
        # - side: opposite to position side (SELL for long, BUY for short)
        # - quantity: the amount to close
        # - stopPrice: trigger price
        # - workingType: MARK_PRICE or CONTRACT_PRICE
        
        params = {
            'symbol': binance_symbol,
            'algoType': 'STOP_LOSS',
            'algoSubType': 'STOP_MARKET',
            'side': side.upper(),  # SELL for long position, BUY for short
            'quantity': qty_str,
            'stopPrice': str(stop_price),
            'workingType': 'MARK_PRICE',
            'newOrderRespType': 'RESULT'
        }
        
        logger.info(f"[NATIVE API] Placing SL Algo Order for {symbol}: Side={side}, Stop={stop_price}, Qty={qty_str}, BinanceSymbol={binance_symbol}")
        logger.debug(f"[NATIVE API] SL Params: {params}")
        
        try:
            result = await self._request('POST', '/fapi/v1/algo/order', params=params, signed=True)
            logger.info(f"[NATIVE API] SL Algo Order placed successfully: ID={result.get('algoId')}, OrderId={result.get('orderList', [{}])[0].get('orderId') if result.get('orderList') else 'N/A'}")
            return result
        except Exception as e:
            logger.error(f"[NATIVE API] Failed to place SL: {e}")
            raise

    async def place_take_profit(self, symbol: str, side: str, tp_price: float, position_size: float = None) -> Dict:
        """
        Place a TAKE_PROFIT algo order using /fapi/v1/algo/order endpoint.
        Uses SUB_ORDER_TYPE = TAKE_PROFIT_MARKET with quantity.
        
        Reference: https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api/New-Algo-Order
        """
        binance_symbol = await self._normalize_symbol(symbol)
        
        # Get position size if not provided
        if position_size is None or position_size == 0:
            position_size, pos_side = await self._get_position_size(symbol)
            if position_size == 0:
                raise Exception(f"No open position found for {symbol}")
            logger.info(f"[TP] Detected position size: {position_size} for {symbol}")
        
        # Format quantity according to exchange precision (use stepSize from LOT_SIZE filter)
        # Most perpetual futures use integer quantities, but some like BTC allow decimals
        # We'll format with up to 3 decimal places and strip trailing zeros
        qty_str = f"{position_size:.3f}".rstrip('0').rstrip('.')
        
        # For TAKE_PROFIT algo order:
        # - algoType: TAKE_PROFIT
        # - algoSubType: TAKE_PROFIT_MARKET (for market order when triggered)
        # - side: opposite to position side (SELL for long, BUY for short)
        # - quantity: the amount to close
        # - stopPrice: trigger price
        # - workingType: MARK_PRICE or CONTRACT_PRICE
        
        params = {
            'symbol': binance_symbol,
            'algoType': 'TAKE_PROFIT',
            'algoSubType': 'TAKE_PROFIT_MARKET',
            'side': side.upper(),  # SELL for long position, BUY for short
            'quantity': qty_str,
            'stopPrice': str(tp_price),
            'workingType': 'MARK_PRICE',
            'newOrderRespType': 'RESULT'
        }
        
        logger.info(f"[NATIVE API] Placing TP Algo Order for {symbol}: Side={side}, TP={tp_price}, Qty={qty_str}, BinanceSymbol={binance_symbol}")
        logger.debug(f"[NATIVE API] TP Params: {params}")
        
        try:
            result = await self._request('POST', '/fapi/v1/algo/order', params=params, signed=True)
            logger.info(f"[NATIVE API] TP Algo Order placed successfully: ID={result.get('algoId')}, OrderId={result.get('orderList', [{}])[0].get('orderId') if result.get('orderList') else 'N/A'}")
            return result
        except Exception as e:
            logger.error(f"[NATIVE API] Failed to place TP: {e}")
            raise

    async def get_algo_orders(self, symbol: str = None) -> list:
        """Fetch open algo orders (including SL/TP)"""
        params = {}
        if symbol:
            params['symbol'] = await self._normalize_symbol(symbol)
        
        try:
            orders = await self._request('GET', '/fapi/v1/algo/openOrders', params=params, signed=True)
            return orders.get('data', [])
        except Exception as e:
            logger.error(f"Failed to fetch algo orders: {e}")
            return []

    async def cancel_algo_order(self, symbol: str, algo_id: str) -> bool:
        """Cancel a specific algo order (SL/TP)"""
        params = {
            'symbol': await self._normalize_symbol(symbol),
            'algoId': algo_id
        }
        try:
            await self._request('DELETE', '/fapi/v1/algo/order', params=params, signed=True)
            return True
        except Exception as e:
            logger.error(f"Failed to cancel algo order {algo_id}: {e}")
            return False

    async def cancel_all_algo_orders(self, symbol: str) -> bool:
        """Cancel all open algo orders for a symbol"""
        params = {
            'symbol': await self._normalize_symbol(symbol)
        }
        try:
            await self._request('DELETE', '/fapi/v1/algo/allOpenOrders', params=params, signed=True)
            return True
        except Exception as e:
            logger.error(f"Failed to cancel all algo orders for {symbol}: {e}")
            return False

    async def get_open_orders(self, symbol: str = None) -> list:
        """Fetch regular open orders (non-algo)"""
        params = {}
        if symbol:
            params['symbol'] = await self._normalize_symbol(symbol)
            
        try:
            orders = await self._request('GET', '/fapi/v1/openOrders', params=params, signed=True)
            return orders
        except Exception as e:
            logger.error(f"Failed to fetch open orders: {e}")
            return []

    async def cancel_order(self, symbol: str, order_id: int) -> bool:
        """Cancel a specific regular order"""
        params = {
            'symbol': await self._normalize_symbol(symbol),
            'orderId': order_id
        }
        try:
            await self._request('DELETE', '/fapi/v1/order', params=params, signed=True)
            return True
        except Exception as e:
            logger.error(f"Failed to cancel order {order_id}: {e}")
            return False

    async def cancel_all_open_orders(self, symbol: str) -> bool:
        """Cancel all regular open orders for a symbol"""
        params = {
            'symbol': await self._normalize_symbol(symbol)
        }
        try:
            await self._request('DELETE', '/fapi/v1/allOpenOrders', params=params, signed=True)
            return True
        except Exception as e:
            logger.error(f"Failed to cancel all orders for {symbol}: {e}")
            return False
