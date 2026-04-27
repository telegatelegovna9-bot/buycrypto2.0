# Binance API Fix Summary - SL/TP Orders

## Problem
After recent Binance API updates, the bot was failing to place stop-loss and take-profit orders on the exchange side using the `/fapi/v1/algoOrder` endpoint. This resulted in:
- Multiple error messages in logs
- SL/TP being handled by the bot instead of the exchange
- Increased risk during bot downtime or latency

## Root Cause
Binance has been updating their API structure. The `algoOrder` endpoint (`/fapi/v1/algoOrder`) that was previously used for SL/TP orders has been:
1. Deprecated in some regions
2. Requiring different permissions
3. Less reliable than standard order types

## Solution Implemented

### Changed Order Type
**Before:** Using `algoOrder` endpoint with `algoType: STOP_LOSS` / `TAKE_PROFIT`
**After:** Using standard `/fapi/v1/order` endpoint with `type: STOP_MARKET` / `TAKE_PROFIT_MARKET`

### Key Changes in `binance_native_api.py`:

1. **Stop Loss Orders** (lines 160-202):
   - Changed from `/fapi/v1/algoOrder` to `/fapi/v1/order`
   - Changed order type from `algoType: STOP_LOSS` to `type: STOP_MARKET`
   - Added proper parameters: `priceProtect`, `timeInForce`, `newOrderRespType`
   - Fixed `reduceOnly` from string `'true'` to boolean `True`

2. **Take Profit Orders** (lines 204-246):
   - Changed from `/fapi/v1/algoOrder` to `/fapi/v1/order`
   - Changed order type from `algoType: TAKE_PROFIT` to `type: TAKE_PROFIT_MARKET`
   - Same parameter improvements as SL

3. **Order Cancellation** (lines 267-291):
   - Updated to use standard order cancel endpoint `/fapi/v1/order`
   - Changed from `algoId` to `orderId` parameter
   - Updated fetch method to filter regular orders by type

4. **Order Fetching** (lines 248-265):
   - Changed from `/fapi/v1/openAlgoOrders` to `/fapi/v1/openOrders`
   - Added filtering for SL/TP order types

## Technical Details

### New Order Parameters:
```python
{
    'symbol': 'BTCUSDT',
    'side': 'SELL',  # or 'BUY'
    'type': 'STOP_MARKET',  # or 'TAKE_PROFIT_MARKET'
    'stopPrice': 50000.00,
    'quantity': 0.001,
    'reduceOnly': True,  # Boolean, not string!
    'workingType': 'MARK_PRICE',  # or 'CONTRACT_PRICE'
    'priceProtect': True,  # Prevents trigger from outlier prices
    'timeInForce': 'GTC',  # Good Till Cancel
    'newOrderRespType': 'RESULT'  # Returns full order details
}
```

### API Endpoints Used:
- **Place Order:** `POST /fapi/v1/order`
- **Cancel Order:** `DELETE /fapi/v1/order`
- **Fetch Orders:** `GET /fapi/v1/openOrders`

## Benefits

1. **Better Compatibility**: Uses standard order endpoints that work globally
2. **More Reliable**: Standard endpoints have better uptime and support
3. **Faster Execution**: Direct order placement without algo layer
4. **Clearer Error Messages**: Standard errors are easier to debug
5. **Future-Proof**: Aligned with Binance's current API direction

## Testing Checklist

Before going live, verify:

- [ ] Bot can place STOP_MARKET orders successfully
- [ ] Bot can place TAKE_PROFIT_MARKET orders successfully
- [ ] Orders appear correctly in Binance UI
- [ ] Orders trigger at correct price levels
- [ ] Cancel functionality works for both SL and TP
- [ ] No errors in logs during order placement
- [ ] Position size is correctly detected and used
- [ ] reduceOnly flag prevents position reversal

## Monitoring

After deployment, watch for these log messages:

**Success:**
```
[NATIVE API] Placing SL via /fapi/v1/order for BTC/USDT:USDT: Side=sell, Stop=49500.00, Qty=0.001
[NATIVE API] SL order placed: OrderId=12345678, Status=NEW
[NATIVE API] TP order placed: OrderId=12345679, Status=NEW
```

**Errors to Watch:**
```
[NATIVE API] SL order failed: ...  # Investigate immediately
[SL ERROR] Failed to set stop loss on exchange: ...
No open position found for ...  # Check position sync
```

## Rollback Plan

If issues occur, you can temporarily revert to bot-side SL/TP management:

1. In `execution_engine.py`, catch exceptions from `set_stop_loss` and `set_take_profit`
2. Fall back to local monitoring in `position_monitor.py`
3. Log warnings but continue trading

## Additional Recommendations

1. **Test on Testnet First**: Use Binance Futures Testnet before live deployment
2. **Start Small**: Test with minimal position sizes initially
3. **Monitor Closely**: Watch first 10-20 trades carefully
4. **Keep Logs**: Preserve logs for troubleshooting any issues
5. **Check Permissions**: Ensure API key has "Enable Futures" permission

## Related Files Modified

- `/workspace/buycrypto2.0/binance_native_api.py` - Main API client (MODIFIED)
- `/workspace/buycrypto2.0/execution_engine.py` - Uses native API (no changes needed)
- `/workspace/buycrypto2.0/position_monitor.py` - Monitors positions (no changes needed)

## References

- [Binance Futures Order API](https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api/New-Order)
- [Stop Market Orders](https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api/Order-Types#stop-market)
- [Take Profit Market Orders](https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api/Order-Types#take-profit-market)

---

**Date:** 2026-01-XX
**Status:** ✅ IMPLEMENTED
**Tested:** ⏳ PENDING LIVE TEST
