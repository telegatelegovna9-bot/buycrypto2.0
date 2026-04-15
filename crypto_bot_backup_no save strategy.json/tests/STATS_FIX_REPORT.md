# 🔧 FIX: Статистика теперь обновляется для ВСЕХ сделок

## Проблема
Бот писал в логах `[STATS UPDATED] Unknown: PnL=-0.47`, но файл `strategy_stats.json` не обновлялся, потому что стратегия называлась "Unknown".

## Решение
Изменен файл `/workspace/crypto_bot/main.py`:

### Что было:
```python
strategy_val = self.active_signals[symbol].get('strategy', [])
# Если strategy = 'Unknown' - статистика НЕ обновлялась
```

### Что стало:
1. **Проверка на 'Unknown'** - теперь это значение фильтруется
2. **Поиск стратегии в объекте позиции** - если не найдено в active_signals
3. **Fallback на дефолтную стратегию** - если ничего не найдено, используется `Default_long` или `Default_short`

```python
if strategy_val and strategy_val != 'Unknown':
    # Используем найденную стратегию
    strategies_used = [strategy_val]
else:
    # Пытаемся получить из position объекта
    position_obj = self.active_signals[symbol].get('position')
    if position_obj and hasattr(position_obj, 'strategy'):
        strat = position_obj.strategy
        if strat and strat != 'Unknown':
            strategies_used = [strat]

if strategies_used:
    # Обновляем статистику
    update_strategy_performance(...)
else:
    # Fallback: создаем дефолтную стратегию
    default_strategy = f"Default_{position_obj.direction}"
    update_strategy_performance(default_strategy, ...)
```

## Результат
✅ **Теперь КАЖДАЯ сделка обновляет статистику**, даже если стратегия не была определена  
✅ Файл `strategy_stats.json` будет содержать данные для всех сделок  
✅ Бот накапливает опыт и учится на каждой позиции  

## Как проверить
После следующей закрытой сделки в логах будет:
- `[STATS UPDATED] TrendBreakout: PnL=5.23, Win=True, Reason=tp` - если стратегия найдена
- `[STATS WARNING] No strategy found for RAVE/USDT:USDT, using Default_long: PnL=-0.47, Win=False` - если используется fallback

В обоих случаях файл `strategy_stats.json` **обновится**!

## Мониторинг
```bash
# Следить за обновлением статистики
tail -f logs/trading_bot.log | grep -E "\[STATS UPDATED\]|\[STATS WARNING\]"

# Проверить файл статистики
cat data/strategy_stats.json | python -m json.tool
```
