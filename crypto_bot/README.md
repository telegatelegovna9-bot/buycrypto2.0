# Crypto Trading Bot

A fully autonomous cryptocurrency trading bot with multiple strategies, adaptive meta-algorithm, and comprehensive risk management.

## 🚀 Features

### Core Capabilities
- **Autonomous Trading**: Self-selects pairs, entry/exit points, position sizes, and leverage
- **Multi-Strategy System**: 4 independent trading strategies working together
- **Meta-Controller**: Dynamically weights strategies based on performance
- **Self-Learning**: Adapts strategy weights based on win rate, profit factor, and consistency
- **Risk Management**: Strict risk controls with configurable parameters
- **Telegram Integration**: Real-time notifications for all trading activity

### Strategies Implemented
1. **TrendBreakout**: EMA crossover + ADX trend strength + breakout detection + market structure analysis
2. **RangeTrading**: RSI mean reversion in sideways markets with support/resistance levels
3. **VolumeStrategy**: Volume spikes, OBV accumulation/distribution, VWAP analysis
4. **OpenInterest**: Funding rate sentiment analysis and OI changes
5. **VolatilityBreakout**: Bollinger Band squeeze + ATR compression + momentum confirmation

### Risk Management
- Configurable risk per trade (default 1%)
- Maximum concurrent positions (default 2)
- Minimum risk-reward ratio (1:2)
- Maximum drawdown protection (15%)
- Dynamic leverage based on confidence and volatility
- Stop-loss, take-profit, trailing stops, and breakeven moves

## 📁 Project Structure

```
crypto_bot/
├── config/
│   └── settings.py          # Configuration classes
├── data/
│   └── data_loader.py       # Market data fetching & analysis
├── strategies/
│   ├── base_strategy.py         # Abstract base class
│   ├── trend_breakout.py        # Trend following strategy
│   ├── range_trading.py         # Mean reversion strategy
│   ├── volume_oi_strategies.py  # Volume & OI strategies
│   └── volatility_breakout.py   # Volatility squeeze breakout
├── backtest/
│   ├── backtest_engine.py   # Backtesting framework
│   └── run_backtest.py      # Backtest runner script
├── utils/
│   └── telegram_notifier.py # Telegram notifications
├── risk_manager.py          # Position sizing & risk controls
├── execution_engine.py      # Order execution
├── meta_controller.py       # Strategy orchestration
├── main.py                  # Main bot entry point
└── requirements.txt         # Dependencies
```

## 🛠️ Installation

1. **Clone and setup environment:**
```bash
cd crypto_bot
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

2. **Configure environment variables:**
```bash
export BINANCE_API_KEY="your_api_key"
export BINANCE_API_SECRET="your_secret"
export TELEGRAM_BOT_TOKEN="your_bot_token"
export TELEGRAM_CHAT_ID="your_chat_id"
```

3. **Edit configuration:**
Modify `config/settings.py` or use environment variables to customize:
- Trading pairs
- Risk parameters
- Strategy settings
- Telegram notifications

## 🚀 Usage

### Live Trading
```bash
python main.py
```

### Run Backtests
```bash
python backtest/run_backtest.py
```

## ⚙️ Configuration

### Risk Settings (`config/settings.py`)
```python
@dataclass
class RiskConfig:
    risk_per_trade: float = 0.01      # 1% risk per trade
    max_positions: int = 2             # Max concurrent positions
    min_rr_ratio: float = 2.0          # Minimum 1:2 risk-reward
    max_drawdown: float = 0.15         # 15% max drawdown
```

### Leverage Settings
```python
@dataclass
class LeverageConfig:
    min_leverage: int = 2
    max_leverage: int = 10
    # Low confidence → 2x, Medium → 4-6x, High → 10x
```

### Strategy Adaptation
The bot automatically adapts every 6 hours:
- Increases weight of profitable strategies
- Decreases weight of underperforming strategies
- Maintains minimum weight (0.2) for diversification

## 📊 Backtesting

The backtest engine includes:
- Realistic commission (0.04%)
- Slippage modeling (0.01%)
- No lookahead bias
- Comprehensive metrics:
  - Total return
  - Win rate
  - Profit factor
  - Max drawdown
  - Sharpe ratio
  - Consecutive wins/losses

## 🔔 Telegram Notifications

Receive real-time alerts for:
- New trade entries (symbol, direction, entry, SL, TP, leverage)
- Trade exits (PnL, reason)
- Stop loss hits
- Take profit hits
- Daily performance reports
- System alerts

## ⚠️ Important Warnings

1. **This is not financial advice** - Use at your own risk
2. **Start with testnet/sandbox** - Test thoroughly before live trading
3. **Monitor regularly** - Even autonomous bots need supervision
4. **Understand the risks** - Crypto futures trading can result in total loss
5. **Never risk more than you can afford to lose**

## 📈 Performance Metrics

Track these key metrics:
- **Win Rate**: Target > 45%
- **Profit Factor**: Target > 1.5
- **Max Drawdown**: Keep < 20%
- **Sharpe Ratio**: Target > 1.0
- **Average RR**: Maintain ≥ 1:2

## 🔧 Extending the Bot

### Adding a New Strategy
```python
from strategies.base_strategy import BaseStrategy, Signal

class MyNewStrategy(BaseStrategy):
    def generate_signal(self, df: pd.DataFrame, market_data: Dict) -> Signal:
        # Your strategy logic here
        return Signal(
            symbol=symbol,
            direction='long',  # or 'short' or 'neutral'
            confidence=0.7,
            entry_price=price,
            stop_loss=sl,
            take_profit=tp,
            strategy_name=self.name
        )
```

Then add it to `MetaController.__init__()`.

## 📝 Logging

All activity is logged to `logs/trading_bot.log`:
- Signal generation
- Trade execution
- Position management
- Errors and warnings

## 🆘 Troubleshooting

### Common Issues

1. **API errors**: Check API keys and network connection
2. **Insufficient data**: Ensure enough historical bars are available
3. **No signals**: Adjust strategy parameters or check market conditions
4. **High drawdown**: Reduce risk_per_trade or max_positions

## 📄 License

MIT License - Use at your own risk.

## 🤝 Contributing

Contributions welcome! Please ensure:
- Code follows existing patterns
- Strategies include proper documentation
- Backtests pass before merging

---

**Remember**: Past performance does not guarantee future results. Always test thoroughly before deploying capital.
