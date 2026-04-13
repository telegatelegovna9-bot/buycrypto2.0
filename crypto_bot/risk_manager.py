"""
Risk Management Module.
Handles position sizing, stop-loss, take-profit, and portfolio risk controls.
"""
from dataclasses import dataclass
from typing import Dict, Optional, List
import logging

logger = logging.getLogger(__name__)


@dataclass
class Position:
    """Represents an open trading position."""
    symbol: str
    direction: str  # 'long' or 'short'
    entry_price: float
    size: float  # Position size in base currency
    leverage: int
    stop_loss: float
    take_profit: float
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    
    def update_unrealized_pnl(self, current_price: float):
        """Update unrealized PnL based on current price."""
        if self.direction == 'long':
            self.unrealized_pnl = (current_price - self.entry_price) * self.size
        else:
            self.unrealized_pnl = (self.entry_price - current_price) * self.size
    
    def get_pnl_pct(self, current_price: float) -> float:
        """Get PnL as percentage of entry."""
        if self.direction == 'long':
            return (current_price - self.entry_price) / self.entry_price
        else:
            return (self.entry_price - current_price) / self.entry_price


class RiskManager:
    """
    Central risk management system.
    Controls position sizing, exposure limits, and drawdown protection.
    """
    
    def __init__(self, config):
        self.config = config
        self.risk_config = config.risk
        self.leverage_config = config.leverage
        
        # Balance will be set from exchange in live mode, or from config in backtest
        self.balance = self.config.backtest.initial_balance
        self.positions: Dict[str, Position] = {}
        self.closed_trades = []
        
        # Drawdown tracking
        self.peak_balance = self.balance
        self.current_drawdown = 0.0
        self.max_drawdown = 0.0
        
        # Daily limits
        self.daily_pnl = 0.0
        self.daily_trades = 0
    
    async def sync_balance_from_exchange(self, execution_engine):
        """
        Sync balance from real exchange (for live trading).
        Should be called after initialization in live mode.
        """
        try:
            # Получаем полный объект баланса из execution_engine
            balance_dict = await execution_engine.get_balance()
            
            # Пытаемся получить USDT баланс разными способами
            usdt_balance = 0.0
            
            # Способ 1: Прямой доступ через balance['USDT']
            if 'USDT' in balance_dict:
                usdt_data = balance_dict['USDT']
                if isinstance(usdt_data, dict):
                    usdt_balance = float(usdt_data.get('total', usdt_data.get('free', 0)))
                elif isinstance(usdt_data, (int, float)):
                    usdt_balance = float(usdt_data)
            
            # Логирование для отладки
            logger.info(f"Balance dict received: {balance_dict}")
            logger.info(f"Extracted USDT balance: ${usdt_balance:.2f}")
            
            if usdt_balance > 0:
                old_balance = self.balance
                self.balance = usdt_balance
                self.peak_balance = usdt_balance
                logger.info(f"[OK] Synced balance from exchange: ${usdt_balance:.2f} (was: ${old_balance:.2f})")
            else:
                logger.warning(f"No USDT balance found on exchange, using configured balance: ${self.balance:.2f}")
        except Exception as e:
            logger.error(f"Failed to sync balance from exchange: {e}")
            logger.warning(f"Using configured balance: ${self.balance:.2f}")
    
    def calculate_position_size(
        self, 
        symbol: str, 
        entry_price: float, 
        stop_loss: float,
        confidence: float = 0.5
    ) -> float:
        """
        Calculate position size based on risk per trade.
        
        Args:
            symbol: Trading pair
            entry_price: Entry price
            stop_loss: Stop loss price
            confidence: Signal confidence (0-1)
        
        Returns:
            Position size in base currency
        """
        # Calculate risk amount (in quote currency)
        risk_amount = self.balance * self.risk_config.risk_per_trade
        
        # Calculate risk per unit
        if self.risk_config.risk_per_trade > 0:
            risk_per_unit = abs(entry_price - stop_loss)
            
            if risk_per_unit <= 0:
                logger.warning(f"Invalid stop loss for {symbol}")
                return 0.0
            
            # Base position size
            position_size = risk_amount / risk_per_unit
            
            # Adjust by confidence (higher confidence = larger position)
            confidence_multiplier = 0.5 + confidence * 0.5  # 0.5 to 1.0
            position_size *= confidence_multiplier
            
            return position_size
        
        return 0.0
    
    def calculate_leverage(self, confidence: float, volatility: float = 0.02) -> int:
        """
        Calculate dynamic leverage based on signal confidence and volatility.
        
        Args:
            confidence: Signal confidence (0-1)
            volatility: Market volatility (ATR/price)
        
        Returns:
            Leverage multiplier
        """
        # Reduce leverage in high volatility
        vol_adjustment = max(0.5, 1.0 - volatility * 10)  # Reduce if vol > 5%
        
        # Base leverage on confidence
        if confidence < self.leverage_config.low_confidence_threshold:
            base_leverage = self.leverage_config.min_leverage
        elif confidence < self.leverage_config.medium_confidence_threshold:
            base_leverage = 4
        elif confidence < self.leverage_config.high_confidence_threshold:
            base_leverage = 6
        else:
            base_leverage = self.leverage_config.max_leverage
        
        # Apply volatility adjustment
        final_leverage = int(base_leverage * vol_adjustment)
        
        # Clamp to min/max
        return max(
            self.leverage_config.min_leverage,
            min(final_leverage, self.leverage_config.max_leverage)
        )
    
    def can_open_position(self, symbol: str) -> bool:
        """Check if we can open a new position."""
        # Check max positions limit
        if len(self.positions) >= self.risk_config.max_positions:
            return False
        
        # Check if already have position in this symbol
        if symbol in self.positions:
            return False
        
        # Check drawdown limit
        if self.current_drawdown >= self.risk_config.max_drawdown:
            logger.warning("Max drawdown reached, no new positions")
            return False
        
        return True
    
    def create_position(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
        confidence: float,
        market_info: Optional[Dict] = None
    ) -> Optional[Position]:
        """
        Create a new position with proper risk management.
        
        Args:
            symbol: Trading pair
            direction: 'long' or 'short'
            entry_price: Entry price
            stop_loss: Stop loss price (if 0, will be calculated)
            take_profit: Take profit price (if 0, will be calculated)
            confidence: Signal confidence (0-1)
            market_info: Optional dict with 'min_notional', 'step_size', etc.
        
        Returns:
            Position object or None if cannot open
        """
        if not self.can_open_position(symbol):
            return None
        
        # Calculate stop loss and take profit if not provided
        if stop_loss <= 0 or take_profit <= 0:
            if direction == 'long':
                stop_loss = entry_price * (1 - self.risk_config.stop_loss_pct)
                take_profit = entry_price * (1 + self.risk_config.take_profit_pct)
            else:
                stop_loss = entry_price * (1 + self.risk_config.stop_loss_pct)
                take_profit = entry_price * (1 - self.risk_config.take_profit_pct)
        
        # Calculate position size
        size = self.calculate_position_size(symbol, entry_price, stop_loss, confidence)
        
        if size <= 0:
            return None
        
        # Apply minimum notional value constraint (Binance requires min $5 for futures)
        min_notional = 5.0  # Binance minimum
        if market_info and 'min_notional' in market_info:
            min_notional = market_info['min_notional']
        
        position_value = entry_price * size
        if position_value < min_notional:
            # Increase size to meet minimum
            size = min_notional / entry_price
            logger.info(f"Adjusted {symbol} size from {position_value/entry_price:.4f} to {size:.4f} to meet min notional ${min_notional}")
        
        # Apply step size rounding if market info available
        if market_info and 'step_size' in market_info:
            step_size = market_info['step_size']
            if step_size > 0:
                size = round(size / step_size) * step_size
                logger.debug(f"Rounded {symbol} size to step {step_size}: {size:.4f}")
        
        # Recalculate position value after adjustments
        position_value = entry_price * size
        if position_value < min_notional:
            logger.warning(f"Cannot open {symbol}: even adjusted position value ${position_value:.2f} is below min ${min_notional}")
            return None
        
        # Calculate leverage
        volatility = abs(entry_price - stop_loss) / entry_price if entry_price > 0 else 0.02
        leverage = self.calculate_leverage(confidence, volatility)
        
        # Create position
        position = Position(
            symbol=symbol,
            direction=direction,
            entry_price=entry_price,
            size=size,
            leverage=leverage,
            stop_loss=stop_loss,
            take_profit=take_profit
        )
        
        self.positions[symbol] = position
        logger.info(f"Opened {direction} position on {symbol}: size={size:.4f}, value=${position_value:.2f}, leverage={leverage}x")
        
        return position
    
    def update_positions(self, prices: Dict[str, float]):
        """Update all positions with current prices."""
        for symbol, position in self.positions.items():
            if symbol in prices:
                position.update_unrealized_pnl(prices[symbol])
    
    def check_stop_loss_take_profit(self, symbol: str, current_price: float) -> Optional[str]:
        """
        Check if SL or TP was hit.
        
        Returns:
            'sl', 'tp', or None
        """
        if symbol not in self.positions:
            return None
        
        position = self.positions[symbol]
        
        if position.direction == 'long':
            if current_price <= position.stop_loss:
                return 'sl'
            if current_price >= position.take_profit:
                return 'tp'
        else:  # short
            if current_price >= position.stop_loss:
                return 'sl'
            if current_price <= position.take_profit:
                return 'tp'
        
        return None
    
    def close_position(
        self, 
        symbol: str, 
        close_price: float, 
        reason: str = 'manual'
    ) -> float:
        """
        Close a position and realize PnL.
        
        Returns:
            Realized PnL
        """
        if symbol not in self.positions:
            return 0.0
        
        position = self.positions.pop(symbol)
        
        # Calculate realized PnL
        if position.direction == 'long':
            pnl = (close_price - position.entry_price) * position.size
        else:
            pnl = (position.entry_price - close_price) * position.size
        
        # Apply commission
        commission = close_price * position.size * self.config.backtest.commission_rate * 2
        pnl -= commission
        
        position.realized_pnl = pnl
        
        # Update balance
        self.balance += pnl
        self.daily_pnl += pnl
        
        # Track closed trades
        self.closed_trades.append({
            'symbol': symbol,
            'direction': position.direction,
            'entry_price': position.entry_price,
            'close_price': close_price,
            'pnl': pnl,
            'reason': reason,
            'leverage': position.leverage
        })
        
        # Update peak and drawdown
        if self.balance > self.peak_balance:
            self.peak_balance = self.balance
        
        if self.peak_balance > 0:
            self.current_drawdown = (self.peak_balance - self.balance) / self.peak_balance
            self.max_drawdown = max(self.max_drawdown, self.current_drawdown)
        
        logger.info(
            f"Closed {symbol} {reason}: PnL={pnl:.2f}, Balance={self.balance:.2f}"
        )
        
        return pnl
    
    def move_stop_loss_to_breakeven(self, symbol: str, current_price: float):
        """Move stop loss to breakeven when profitable."""
        if symbol not in self.positions:
            return
        
        position = self.positions[symbol]
        
        if position.direction == 'long':
            if current_price > position.entry_price * 1.01:  # 1% profit
                position.stop_loss = position.entry_price * 1.001  # Just above entry
        else:  # short
            if current_price < position.entry_price * 0.99:  # 1% profit
                position.stop_loss = position.entry_price * 1.001  # Just below entry
    
    def trail_stop_loss(self, symbol: str, current_price: float, atr: float):
        """Implement trailing stop loss."""
        if symbol not in self.positions:
            return
        
        position = self.positions[symbol]
        
        if position.direction == 'long':
            # Trail stop below price
            new_sl = current_price - 2 * atr
            if new_sl > position.stop_loss:
                position.stop_loss = new_sl
        else:  # short
            # Trail stop above price
            new_sl = current_price + 2 * atr
            if new_sl < position.stop_loss:
                position.stop_loss = new_sl
    
    def partial_close(
        self, 
        symbol: str, 
        close_price: float, 
        percentage: float
    ) -> float:
        """
        Close partial position (e.g., take 50% profits).
        
        Args:
            symbol: Trading pair
            close_price: Current price
            percentage: Percentage to close (0-1)
        
        Returns:
            Realized PnL from partial close
        """
        if symbol not in self.positions:
            return 0.0
        
        position = self.positions[symbol]
        
        # Calculate partial size
        close_size = position.size * percentage
        remaining_size = position.size * (1 - percentage)
        
        # Calculate PnL on closed portion
        if position.direction == 'long':
            pnl = (close_price - position.entry_price) * close_size
        else:
            pnl = (position.entry_price - close_price) * close_size
        
        # Update position
        position.size = remaining_size
        position.realized_pnl += pnl
        
        # Update balance
        self.balance += pnl
        
        logger.info(
            f"Partial close {symbol}: {percentage*100:.0f}%, PnL={pnl:.2f}"
        )
        
        return pnl
    
    def get_total_exposure(self) -> float:
        """Get total account exposure."""
        total = 0
        for position in self.positions.values():
            exposure = position.entry_price * position.size * position.leverage
            total += abs(exposure)
        return total
    
    def get_summary(self) -> Dict:
        """Get risk manager summary."""
        return {
            'balance': self.balance,
            'peak_balance': self.peak_balance,
            'current_drawdown': self.current_drawdown,
            'max_drawdown': self.max_drawdown,
            'open_positions': len(self.positions),
            'total_trades': len(self.closed_trades),
            'daily_pnl': self.daily_pnl,
            'winning_trades': sum(1 for t in self.closed_trades if t['pnl'] > 0),
            'losing_trades': sum(1 for t in self.closed_trades if t['pnl'] <= 0)
        }
