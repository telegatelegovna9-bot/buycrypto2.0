# Исправление сохранения статистики стратегий

## Проблема
Бот не сохранял статистику в `strategy_stats.json` после закрытия сделок, хотя логировал `[STATS UPDATED]`.

## Причины
1. **Потеря имени стратегии**: Сигнал приходил с несколькими стратегиями (`source_strategies`), но при сохранении в `active_signals` использовалось только одно имя или "Unknown"
2. **Неправильное извлечение стратегии**: При закрытии позиции код не мог найти имя стратегии, так как оно терялось между открытием и закрытием
3. **Fallback на "Unknown"**: Когда стратегия не находилась, код использовал "Unknown", который игнорировался при обновлении статистики

## Решение

### 1. main.py - Изменения в получении стратегий из сигнала
```python
# Было:
source_strategy = decision.get('source', 'Unknown')

# Стало:
source_strategies = decision.get('source_strategies', [])
if not source_strategies:
    single_source = decision.get('source', None)
    if single_source and single_source != 'Unknown':
        source_strategies = [single_source]

# Сохраняем ПОЛНЫЙ список стратегий в active_signals
self.active_signals[symbol] = {
    'strategy': source_strategies if len(source_strategies) > 1 else (source_strategies[0] if source_strategies else 'Unknown')
}
```

### 2. main.py - Улучшенное извлечение стратегии при закрытии
```python
# Теперь обрабатываем все варианты:
# - Список стратегий
# - Одиночная строка
# - Извлечение из position объекта
# - Фильтрация 'Unknown' значений

strategies_used = []
if strategy_val:
    if isinstance(strategy_val, list):
        strategies_used = [s for s in strategy_val if s and s != 'Unknown']
    elif isinstance(strategy_val, str) and strategy_val != 'Unknown':
        strategies_used = [strategy_val]

# Если не найдено, пробуем из position объекта
if not strategies_used:
    position_obj = self.active_signals[symbol].get('position')
    if position_obj and hasattr(position_obj, 'strategy'):
        strat = position_obj.strategy
        # ... обработка списка или строки

# Добавлен ERROR лог если стратегия не найдена вообще
else:
    logger.error(f"[STATS ERROR] Cannot determine strategy for {symbol}, stats NOT updated")
```

### 3. meta_controller.py - Гарантированное сохранение
- Добавлен `f.flush()` и `os.fsync()` для записи на диск
- Проверка файла после сохранения `[STATS VERIFY]`
- Атомарная запись через temp файл + `os.replace()`

## Результат
Теперь каждая сделка:
1. ✅ Сохраняет полное имя стратегии(й) при открытии
2. ✅ Корректно извлекает стратегию при закрытии
3. ✅ Обновляет `strategy_stats.json` с правильным именем
4. ✅ Бот использует накопленную статистику для адаптации весов

## Мониторинг
```bash
# Проверка обновления статистики
grep "\[STATS UPDATED\]" logs/trading_bot.log | tail -10

# Проверка сохранения файла
grep "\[STATS SAVED\]" logs/trading_bot.log | tail -5

# Проверка файла
cat data/strategy_stats.json
```
