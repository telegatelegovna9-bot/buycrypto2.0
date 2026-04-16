"""
Native Binance Futures API Client for Risk Management.
Uses direct HTTP requests to fapi.binance.com for Algo Orders (SL/TP).
Reference: https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api/New-Algo-Order
"""
import hmac
import hashlib
import time
import json
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
        self.time_offset: int = 0  # Time offset from Binance server in ms
        
    async def start_session(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
            # Sync time with Binance server on startup
            await self._sync_time()
            
    async def close_session(self):
        if self.session and not self.session.closed:
            await self.session.close()
    
    async def _sync_time(self):
        """Sync local time with Binance server time."""
        try:
            async with self.session.get(f"{self.base_url}/fapi/v1/time") as response:
                if response.status == 200:
                    data = await response.json()
                    server_time = data.get('serverTime', 0)
                    local_time = int(time.time() * 1000)
                    self.time_offset = server_time - local_time
                    logger.info(f"[TIME SYNC] Server: {server_time}, Local: {local_time}, Offset: {self.time_offset}ms")
        except Exception as e:
            logger.warning(f"[TIME SYNC] Failed to sync time: {e}")
            self.time_offset = 0
            
    def _get_timestamp(self) -> int:
        """Get current timestamp with offset correction."""
        return int(time.time() * 1000) + self.time_offset
        
    def _generate_signature(self, query_string: str) -> str:
        return hmac.new(
            self.secret_key.encode('utf-8'),
            query_string.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        
    def _safe_preview(self, text: str, limit: int = 300) -> str:
        compact = " ".join(text.split())
        safe = compact.encode("ascii", errors="ignore").decode("ascii")
        return safe[:limit]

    async def _request(
        self,
        method: str,
        path: str,
        params: Dict[str, Any] = None,
        signed: bool = False,
        base_url: Optional[str] = None
    ) -> Dict:
        """Make HTTP request to Binance API with proper form encoding for orders."""
        if self.session is None:
            await self.start_session()
            
        url = f"{base_url or self.base_url}{path}"
        
        if params is None:
            params = {}
            
        if signed:
            params['timestamp'] = self._get_timestamp()
            params['recvWindow'] = 10000  # 10 seconds window for timestamp
            query_string = urlencode(params)
            params['signature'] = self._generate_signature(query_string)
            
        headers = {
            'X-MBX-APIKEY': self.api_key,
            'Content-Type': 'application/x-www-form-urlencoded'
        }
        
        try:
            # For POST requests, send data as form-encoded in body
            if method == 'POST':
                # Convert params to form-encoded string for POST body
                data_str = urlencode(params)
                async with self.session.request(method, url, data=data_str, headers=headers) as response:
                    text = await response.text()
                    
                    if response.status != 200:
                        # Try to parse error JSON, otherwise return text
                        try:
                            error_data = json.loads(text)
                            raise Exception(f"Binance API Error {response.status}: {error_data}")
                        except json.JSONDecodeError:
                            raise Exception(f"Binance API Error {response.status}: {self._safe_preview(text)}")
                    return json.loads(text)
            else:
                # GET request - params go in URL
                async with self.session.request(method, url, params=params, headers=headers) as response:
                    text = await response.text()
                    
                    if response.status != 200:
                        try:
                            error_data = json.loads(text)
                            raise Exception(f"Binance API Error {response.status}: {error_data}")
                        except json.JSONDecodeError:
                            raise Exception(f"Binance API Error {response.status}: {self._safe_preview(text)}")
                    return json.loads(text)
        except Exception as e:
            logger.error(f"Request failed: {self._safe_preview(str(e), limit=500)}")
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
        Place STOP_MARKET order via /fapi/v1/order with closePosition=true
        Reference: https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api/New-Order
        """
        binance_symbol = await self._normalize_symbol(symbol)
        
        # Get position size if not provided
        if position_size is None or position_size == 0:
            position_size, pos_side = await self._get_position_size(symbol)
            if position_size == 0:
                raise Exception(f"No open position found for {symbol}")
            logger.info(f"[SL] Detected position size: {position_size} for {symbol}")
        
        algo_params = {
            'symbol': binance_symbol,
            'side': side.upper(),
            'algoType': 'STOP_LOSS',
            'stopPrice': stop_price,
            'quantity': position_size,
            'reduceOnly': 'true',
            'workingType': 'MARK_PRICE'
        }
        
        logger.info(f"[NATIVE API] Placing SL via /algo/order for {symbol}: Side={side}, Stop={stop_price}")
        try:
            result = await self._request(
                'POST',
                '/fapi/v1/algoOrder',
                params=algo_params,
                signed=True
            )
            logger.info(f"[NATIVE API] SL algo order placed: AlgoId={result.get('algoId')}")
            return result
        except Exception as algo_error:
            logger.warning(f"[NATIVE API] SL algo order failed: {self._safe_preview(str(algo_error))}")
            raise

    async def place_take_profit(self, symbol: str, side: str, tp_price: float, position_size: float = None) -> Dict:
        """
        Place TAKE_PROFIT_MARKET order via /fapi/v1/order with closePosition=true
        Reference: https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api/New-Order
        """
        binance_symbol = await self._normalize_symbol(symbol)
        
        # Get position size if not provided
        if position_size is None or position_size == 0:
            position_size, pos_side = await self._get_position_size(symbol)
            if position_size == 0:
                raise Exception(f"No open position found for {symbol}")
            logger.info(f"[TP] Detected position size: {position_size} for {symbol}")
        
        algo_params = {
            'symbol': binance_symbol,
            'side': side.upper(),
            'algoType': 'TAKE_PROFIT',
            'stopPrice': tp_price,
            'quantity': position_size,
            'reduceOnly': 'true',
            'workingType': 'MARK_PRICE'
        }
        
        logger.info(f"[NATIVE API] Placing TP via /algo/order for {symbol}: Side={side}, TP={tp_price}")
        try:
            result = await self._request(
                'POST',
                '/fapi/v1/algoOrder',
                params=algo_params,
                signed=True
            )
            logger.info(f"[NATIVE API] TP algo order placed: AlgoId={result.get('algoId')}")
            return result
        except Exception as algo_error:
            logger.warning(f"[NATIVE API] TP algo order failed: {self._safe_preview(str(algo_error))}")
            raise

    async def get_algo_orders(self, symbol: str = None) -> list:
        """Fetch open algo orders (including SL/TP)"""
        params = {}
        if symbol:
            params['symbol'] = await self._normalize_symbol(symbol)
        
        try:
            orders = await self._request(
                'GET',
                '/fapi/v1/openAlgoOrders',
                params=params,
                signed=True
            )
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
            await self._request(
                'DELETE',
                '/fapi/v1/algoOrder',
                params=params,
                signed=True
            )
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
            await self._request(
                'DELETE',
                '/fapi/v1/algoOpenOrders',
                params=params,
                signed=True
            )
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
