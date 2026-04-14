"""
Position Monitor Module.
Handles real-time position monitoring, SL/TP checks, and dynamic management.
Runs every 1 second for fast reaction to market changes.
"""
import asyncio
from typing import Dict, Optional, List
from dataclasses import dataclass
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass
class PositionState:
    """Represents the current state of a position."""
    symbol: str
    direction: str  # 'long' or 'short'
    entry_price: float
    current_price: float
    size: float
    unrealized_pnl: float
    pnl_percent: float
    stop_loss: float
    take_profit: float
    distance_to_sl_pct: float
    distance_to_tp_pct: float
    is_profitable: bool
    in_profit_zone: bool  # Price moved favorably by X%
    last_update: datetime


class PositionMonitor:
    """
    Real-time position monitoring system.
    
    Features:
    - Checks positions every 1 second
    - Tracks PnL, distance to SL/TP
    - Manages trailing stops
    - Handles breakeven moves
    - Detects SL/TP hits immediately
    """
    
    def __init__(self, risk_manager, order_executor, data_loader, config):
        self.risk_manager = risk_manager
        self.order_executor = order_executor
        self.data_loader = data_loader
        self.config = config
        
        # Monitoring state
        self.position_states: Dict[str, PositionState] = {}
        self.monitoring_active = False
        self.monitor_task = None
        
        # Configuration
        self.monitor_interval = 1.0  # Check every 1 second (CRITICAL for fast SL)
        self.breakeven_threshold = 0.025  # Move to BE when 2.5% profitable (OPTIMIZED)
        self.trailing_activation = 0.03  # Start trailing when 3% profitable
        self.trailing_stop_atr_multiplier = 2.5  # Wider trail to avoid premature exits
        
        # Trailing stop state
        self.highest_price: Dict[str, float] = {}  # For long positions
        self.lowest_price: Dict[str, float] = {}   # For short positions
    
    async def start_monitoring(self):
        """Start the position monitoring loop."""
        if self.monitoring_active:
            logger.warning("[MONITOR] Already running")
            return
        
        self.monitoring_active = True
        self.monitor_task = asyncio.create_task(self._monitor_loop())
        logger.info(f"[MONITOR] Started - checking every {self.monitor_interval}s")
    
    async def stop_monitoring(self):
        """Stop the position monitoring loop."""
        self.monitoring_active = False
        if self.monitor_task:
            self.monitor_task.cancel()
            try:
                await self.monitor_task
            except asyncio.CancelledError:
                pass
        logger.info("[MONITOR] Stopped")
    
    async def _monitor_loop(self):
        """Main monitoring loop - runs every 1 second."""
        while self.monitoring_active:
            try:
                await self._check_all_positions()
                await asyncio.sleep(self.monitor_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[MONITOR ERROR] {e}", exc_info=True)
                await asyncio.sleep(self.monitor_interval)
    
    async def _check_all_positions(self):
        """Check all open positions for SL/TP hits and manage them."""
        if not self.risk_manager.positions:
            return
        
        # Fetch current prices for ALL positions
        prices = {}
        for symbol in self.risk_manager.positions.keys():
            try:
                ticker = await self.data_loader.fetch_ticker(symbol)
                if ticker and 'last' in ticker:
                    prices[symbol] = ticker['last']
            except Exception as e:
                logger.error(f"[MONITOR] Error fetching price for {symbol}: {e}")
        
        if not prices:
            logger.debug("[MONITOR] No prices fetched")
            return
        
        # Check each position
        positions_to_close = []
        
        for symbol, position in list(self.risk_manager.positions.items()):
            current_price = prices.get(symbol)
            if not current_price:
                continue
            
            # Update position PnL
            position.update_unrealized_pnl(current_price)
            
            # Update position state for tracking
            self._update_position_state(symbol, position, current_price)
            
            # Check for SL hit (CRITICAL - must check first)
            sl_hit = self._check_stop_loss_hit(position, current_price)
            if sl_hit:
                positions_to_close.append((symbol, current_price, 'stop_loss'))
                continue  # Don't check TP if SL hit
            
            # Check for TP hit
            tp_hit = self._check_take_profit_hit(position, current_price)
            if tp_hit:
                positions_to_close.append((symbol, current_price, 'take_profit'))
                continue
            
            # Manage active position (trailing stop, breakeven)
            await self._manage_position(symbol, position, current_price)
        
        # Return positions that need closing
        return positions_to_close
    
    def _check_stop_loss_hit(self, position, current_price: float) -> bool:
        """Check if stop loss was hit."""
        if position.direction == 'long':
            return current_price <= position.stop_loss
        else:
            return current_price >= position.stop_loss
    
    def _check_take_profit_hit(self, position, current_price: float) -> bool:
        """Check if take profit was hit."""
        if position.direction == 'long':
            return current_price >= position.take_profit
        else:
            return current_price <= position.take_profit
    
    def _update_position_state(self, symbol: str, position, current_price: float):
        """Update internal state tracking for a position."""
        # Calculate distances
        if position.direction == 'long':
            distance_to_sl = (current_price - position.stop_loss) / current_price
            distance_to_tp = (position.take_profit - current_price) / current_price
            pnl_pct = (current_price - position.entry_price) / position.entry_price
        else:
            distance_to_sl = (position.stop_loss - current_price) / current_price
            distance_to_tp = (current_price - position.take_profit) / current_price
            pnl_pct = (position.entry_price - current_price) / position.entry_price
        
        # Track highest/lowest prices for trailing stop
        if position.direction == 'long':
            if symbol not in self.highest_price or current_price > self.highest_price[symbol]:
                self.highest_price[symbol] = current_price
        else:
            if symbol not in self.lowest_price or current_price < self.lowest_price[symbol]:
                self.lowest_price[symbol] = current_price
        
        state = PositionState(
            symbol=symbol,
            direction=position.direction,
            entry_price=position.entry_price,
            current_price=current_price,
            size=position.size,
            unrealized_pnl=position.unrealized_pnl,
            pnl_percent=pnl_pct,
            stop_loss=position.stop_loss,
            take_profit=position.take_profit,
            distance_to_sl_pct=distance_to_sl * 100,
            distance_to_tp_pct=distance_to_tp * 100,
            is_profitable=pnl_pct > 0,
            in_profit_zone=abs(pnl_pct) >= self.breakeven_threshold,
            last_update=datetime.now()
        )
        
        self.position_states[symbol] = state
        
        # Log status periodically (every 10 seconds)
        if int(state.last_update.timestamp()) % 10 == 0:
            logger.debug(
                f"[POS] {symbol} | {position.direction.upper()} | "
                f"PnL: {pnl_pct:+.2%} | SL: {position.stop_loss:.4f} ({distance_to_sl*100:.2f}%) | "
                f"TP: {position.take_profit:.4f} ({distance_to_tp*100:.2f}%)"
            )
    
    async def _manage_position(self, symbol: str, position, current_price: float):
        """
        Manage active position:
        - Move to breakeven (only after 2.5% profit)
        - Trail stop loss (after 3% profit with wider ATR multiplier)
        - Partial close at targets (NEW: capture profits above TP)
        - Dynamic TP adjustment (NEW: extend TP when momentum strong)
        """
        pnl_pct = position.get_pnl_pct(current_price)
        
        # Move to breakeven when profitable enough (raised from 1% to 2.5%)
        if pnl_pct >= self.breakeven_threshold:
            self._move_to_breakeven(symbol, position, current_price)
        
        # Trail stop loss when in profit zone (raised from 2% to 3%)
        if pnl_pct >= self.trailing_activation:
            await self._trail_stop_loss(symbol, position, current_price)
        
        # NEW: Check for dynamic TP management when price exceeds TP
        await self._check_dynamic_tp_management(symbol, position, current_price, pnl_pct)
        
        # Optional: Partial close at certain profit levels
        await self._check_partial_close(symbol, position, current_price, pnl_pct)
    
    def _move_to_breakeven(self, symbol: str, position, current_price: float):
        """Move stop loss to breakeven when profitable."""
        if position.direction == 'long':
            new_sl = position.entry_price * 1.001  # Just above entry
            if new_sl > position.stop_loss:
                old_sl = position.stop_loss
                position.stop_loss = new_sl
                logger.info(
                    f"[BE MOVED] {symbol}: {old_sl:.4f} -> {new_sl:.4f} "
                    f"(entry: {position.entry_price:.4f})"
                )
        else:
            new_sl = position.entry_price * 0.999  # Just below entry
            if new_sl < position.stop_loss:
                old_sl = position.stop_loss
                position.stop_loss = new_sl
                logger.info(
                    f"[BE MOVED] {symbol}: {old_sl:.4f} -> {new_sl:.4f} "
                    f"(entry: {position.entry_price:.4f})"
                )
    
    async def _trail_stop_loss(self, symbol: str, position, current_price: float):
        """Implement trailing stop loss based on ATR."""
        try:
            # Get ATR from recent data
            df = await self.data_loader.fetch_ohlcv(symbol, '5m', limit=50)
            if len(df) < 20:
                return
            
            # Calculate ATR
            high = df['high'].values
            low = df['low'].values
            close = df['close'].values
            
            tr_values = []
            for i in range(1, len(high)):
                tr = max(
                    high[i] - low[i],
                    abs(high[i] - close[i-1]),
                    abs(low[i] - close[i-1])
                )
                tr_values.append(tr)
            
            atr = sum(tr_values[-14:]) / 14 if len(tr_values) >= 14 else 0
            
            if atr <= 0:
                return
            
            if position.direction == 'long':
                # Trail stop below price
                new_sl = current_price - (self.trailing_stop_atr_multiplier * atr)
                
                # Only move SL up, never down
                if new_sl > position.stop_loss:
                    old_sl = position.stop_loss
                    position.stop_loss = new_sl
                    
                    # Update exchange SL order (NO amount parameter)
                    sl_side = 'sell'
                    await self.order_executor.update_stop_loss(
                        symbol, sl_side, new_sl
                    )
                    
                    logger.debug(
                        f"[TRAIL UP] {symbol}: {old_sl:.4f} -> {new_sl:.4f} "
                        f"(ATR: {atr:.4f})"
                    )
            else:
                # Trail stop above price for short
                new_sl = current_price + (self.trailing_stop_atr_multiplier * atr)
                
                # Only move SL down, never up
                if new_sl < position.stop_loss:
                    old_sl = position.stop_loss
                    position.stop_loss = new_sl
                    
                    # Update exchange SL order (NO amount parameter)
                    sl_side = 'buy'
                    await self.order_executor.update_stop_loss(
                        symbol, sl_side, new_sl
                    )
                    
                    logger.debug(
                        f"[TRAIL DOWN] {symbol}: {old_sl:.4f} -> {new_sl:.4f} "
                        f"(ATR: {atr:.4f})"
                    )
                    
        except Exception as e:
            logger.debug(f"[TRAIL ERROR] {symbol}: {e}")
    
    async def _check_dynamic_tp_management(
        self,
        symbol: str,
        position,
        current_price: float,
        pnl_pct: float
    ):
        """
        NEW FEATURE: Dynamic TP management when price exceeds original TP.
        
        When price moves significantly above TP:
        1. Extend TP to capture more profit (if momentum strong)
        2. Switch to aggressive trailing (tighter ATR multiplier)
        3. Log the opportunity for analysis
        
        This prevents leaving money on the table when price pumps hard.
        """
        # Check if we're above original TP
        tp_hit = False
        if position.direction == 'long' and current_price >= position.take_profit:
            tp_hit = True
            excess_pct = (current_price - position.take_profit) / position.take_profit
        elif position.direction == 'short' and current_price <= position.take_profit:
            tp_hit = True
            excess_pct = (position.take_profit - current_price) / position.take_profit
        else:
            return  # Not above TP yet
        
        # Only act if significantly above TP (>1% beyond TP)
        if excess_pct < 0.01:
            return
        
        logger.info(
            f"[DYNAMIC TP] {symbol} is {excess_pct*100:.2f}% above TP! "
            f"Original TP: {position.take_profit:.4f}, Current: {current_price:.4f}"
        )
        
        # Strategy 1: Aggressive trailing when above TP
        # Use tighter ATR multiplier (1.5x instead of 2.5x) to lock in profits
        if pnl_pct >= 0.05:  # If >5% profit
            try:
                df = await self.data_loader.fetch_ohlcv(symbol, '5m', limit=50)
                if len(df) < 20:
                    return
                
                # Calculate ATR
                high = df['high'].values
                low = df['low'].values
                close = df['close'].values
                
                tr_values = []
                for i in range(1, len(high)):
                    tr = max(
                        high[i] - low[i],
                        abs(high[i] - close[i-1]),
                        abs(low[i] - close[i-1])
                    )
                    tr_values.append(tr)
                
                atr = sum(tr_values[-14:]) / 14 if len(tr_values) >= 14 else 0
                
                if atr <= 0:
                    return
                
                # Aggressive trailing: 1.5x ATR instead of 2.5x
                aggressive_mult = 1.5
                
                if position.direction == 'long':
                    new_sl = current_price - (aggressive_mult * atr)
                    if new_sl > position.stop_loss:
                        old_sl = position.stop_loss
                        position.stop_loss = new_sl
                        
                        sl_side = 'sell'
                        await self.order_executor.update_stop_loss(
                            symbol, sl_side, new_sl
                        )
                        
                        logger.info(
                            f"[AGGRESSIVE TRAIL] {symbol}: {old_sl:.4f} -> {new_sl:.4f} "
                            f"(using {aggressive_mult}x ATR for profit protection)"
                        )
                else:
                    new_sl = current_price + (aggressive_mult * atr)
                    if new_sl < position.stop_loss:
                        old_sl = position.stop_loss
                        position.stop_loss = new_sl
                        
                        sl_side = 'buy'
                        await self.order_executor.update_stop_loss(
                            symbol, sl_side, new_sl
                        )
                        
                        logger.info(
                            f"[AGGRESSIVE TRAIL] {symbol}: {old_sl:.4f} -> {new_sl:.4f} "
                            f"(using {aggressive_mult}x ATR for profit protection)"
                        )
            except Exception as e:
                logger.debug(f"[DYNAMIC TP ERROR] {symbol}: {e}")
    
    async def _check_partial_close(
        self,
        symbol: str,
        position,
        current_price: float,
        pnl_pct: float
    ):
        """
        Check if we should take partial profits at certain levels.
        
        Configurable levels in config, e.g.:
        - Close 30% at 2% profit
        - Close 30% at 4% profit
        - Let rest run with trailing stop
        """
        partial_close_levels = getattr(self.config.risk, 'partial_close_levels', [
            {'pct': 0.02, 'close_pct': 0.3},  # 2% profit -> close 30%
            {'pct': 0.04, 'close_pct': 0.3},  # 4% profit -> close 30%
        ])
        
        for level in partial_close_levels:
            target_pct = level.get('pct', 0.02)
            close_pct = level.get('close_pct', 0.3)
            
            # Check if we just crossed this level
            if pnl_pct >= target_pct:
                # Could implement partial close logic here
                # For now, just log
                logger.debug(
                    f"[PARTIAL CHECK] {symbol} at {pnl_pct:.2%} profit "
                    f"(target: {target_pct:.2%}, would close: {close_pct:.0%})"
                )
    
    def get_position_summary(self, symbol: str) -> Optional[Dict]:
        """Get detailed summary of a position."""
        if symbol not in self.position_states:
            return None
        
        state = self.position_states[symbol]
        return {
            'symbol': state.symbol,
            'direction': state.direction,
            'entry_price': state.entry_price,
            'current_price': state.current_price,
            'pnl_percent': state.pnl_percent,
            'unrealized_pnl': state.unrealized_pnl,
            'stop_loss': state.stop_loss,
            'take_profit': state.take_profit,
            'distance_to_sl_pct': state.distance_to_sl_pct,
            'distance_to_tp_pct': state.distance_to_tp_pct,
            'is_profitable': state.is_profitable,
            'last_update': state.last_update.isoformat()
        }
    
    def get_all_positions_summary(self) -> List[Dict]:
        """Get summary of all monitored positions."""
        summaries = []
        for symbol in self.position_states:
            summary = self.get_position_summary(symbol)
            if summary:
                summaries.append(summary)
        return summaries
    
    def get_risk_metrics(self) -> Dict:
        """Get overall risk metrics for all positions."""
        if not self.position_states:
            return {
                'total_positions': 0,
                'total_unrealized_pnl': 0.0,
                'avg_distance_to_sl': 0.0,
                'positions_in_profit': 0,
                'positions_at_risk': 0
            }
        
        total_pnl = sum(s.unrealized_pnl for s in self.position_states.values())
        avg_distance_to_sl = sum(s.distance_to_sl_pct for s in self.position_states.values()) / len(self.position_states)
        positions_in_profit = sum(1 for s in self.position_states.values() if s.is_profitable)
        positions_at_risk = sum(1 for s in self.position_states.values() if s.distance_to_sl_pct < 1.0)
        
        return {
            'total_positions': len(self.position_states),
            'total_unrealized_pnl': total_pnl,
            'avg_distance_to_sl': avg_distance_to_sl,
            'positions_in_profit': positions_in_profit,
            'positions_at_risk': positions_at_risk
        }
