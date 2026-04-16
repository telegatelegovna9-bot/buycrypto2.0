# 🔧 Решение проблемы с подключением к Binance

## Проблема
Ошибка `Could not contact DNS servers` означает, что **Binance заблокирован в вашем регионе** или есть проблемы с DNS.

## ✅ 4 способа решения

### Способ 1: Использовать прокси (рекомендуется)

1. Найдите рабочий прокси (HTTP или SOCKS5)
2. Создайте файл `.env` в папке проекта:
   ```
   BINANCE_API_KEY=ваш_api_key
   BINANCE_API_SECRET=ваш_api_secret
   PROXY_URL=http://username:password@proxy_ip:port
   ```
   Или для SOCKS5:
   ```
   PROXY_URL=socks5h://127.0.0.1:9050
   ```

3. Запустите бота: `python main.py`

### Способ 2: Изменить DNS (Windows)

1. Панель управления → Сеть и Интернет → Центр управления сетями
2. Ваше подключение → Свойства → IP версии 4 (TCP/IPv4) → Свойства
3. Укажите DNS:
   - Предпочитаемый: `8.8.8.8` (Google)
   - Альтернативный: `1.1.1.1` (Cloudflare)
4. Сохраните и перезапустите командную строку

### Способ 3: Использовать VPN
Включите VPN перед запуском бота.

### Способ 4: Использовать другую биржу
В файле `config/settings.py` измените:
```python
exchange_id: str = "bybit"  # или "okx", "kucoin"
```

---

## 🚀 Быстрый старт

### 1. Создать файл .env
Создайте файл `.env` в папке `crypto_bot` с содержимым:
```
BINANCE_API_KEY=your_key_here
BINANCE_API_SECRET=your_secret_here
PROXY_URL=  # оставьте пустым или укажите прокси
TELEGRAM_BOT_TOKEN=  # опционально
TELEGRAM_CHAT_ID=    # опционально
```

### 2. Запустить бэктест (не требует интернета)
```bash
python backtest/run_backtest.py
```

### 3. Запустить live-торговлю
```bash
python main.py
```

---

## 📊 Архитектура бота

- **5 стратегий**: TrendBreakout, RangeTrading, Volume, OI, Volatility
- **Meta-Controller**: Динамическое взвешивание стратегий
- **Risk Management**: 1% риска, макс 2 позиции, SL/TP
- **Dynamic Leverage**: 2x–10x по confidence сигнала
- **Telegram**: Уведомления о сделках

⚠️ **Не торгуйте на реальные деньги без тестирования!**
