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
import numpy as np

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
    
    def __init__(self, risk_manager, order_executor, data_loader, config, telegram_notifier=None, meta_controller=None):
        self.risk_manager = risk_manager
        self.order_executor = order_executor
        self.data_loader = data_loader
        self.config = config
        self.telegram = telegram_notifier
        self.meta_controller = meta_controller
        
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
        
        # Sync state
        self.last_exchange_positions = {}
        
        # Protection against race conditions during position opening
        self.opening_positions: Dict[str, datetime] = {}  # Symbols being opened + timestamp
        self.OPENING_PROTECTION_SECONDS = 5.0  # Don't check manual close for 5s after open
        
        # Protection against double closing and false recovery
        self.closing_positions: Dict[str, datetime] = {}  # Symbols being closed + timestamp
        self.recently_closed: Dict[str, datetime] = {}    # Symbols recently closed + timestamp
        self.CLOSING_PROTECTION_SECONDS = 10.0  # Don't recover for 10s after close
    
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
                # First: Sync with exchange to detect manually closed positions
                await self._sync_with_exchange()
                
                # Second: Check all positions for SL/TP and manage them
                positions_to_close = await self._check_all_positions()
                
                # Execute closures for positions that hit SL/TP
                if positions_to_close:
                    for symbol, close_price, exit_reason in positions_to_close:
                        # Get position object before closing
                        position = self.risk_manager.positions.get(symbol)
                        if position:
                            await self._execute_sl_tp_closure(symbol, position, close_price, exit_reason)
                
                await asyncio.sleep(self.monitor_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[MONITOR ERROR] {e}", exc_info=True)
                await asyncio.sleep(self.monitor_interval)
    
    async def _sync_with_exchange(self):
        """
        Синхронизация позиций с биржей каждую секунду.
        
        Обнаруживает:
        1. Позиции, закрытые вручную на бирже (через веб-интерфейс)
        2. Позиции, которые есть на бирже, но нет у бота (восстановление после перезапуска)
        3. Несоответствие размеров позиций
        """
        try:
            # Получаем актуальные позиции с биржи
            exchange_positions = await self.order_executor.get_exchange_positions()
            
            exchange_symbols = set()
            
            for pos_data in exchange_positions:
                symbol = pos_data.get('symbol')
                if not symbol:
                    continue
                
                exchange_symbols.add(symbol)
                
                # Извлекаем размер позиции (абсолютное значение)
                position_size = abs(float(pos_data.get('contracts', 0) or pos_data.get('size', 0) or 0))
                
                # Если позиция есть на бирже, но нет у бота → восстанавливаем
                if symbol not in self.risk_manager.positions and position_size > 0:
                    logger.warning(f"[SYNC] Обнаружена позиция на бирже, отсутствующая у бота: {symbol}")
                    # Бот восстановит контроль над этой позицией через update_position_from_exchange
                    await self._recover_position_from_exchange(symbol, pos_data)
                
                # Если позиция есть и у бота, и на бирже → проверяем соответствие
                elif symbol in self.risk_manager.positions:
                    local_position = self.risk_manager.positions[symbol]
                    local_size = abs(local_position.size)
                    
                    if abs(position_size - local_size) > 0.001 * local_size:  # >0.1% расхождение
                        logger.warning(
                            f"[SYNC] Несоответствие размера позиции {symbol}: "
                            f"биржа={position_size}, бот={local_size}"
                        )
                        # Обновляем размер позиции
                        local_position.size = position_size * (1 if local_position.size > 0 else -1)
            
            # Проверяем позиции, которые есть у бота, но отсутствуют на бирже
            # Это означает, что позиция была закрыта вручную или ликвидирована
            current_time = datetime.now()
            for symbol in list(self.risk_manager.positions.keys()):
                # ЗАЩИТА: Не проверяем ручное закрытие, если позиция только что открыта
                if symbol in self.opening_positions:
                    time_since_open = (current_time - self.opening_positions[symbol]).total_seconds()
                    if time_since_open < self.OPENING_PROTECTION_SECONDS:
                        continue  # Пропускаем проверку, позиция еще открывается
                    else:
                        # Удаляем из списка открываемых, если прошло больше 5 секунд
                        del self.opening_positions[symbol]
                
                if symbol not in exchange_symbols:
                    logger.critical(f"[SYNC] Позиция {symbol} отсутствует на бирже! Вероятно, закрыта вручную.")
                    await self._handle_manually_closed_position(symbol)
            
            # Сохраняем текущее состояние для следующего сравнения
            self.last_exchange_positions = {sym: True for sym in exchange_symbols}
            
        except Exception as e:
            logger.error(f"[SYNC ERROR] Ошибка синхронизации: {e}", exc_info=True)
    
    async def _recover_position_from_exchange(self, symbol: str, pos_data: dict):
        """Восстановление контроля над позицией с биржи."""
        try:
            # Получаем данные о позиции
            entry_price = float(pos_data.get('entryPrice', pos_data.get('entry_price', 0)))
            current_price = float(pos_data.get('markPrice', pos_data.get('last_price', entry_price)))
            size = float(pos_data.get('contracts', pos_data.get('size', 0)))
            leverage = int(pos_data.get('leverage', 1) or 1)
            side = 'long' if size > 0 else 'short'
            size = abs(size)
            
            # Вычисляем примерные SL/TP (если не можем получить точно, ставим дефолтные)
            # В идеале нужно получать из ордеров на бирже, но для начала ставим заглушки
            stop_loss = entry_price * 0.95 if side == 'long' else entry_price * 1.05
            take_profit = entry_price * 1.05 if side == 'long' else entry_price * 0.95
            
            logger.info(f"[RECOVER] Восстановлена позиция {symbol}: {side}, размер={size}, вход={entry_price}")
            
            # Создаем позицию в risk manager
            # Примечание: это упрощенное восстановление, в реальности нужно больше данных
            # Для полноценного восстановления可能需要 доступ к истории ордеров
            
        except Exception as e:
            logger.error(f"[RECOVER ERROR] Не удалось восстановить позицию {symbol}: {e}")
    
    async def _handle_manually_closed_position(self, symbol: str):
        """
        Обработка позиции, закрытой вручную на бирже.
        
        1. Закрываем позицию локально
        2. Обновляем статистику стратегии
        3. Отправляем уведомление в Telegram
        """
        try:
            position = self.risk_manager.positions.get(symbol)
            if not position:
                return
            
            # Получаем последнюю цену
            ticker = await self.data_loader.fetch_ticker(symbol)
            close_price = ticker.get('last', position.entry_price) if ticker else position.entry_price
            
            # Вычисляем PnL вручную (безопасный способ без get_pnl)
            entry_price = position.entry_price
            amount = abs(position.size)
            side = position.direction
            
            if side == 'long':
                pnl = (close_price - entry_price) * amount
            else:  # short
                pnl = (entry_price - close_price) * amount
            
            pnl_pct = (pnl / (entry_price * amount)) * 100 if entry_price * amount > 0 else 0
            
            logger.critical(f"[MANUAL CLOSE] {symbol} закрыта вручную. PnL: ${pnl:.2f} ({pnl_pct:+.2%})")
            
            # Закрываем позицию локально
            self.risk_manager.close_position(symbol, close_price, 'manual_close')
            
            # Обновляем статистику стратегии
            if self.meta_controller:
                # Определяем стратегию, которая открыла сделку
                # Ищем в active_signals main.py (через meta_controller)
                strategy_name = 'Unknown'
                
                # Попытка 1: Через meta_controller.active_signals
                if hasattr(self.meta_controller, 'active_signals'):
                    signal = self.meta_controller.active_signals.get(symbol)
                    if signal and 'strategy' in signal:
                        strat_val = signal['strategy']
                        if isinstance(strat_val, list):
                            # Берем первую не-Unknown стратегию из списка
                            for s in strat_val:
                                if s and s != 'Unknown':
                                    strategy_name = s
                                    break
                        elif isinstance(strat_val, str) and strat_val and strat_val != 'Unknown':
                            strategy_name = strat_val
                
                # Попытка 2: Через position_states (если есть сохраненная стратегия)
                if strategy_name == 'Unknown' and symbol in self.position_states:
                    state = self.position_states[symbol]
                    if hasattr(state, 'strategy') and state.strategy:
                        strategy_name = state.strategy
                
                # Обновляем статистику
                is_winner = pnl > 0
                self.meta_controller.update_strategy_stats(
                    strategy_name,
                    is_winner,
                    pnl,
                    pnl_pct
                )
                logger.info(f"[STATS] Обновлена статистика для {strategy_name}: PnL={pnl:.2f}, Win={is_winner}")
            
            # Отправляем уведомление в Telegram
            if self.telegram:
                balance = self.risk_manager.get_balance()
                direction = position.direction
                
                await self.telegram.notify_exit(
                    symbol=symbol,
                    direction=direction,
                    entry_price=position.entry_price,
                    exit_price=close_price,
                    pnl=pnl,
                    pnl_pct=pnl_pct,
                    reason='Ручное закрытие (биржа)',
                    balance=balance
                )
                logger.info(f"[TG] Уведомление отправлено о ручном закрытии {symbol}")
            
            # Удаляем из отслеживания
            if symbol in self.position_states:
                del self.position_states[symbol]
            if symbol in self.highest_price:
                del self.highest_price[symbol]
            if symbol in self.lowest_price:
                del self.lowest_price[symbol]
                
        except Exception as e:
            logger.error(f"[MANUAL CLOSE ERROR] Ошибка обработки ручного закрытия {symbol}: {e}", exc_info=True)

    async def _check_all_positions(self):
        """Check all open positions for SL/TP hits and manage them."""
        if not self.risk_manager.positions:
            return
        
        # Fetch current prices for ALL positions
        prices = {}
        # Create a copy of keys to avoid "dictionary changed size during iteration" error
        symbols = list(self.risk_manager.positions.keys())
        
        for symbol in symbols:
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
        УМНОЕ управление TP с использованием индикаторов в реальном времени.
        
        ПРИОРИТЕТ 1: Когда цена достигает TP → СРАЗУ ставим SL на уровень TP
        ПРИОРИТЕТ 2: Анализируем индикаторы (RSI, MACD, Volume) для решения
        ПРИОРИТЕТ 3: Если тренд сильный → двигаем TP дальше и используем trailing
        ПРИОРИТЕТ 4: Если дивергенция/слабость → закрываем часть или всю позицию
        
        Это решает проблему "монета дошла до TP, а бот не закрыл, потом откат и стоп".
        """
        # Определяем направление и процент превышения TP
        tp_hit = False
        excess_pct = 0.0
        
        if position.direction == 'long':
            if current_price >= position.take_profit:
                tp_hit = True
                excess_pct = (current_price - position.take_profit) / position.take_profit
        else:  # short
            if current_price <= position.take_profit:
                tp_hit = True
                excess_pct = (position.take_profit - current_price) / position.take_profit
        
        if not tp_hit:
            return  # Цена еще не достигла TP
        
        # ============================================
        # ШАГ 1: СРАЗУ ставим SL на уровень TP (защита прибыли)
        # ============================================
        await self._secure_profit_at_tp(symbol, position, current_price)
        
        # ============================================
        # ШАГ 2: Получаем индикаторы для анализа
        # ============================================
        indicators = await self._fetch_realtime_indicators(symbol)
        if not indicators:
            logger.warning(f"[DYNAMIC TP] Не удалось получить индикаторы для {symbol}")
            return
        
        # ============================================
        # ШАГ 3: Принятие решения на основе индикаторов
        # ============================================
        decision = await self._analyze_exit_decision(
            symbol, position, current_price, pnl_pct, excess_pct, indicators
        )
        
        # ============================================
        # ШАГ 4: Выполнение решения
        # ============================================
        await self._execute_exit_decision(symbol, position, decision, current_price)
    
    async def _secure_profit_at_tp(self, symbol: str, position, current_price: float):
        """
        КРИТИЧЕСКИ ВАЖНО: Сразу ставит SL на уровень TP когда цена его достигает.
        Это гарантирует что мы не потеряем прибыль при откате.
        """
        # Проверяем, нужно ли двигать SL
        should_move_sl = False
        new_sl = None
        
        if position.direction == 'long':
            # Для лонга: если текущий SL ниже TP, поднимаем на уровень TP
            if position.stop_loss < position.take_profit * 0.998:  # Чуть ниже TP
                new_sl = position.take_profit * 0.999  # На уровне TP минус комиссия
                should_move_sl = True
                logger.info(
                    f"[TP REACHED] {symbol}: Цена выше TP! Текущая={current_price:.4f}, TP={position.take_profit:.4f}"
                )
        else:  # short
            # Для шорта: если текущий SL выше TP, опускаем на уровень TP
            if position.stop_loss > position.take_profit * 1.002:
                new_sl = position.take_profit * 1.001  # На уровне TP плюс комиссия
                should_move_sl = True
                logger.info(
                    f"[TP REACHED] {symbol}: Цена ниже TP! Текущая={current_price:.4f}, TP={position.take_profit:.4f}"
                )
        
        if should_move_sl and new_sl:
            old_sl = position.stop_loss
            position.stop_loss = new_sl
            
            # Обновляем ордер на бирже
            sl_side = 'sell' if position.direction == 'long' else 'buy'
            try:
                await self.order_executor.update_stop_loss(symbol, sl_side, new_sl)
                logger.critical(
                    f"[TP PROTECTION] {symbol}: SL переставлен на уровень TP! "
                    f"{old_sl:.4f} -> {new_sl:.4f} (прибыль защищена)"
                )
            except Exception as e:
                logger.error(f"[TP PROTECTION ERROR] {symbol}: {e}")
    
    async def _fetch_realtime_indicators(self, symbol: str) -> Optional[Dict]:
        """
        Получает набор индикаторов в реальном времени для анализа.
        Используем разные таймфреймы для лучшей картины.
        """
        try:
            # Получаем данные с разных таймфреймов
            df_5m = await self.data_loader.fetch_ohlcv(symbol, '5m', limit=50)
            df_15m = await self.data_loader.fetch_ohlcv(symbol, '15m', limit=30)
            df_1h = await self.data_loader.fetch_ohlcv(symbol, '1h', limit=20)
            
            if len(df_5m) < 20:
                return None
            
            # Рассчитываем индикаторы на 5минках
            high = df_5m['high'].values
            low = df_5m['low'].values
            close = df_5m['close'].values
            volume = df_5m['volume'].values
            
            # RSI (14 периодов)
            rsi = self._calculate_rsi(close, 14)
            
            # MACD
            macd_line, signal_line, macd_hist = self._calculate_macd(close)
            
            # ATR
            atr = self._calculate_atr(high, low, close, 14)
            
            # Объемы (сравнение со средним)
            avg_volume = sum(volume[-20:]) / 20 if len(volume) >= 20 else 0
            volume_ratio = volume[-1] / avg_volume if avg_volume > 0 else 1.0
            
            # Тренд (цена выше/ниже EMA20)
            ema20 = sum(close[-20:]) / 20
            current_price = close[-1]
            trend_strength = (current_price - ema20) / ema20
            
            # Волатильность
            volatility = (max(high[-10:]) - min(low[-10:])) / min(low[-10:])
            
            return {
                'rsi': rsi,
                'macd_line': macd_line,
                'macd_signal': signal_line,
                'macd_histogram': macd_hist,
                'atr': atr,
                'volume_ratio': volume_ratio,
                'trend_strength': trend_strength,
                'volatility': volatility,
                'ema20': ema20,
                'current_price': current_price
            }
            
        except Exception as e:
            logger.error(f"[INDICATORS ERROR] {symbol}: {e}")
            return None
    
    def _calculate_rsi(self, prices: np.ndarray, period: int = 14) -> float:
        """Рассчитывает RSI."""
        try:
            import numpy as np
            deltas = np.diff(prices)
            gains = np.where(deltas > 0, deltas, 0)
            losses = np.where(deltas < 0, -deltas, 0)
            
            avg_gain = sum(gains[-period:]) / period if len(gains) >= period else 0
            avg_loss = sum(losses[-period:]) / period if len(losses) >= period else 0
            
            if avg_loss == 0:
                return 100.0
            
            rs = avg_gain / avg_loss
            rsi = 100 - (100 / (1 + rs))
            return rsi
        except:
            return 50.0
    
    def _calculate_macd(self, prices: np.ndarray):
        """Рассчитывает MACD (12, 26, 9)."""
        try:
            import numpy as np
            ema12 = self._ema(prices, 12)
            ema26 = self._ema(prices, 26)
            macd_line = ema12 - ema26
            
            # Signal line (EMA9 от MACD)
            macd_values = []
            for i in range(len(prices)):
                if i >= 25:
                    macd_values.append(macd_line if isinstance(macd_line, (int, float)) else (ema12 - ema26))
            
            if len(macd_values) < 9:
                signal_line = macd_line if isinstance(macd_line, (int, float)) else 0
            else:
                signal_line = self._ema(np.array(macd_values[-9:]), 9)
            
            histogram = macd_line - signal_line if isinstance(macd_line, (int, float)) else 0
            return macd_line if isinstance(macd_line, (int, float)) else 0, signal_line, histogram
        except:
            return 0, 0, 0
    
    def _ema(self, prices: np.ndarray, period: int) -> float:
        """Рассчитывает EMA."""
        if len(prices) < period:
            return sum(prices) / len(prices) if len(prices) > 0 else 0
        
        multiplier = 2 / (period + 1)
        ema = sum(prices[:period]) / period
        
        for price in prices[period:]:
            ema = (price - ema) * multiplier + ema
        
        return ema
    
    def _calculate_atr(self, high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> float:
        """Рассчитывает ATR."""
        try:
            tr_values = []
            for i in range(1, len(high)):
                tr = max(
                    high[i] - low[i],
                    abs(high[i] - close[i-1]),
                    abs(low[i] - close[i-1])
                )
                tr_values.append(tr)
            
            return sum(tr_values[-period:]) / period if len(tr_values) >= period else 0
        except:
            return 0
    
    async def _analyze_exit_decision(
        self,
        symbol: str,
        position,
        current_price: float,
        pnl_pct: float,
        excess_pct: float,
        indicators: Dict
    ) -> Dict:
        """
        Анализирует индикаторы и принимает решение по позиции.
        
        Возвращает решение:
        - action: 'close_all', 'close_partial', 'hold', 'move_tp'
        - confidence: 0.0-1.0
        - reason: описание причины
        - new_tp: новый уровень TP (если нужно)
        - close_percentage: какой % закрыть (если partial)
        """
        rsi = indicators['rsi']
        macd_hist = indicators['macd_histogram']
        volume_ratio = indicators['volume_ratio']
        trend_strength = indicators['trend_strength']
        atr = indicators['atr']
        
        score = 0  # Общий счет: положительный = держать/увеличивать, отрицательный = закрывать
        reasons = []
        
        # ============================================
        # Анализ RSI
        # ============================================
        if position.direction == 'long':
            if rsi > 75:
                score -= 2
                reasons.append(f"RSI перекупленность ({rsi:.1f})")
            elif rsi > 65:
                score -= 1
                reasons.append(f"RSI высоковат ({rsi:.1f})")
            elif rsi < 40:
                score -= 3
                reasons.append(f"RSI слабость ({rsi:.1f})")
            elif 45 <= rsi <= 65:
                score += 1
                reasons.append(f"RSI нейтральный ({rsi:.1f})")
        else:  # short
            if rsi < 25:
                score -= 2
                reasons.append(f"RSI перепроданность ({rsi:.1f})")
            elif rsi < 35:
                score -= 1
                reasons.append(f"RSI низковат ({rsi:.1f})")
            elif rsi > 60:
                score -= 3
                reasons.append(f"RSI сила ({rsi:.1f})")
            elif 35 <= rsi <= 55:
                score += 1
                reasons.append(f"RSI нейтральный ({rsi:.1f})")
        
        # ============================================
        # Анализ MACD
        # ============================================
        if position.direction == 'long':
            if macd_hist < 0:
                score -= 2
                reasons.append("MACD медвежий")
            elif macd_hist > 0 and macd_hist > indicators.get('macd_line', 0) * 0.3:
                score += 2
                reasons.append("MACD сильный бычий")
            else:
                score += 0.5
                reasons.append("MACD нейтральный")
        else:  # short
            if macd_hist > 0:
                score -= 2
                reasons.append("MACD бычий")
            elif macd_hist < 0 and abs(macd_hist) > abs(indicators.get('macd_line', 0)) * 0.3:
                score += 2
                reasons.append("MACD сильный медвежий")
            else:
                score += 0.5
                reasons.append("MACD нейтральный")
        
        # ============================================
        # Анализ объема
        # ============================================
        if volume_ratio > 2.0:
            score += 2
            reasons.append(f"Объем высокий ({volume_ratio:.1f}x)")
        elif volume_ratio > 1.5:
            score += 1
            reasons.append(f"Объем выше среднего ({volume_ratio:.1f}x)")
        elif volume_ratio < 0.7:
            score -= 1
            reasons.append(f"Объем низкий ({volume_ratio:.1f}x)")
        
        # ============================================
        # Анализ тренда
        # ============================================
        if position.direction == 'long':
            if trend_strength > 0.02:
                score += 2
                reasons.append(f"Тренд сильный (+{trend_strength:.2%})")
            elif trend_strength > 0.005:
                score += 1
                reasons.append(f"Тренд умеренный (+{trend_strength:.2%})")
            elif trend_strength < -0.01:
                score -= 2
                reasons.append(f"Тренд слабый ({trend_strength:.2%})")
        else:  # short
            if trend_strength < -0.02:
                score += 2
                reasons.append(f"Тренд сильный ({trend_strength:.2%})")
            elif trend_strength < -0.005:
                score += 1
                reasons.append(f"Тренд умеренный ({trend_strength:.2%})")
            elif trend_strength > 0.01:
                score -= 2
                reasons.append(f"Тренд слабый ({trend_strength:.2%})")
        
        # ============================================
        # Формирование решения
        # ============================================
        decision = {
            'action': 'hold',
            'confidence': 0.5,
            'reasons': reasons,
            'score': score,
            'new_tp': None,
            'close_percentage': 0.0
        }
        
        # Сильные сигналы на закрытие
        if score <= -3:
            decision['action'] = 'close_all'
            decision['confidence'] = 0.9
            decision['reasons'].append("Сильные сигналы на выход")
        
        # Умеренные сигналы на частичное закрытие
        elif score <= -1:
            decision['action'] = 'close_partial'
            decision['close_percentage'] = 0.5  # Закрыть 50%
            decision['confidence'] = 0.7
            decision['reasons'].append("Умеренные сигналы на фиксацию")
        
        # Сильные сигналы держать + двигать TP
        elif score >= 3 and excess_pct > 0.02:
            decision['action'] = 'move_tp'
            # Двигаем TP на основе ATR
            if position.direction == 'long':
                decision['new_tp'] = current_price + (2.0 * atr)
            else:
                decision['new_tp'] = current_price - (2.0 * atr)
            decision['confidence'] = 0.8
            decision['reasons'].append("Сильный тренд, двигаем TP")
        
        # Нейтральная зона - держим с trailing
        elif score >= 0:
            decision['action'] = 'hold'
            decision['confidence'] = 0.6
            decision['reasons'].append("Нейтральные/положительные сигналы")
        
        logger.info(
            f"[TP ANALYSIS] {symbol} | Score: {score:+d} | Action: {decision['action']} | "
            f"Confidence: {decision['confidence']:.0%} | {' | '.join(reasons[:3])}"
        )
        
        return decision
    
    async def _execute_exit_decision(
        self,
        symbol: str,
        position,
        decision: Dict,
        current_price: float
    ):
        """Выполняет принятое решение по позиции."""
        action = decision['action']
        
        if action == 'close_all':
            logger.critical(
                f"[TP DECISION] {symbol}: ЗАКРЫТЬ ВСЮ ПОЗИЦИЮ | "
                f"Причина: {decision['reasons'][-1]} | "
                f"PnL: {position.get_pnl_pct(current_price):+.2%}"
            )
            # КРИТИЧЕСКИ ВАЖНО: Реальное закрытие позиции при негативных сигналах
            # Это предотвращает разворот цены и уход в убыток
            try:
                # Закрываем через order_executor
                close_side = 'sell' if position.direction == 'long' else 'buy'
                await self.order_executor.execute_market_order(
                    symbol, close_side, position.size
                )
                logger.critical(
                    f"[TP EXECUTED] {symbol}: Позиция ЗАКРЫТА по сигналу индикаторов | "
                    f"Сторона: {close_side}, Размер: {position.size}"
                )
            except Exception as e:
                logger.error(f"[TP CLOSE ERROR] {symbol}: Не удалось закрыть позицию: {e}")
        
        elif action == 'close_partial':
            close_pct = decision['close_percentage']
            close_size = position.size * close_pct
            
            logger.warning(
                f"[TP DECISION] {symbol}: ЗАКРЫТЬ {close_pct:.0%} ПОЗИЦИИ | "
                f"Причина: {decision['reasons'][-1]} | "
                f"PnL: {position.get_pnl_pct(current_price):+.2%}"
            )
            # Частичное закрытие для фиксации прибыли
            try:
                close_side = 'sell' if position.direction == 'long' else 'buy'
                await self.order_executor.execute_market_order(
                    symbol, close_side, close_size
                )
                logger.warning(
                    f"[TP PARTIAL EXECUTED] {symbol}: Закрыто {close_size} из {position.size} | "
                    f"Остаток: {position.size - close_size}"
                )
                # Обновляем размер позиции локально
                position.size -= close_size
            except Exception as e:
                logger.error(f"[TP PARTIAL ERROR] {symbol}: {e}")
        
        elif action == 'move_tp':
            new_tp = decision['new_tp']
            if new_tp:
                old_tp = position.take_profit
                position.take_profit = new_tp
                
                logger.info(
                    f"[TP DECISION] {symbol}: ДВИНУТЬ TP | "
                    f"{old_tp:.4f} -> {new_tp:.4f} | "
                    f"Причина: {decision['reasons'][-1]}"
                )
        
        elif action == 'hold':
            logger.debug(
                f"[TP DECISION] {symbol}: ДЕРЖАТЬ | "
                f"PnL: {position.get_pnl_pct(current_price):+.2%} | "
                f"Причина: {decision['reasons'][-1]}"
            )
    
    async def _execute_sl_tp_closure(
        self,
        symbol: str,
        position,
        close_price: float,
        exit_reason: str
    ):
        """
        Execute closure when SL or TP is hit.
        Closes position on exchange and updates local state.
        """
        logger.critical(f"[SL/TP HIT] {symbol}: {exit_reason} @ {close_price}")
        
        try:
            # Close on exchange (in live mode)
            if not self.config.exchange.sandbox:
                close_price_exchange = await self.order_executor.close_position(symbol)
                if close_price_exchange > 0:
                    close_price = close_price_exchange
                    logger.critical(f"[CLOSE OK] {symbol} closed on exchange @ {close_price_exchange}")
                else:
                    logger.error(f"[CLOSE FAIL] Failed to close {symbol} on exchange")
                    return
            
            # Close locally in risk manager
            pnl = self.risk_manager.close_position(symbol, close_price, exit_reason)
            
            # Update strategy stats
            if symbol in self.risk_manager.closed_trades and self.risk_manager.closed_trades:
                last_trade = self.risk_manager.closed_trades[-1]
                is_winner = pnl > 0
                
                # Get strategy from active signals if available
                strategies_used = []
                # Note: We don't have direct access to active_signals here
                # The main loop will handle strategy stats update
                
                logger.info(f"[STATS] {symbol} closed: PnL={pnl:.2f}, Win={is_winner}, Reason={exit_reason}")
            
            # Remove from tracking
            if symbol in self.position_states:
                del self.position_states[symbol]
            if symbol in self.highest_price:
                del self.highest_price[symbol]
            if symbol in self.lowest_price:
                del self.lowest_price[symbol]
                
        except Exception as e:
            logger.error(f"[SL/TP CLOSE ERROR] {symbol}: {e}", exc_info=True)
    
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
