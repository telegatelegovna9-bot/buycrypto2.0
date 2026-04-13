"""
Native Binance Futures API Client for Risk Management.
Uses direct HTTP requests to fapi.binance.com to bypass CCXT limitations
for STOP_MARKET and TAKE_PROFIT_MARKET orders.
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
    """Direct client for Binance Futures API (fapi)"""
    
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

    async def get_position_side_mode(self, symbol: str) -> str:
        """Check if account is in Hedge Mode or One-Way Mode"""
        # In a real production env, you might cache this or fetch once at startup
        # For now, we assume BOTH (One-Way mode compatible) as per previous logic
        # If you need dynamic detection, uncomment below:
        # pos_mode = await self._request('GET', '/fapi/v1/positionSide/dual', signed=True)
        # return 'BOTH' if not pos_mode.get('dualSidePosition') else 'LONG/SHORT'
        return 'BOTH'

    async def place_stop_loss(self, symbol: str, side: str, stop_price: float, position_size: float = None) -> Dict:
        """
        Place a STOP_MARKET order with closePosition=true.
        This ensures the entire position is closed when stop price is hit.
        """
        params = {
            'symbol': symbol.replace('/', ''), # Binance expects BTCUSDT, not BTC/USDT
            'side': side.upper(),
            'type': 'STOP_MARKET',
            'stopPrice': f"{stop_price:.8f}".rstrip('0').rstrip('.'),
            'closePosition': 'true',  # Critical: closes entire position
            'workingType': 'MARK_PRICE',
            'positionSide': 'BOTH',   # Compatible with One-Way mode
            'newOrderRespType': 'RESULT'
        }
        
        logger.info(f"[NATIVE API] Placing SL for {symbol}: Side={side}, Stop={stop_price}, Params={params}")
        
        try:
            result = await self._request('POST', '/fapi/v1/order', params=params, signed=True)
            logger.info(f"[NATIVE API] SL Order placed successfully: ID={result.get('orderId')}")
            return result
        except Exception as e:
            logger.error(f"[NATIVE API] Failed to place SL: {e}")
            raise

    async def place_take_profit(self, symbol: str, side: str, tp_price: float, position_size: float = None) -> Dict:
        """
        Place a TAKE_PROFIT_MARKET order with closePosition=true.
        """
        params = {
            'symbol': symbol.replace('/', ''),
            'side': side.upper(),
            'type': 'TAKE_PROFIT_MARKET',
            'stopPrice': f"{tp_price:.8f}".rstrip('0').rstrip('.'),
            'closePosition': 'true',
            'workingType': 'MARK_PRICE',
            'positionSide': 'BOTH',
            'newOrderRespType': 'RESULT'
        }
        
        logger.info(f"[NATIVE API] Placing TP for {symbol}: Side={side}, TP={tp_price}, Params={params}")
        
        try:
            result = await self._request('POST', '/fapi/v1/order', params=params, signed=True)
            logger.info(f"[NATIVE API] TP Order placed successfully: ID={result.get('orderId')}")
            return result
        except Exception as e:
            logger.error(f"[NATIVE API] Failed to place TP: {e}")
            raise

    async def get_open_orders(self, symbol: str = None) -> list:
        """Fetch open orders to verify SL/TP existence"""
        params = {}
        if symbol:
            params['symbol'] = symbol.replace('/', '')
            
        try:
            orders = await self._request('GET', '/fapi/v1/openOrders', params=params, signed=True)
            return orders
        except Exception as e:
            logger.error(f"Failed to fetch open orders: {e}")
            return []

    async def cancel_order(self, symbol: str, order_id: int) -> bool:
        """Cancel a specific order"""
        params = {
            'symbol': symbol.replace('/', ''),
            'orderId': order_id
        }
        try:
            await self._request('DELETE', '/fapi/v1/order', params=params, signed=True)
            return True
        except Exception as e:
            logger.error(f"Failed to cancel order {order_id}: {e}")
            return False

    async def cancel_all_open_orders(self, symbol: str) -> bool:
        """Cancel all open orders for a symbol"""
        params = {
            'symbol': symbol.replace('/', '')
        }
        try:
            await self._request('DELETE', '/fapi/v1/allOpenOrders', params=params, signed=True)
            return True
        except Exception as e:
            logger.error(f"Failed to cancel all orders for {symbol}: {e}")
            return False
