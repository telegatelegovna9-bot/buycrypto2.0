# 🔐 Настройка API ключей и Telegram

## 📍 Где найти и как вставить ключи

### 1. Binance API Keys

**Для тестовой торговли (рекомендуется сначала):**
1. Перейдите на [Binance Testnet](https://testnet.binancefuture.com/)
2. Залогиньтесь через свой Binance аккаунт
3. Нажмите на иконку профиля → **API Management**
4. Создайте новый API ключ
5. Скопируйте **API Key** и **Secret Key**

**Для реальной торговли:**
1. Перейдите на [Binance.com](https://www.binance.com/)
2. Профиль → **API Management**
3. Создайте новый API ключ
4. Включите разрешения:
   - ✅ Enable Reading
   - ✅ Enable Futures
   - ❌ НЕ включайте Withdrawal (для безопасности)
5. Скопируйте **API Key** и **Secret Key**

---

### 2. Telegram Bot Token

1. Откройте Telegram и найдите [@BotFather](https://t.me/BotFather)
2. Отправьте команду `/newbot`
3. Придумайте имя боту (например: `MyTradingBot`)
4. Придумайте username боту (должен заканчиваться на `bot`, например: `my_crypto_bot`)
5. BotFather пришлет вам **TOKEN** — сохраните его

**Как получить Chat ID:**
1. Найдите своего нового бота в Telegram и нажмите **Start**
2. Перейдите в [@userinfobot](https://t.me/userinfobot) и нажмите **Start**
3. Он пришлёт ваш **Chat ID** (число, например: `123456789`)

---

## 📝 Как вставить ключи в бота

### Способ 1: Через файл `.env` (рекомендуется)

Откройте файл `.env` в папке `crypto_bot/` и замените значения:

```bash
# Binance API credentials
BINANCE_API_KEY=ваш_api_key_от_binance
BINANCE_API_SECRET=ваш_secret_key_от_binance

# Telegram Bot credentials
TELEGRAM_BOT_TOKEN=ваш_token_от_botfather
TELEGRAM_CHAT_ID=ваш_chat_id

# Режим работы (true = тестовая сеть, false = реальная торговля)
BINANCE_SANDBOX=true
```

### Способ 2: Через переменные окружения

**Linux/Mac:**
```bash
export BINANCE_API_KEY="ваш_api_key"
export BINANCE_API_SECRET="ваш_secret_key"
export TELEGRAM_BOT_TOKEN="ваш_token"
export TELEGRAM_CHAT_ID="ваш_chat_id"
export BINANCE_SANDBOX=true
```

**Windows (PowerShell):**
```powershell
$env:BINANCE_API_KEY="ваш_api_key"
$env:BINANCE_API_SECRET="ваш_secret_key"
$env:TELEGRAM_BOT_TOKEN="ваш_token"
$env:TELEGRAM_CHAT_ID="ваш_chat_id"
$env:BINANCE_SANDBOX="true"
```

---

## ✅ Проверка настройки

После настройки запустите бота:

```bash
cd crypto_bot
pip install -r requirements.txt
python main.py
```

Если всё настроено правильно:
- Бот подключится к бирже
- Telegram бот отправит уведомление о запуске
- В логах не будет ошибок аутентификации

---

## ⚠️ Меры безопасности

1. **Никогда не коммитьте `.env` файл в Git!** (он уже в `.gitignore`)
2. Используйте **тестовую сеть** для первых тестов
3. Не давайте API ключу права на **вывод средств**
4. Установите **лимиты на IP** в настройках API Binance
5. Регулярно **пересоздавайте API ключи**

---

## 🎯 Пример заполненного `.env`

```bash
# Binance API credentials
BINANCE_API_KEY=Xh2Kq8FjLmNpRtUvWxYz1234567890AbCdEfGhIjKlMnOpQrStUvWxYz
BINANCE_API_SECRET=AbCdEfGhIjKlMnOpQrStUvWxYz1234567890Xh2Kq8FjLmNpRtUvWxYz

# Telegram Bot credentials
TELEGRAM_BOT_TOKEN=1234567890:ABCdefGHIjklMNOpqrSTUvwxYZ
TELEGRAM_CHAT_ID=987654321

# Режим работы (true = тестовая сеть, false = реальная торговля)
BINANCE_SANDBOX=true
```

---

## 🆘 Troubleshooting

**Ошибка "Invalid API-key":**
- Проверьте, что ключ скопирован без пробелов
- Убедитесь, что API ключ активирован на Binance
- Проверьте, что выбран правильный режим (sandbox/production)

**Telegram не отправляет сообщения:**
- Убедитесь, что вы написали боту `/start`
- Проверьте, что Chat ID числовой
- Проверьте, что токен скопирован полностью

**Бот не торгует:**
- В режиме sandbox убедитесь, что у вас есть тестовые USDT на Futures тестнете
- Проверьте минимальный размер позиции на бирже
