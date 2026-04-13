"""
Telegram Notification Module.
Sends trading signals, position updates, and performance reports to Telegram.
"""
import asyncio
import logging
from typing import Optional, Dict
from datetime import datetime

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """
    Sends notifications to Telegram about trading activity.
    """
    
    def __init__(self, config):
        self.config = config.telegram
        self.enabled = self.config.enabled
        self.bot_token = self.config.bot_token
        self.chat_id = self.config.chat_id
        
        if not self.enabled or not self.bot_token or not self.chat_id:
            logger.warning("Telegram notifications disabled or not configured")
            self.enabled = False
    
    async def send_message(self, message: str) -> bool:
        """Send a message to Telegram."""
        if not self.enabled:
            return False
        
        try:
            import aiohttp
            
            url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            data = {
                'chat_id': self.chat_id,
                'text': message,
                'parse_mode': 'HTML'
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=data) as response:
                    result = await response.json()
                    
                    if result.get('ok'):
                        logger.info("Telegram message sent successfully")
                        return True
                    else:
                        logger.error(f"Telegram error: {result}")
                        return False
                        
        except Exception as e:
            logger.error(f"Error sending Telegram message: {e}")
            return False
    
    async def notify_entry(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
        leverage: int,
        confidence: float,
        balance: float
    ):
        """Notify about new trade entry."""
        emoji = "🟢" if direction == "long" else "🔴"
        
        message = f"""
{emoji} <b>NEW TRADE OPENED</b> {emoji}

📊 Symbol: <code>{symbol}</code>
📈 Direction: <b>{direction.upper()}</b>
💰 Entry Price: <code>{entry_price:.4f}</code>
🛑 Stop Loss: <code>{stop_loss:.4f}</code>
✅ Take Profit: <code>{take_profit:.4f}</code>
⚡ Leverage: <b>{leverage}x</b>
🎯 Confidence: <b>{confidence:.1%}</b>

💵 Balance: <code>${balance:.2f}</code>
⏰ Time: <code>{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</code>
        """.strip()
        
        await self.send_message(message)
    
    async def notify_exit(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        exit_price: float,
        pnl: float,
        pnl_pct: float,
        reason: str,
        balance: float
    ):
        """Notify about trade exit."""
        pnl_emoji = "✅" if pnl > 0 else "❌"
        direction_emoji = "🟢" if direction == "long" else "🔴"
        
        message = f"""
{pnl_emoji} <b>TRADE CLOSED</b> {pnl_emoji}

📊 Symbol: <code>{symbol}</code>
📈 Direction: <b>{direction.upper()}</b>
💰 Entry: <code>{entry_price:.4f}</code>
💵 Exit: <code>{exit_price:.4f}</code>
💹 PnL: <b>{pnl_emoji} ${pnl:.2f} ({pnl_pct:+.2%})</b>
📝 Reason: <code>{reason}</code>

💵 New Balance: <code>${balance:.2f}</code>
⏰ Time: <code>{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</code>
        """.strip()
        
        await self.send_message(message)
    
    async def notify_stop_loss(self, symbol: str, pnl: float, balance: float):
        """Notify about stop loss hit."""
        message = f"""
⚠️ <b>STOP LOSS HIT</b> ⚠️

📊 Symbol: <code>{symbol}</code>
💹 PnL: <b>❌ ${pnl:.2f}</b>
💵 Balance: <code>${balance:.2f}</code>
⏰ Time: <code>{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</code>
        """.strip()
        
        await self.send_message(message)
    
    async def notify_take_profit(self, symbol: str, pnl: float, balance: float):
        """Notify about take profit hit."""
        message = f"""
🎯 <b>TAKE PROFIT HIT</b> 🎯

📊 Symbol: <code>{symbol}</code>
💹 PnL: <b>✅ ${pnl:.2f}</b>
💵 Balance: <code>${balance:.2f}</code>
⏰ Time: <code>{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</code>
        """.strip()
        
        await self.send_message(message)
    
    async def send_daily_report(
        self,
        total_pnl: float,
        total_trades: int,
        winning_trades: int,
        losing_trades: int,
        current_balance: float,
        peak_balance: float,
        drawdown: float
    ):
        """Send daily performance report."""
        win_rate = (winning_trades / total_trades * 100) if total_trades > 0 else 0
        
        emoji = "📈" if total_pnl > 0 else "📉"
        
        message = f"""
{emoji} <b>DAILY REPORT</b> {emoji}

💵 Balance: <code>${current_balance:.2f}</code>
📊 PnL Today: <b>${total_pnl:+.2f}</b>
📈 Peak Balance: <code>${peak_balance:.2f}</code>
📉 Drawdown: <code>{drawdown:.2%}</code>

📝 Total Trades: <code>{total_trades}</code>
✅ Winners: <code>{winning_trades}</code>
❌ Losers: <code>{losing_trades}</code>
🎯 Win Rate: <code>{win_rate:.1f}%</code>

⏰ {datetime.now().strftime('%Y-%m-%d')}
        """.strip()
        
        await self.send_message(message)
    
    async def send_alert(self, title: str, message_text: str):
        """Send custom alert."""
        message = f"""
⚠️ <b>{title}</b> ⚠️

{message_text}

⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
        """.strip()
        
        await self.send_message(message)
    
    async def send_signal_summary(
        self,
        symbol: str,
        long_confidence: float,
        short_confidence: float,
        decision: str,
        strategies: list
    ):
        """Send signal analysis summary."""
        strategy_text = "\n".join([
            f"  • {s['name']}: {s['direction']} ({s['confidence']:.1%})"
            for s in strategies
        ])
        
        message = f"""
📊 <b>SIGNAL ANALYSIS</b> 📊

📊 Symbol: <code>{symbol}</code>

📈 Long Confidence: <b>{long_confidence:.1%}</b>
📉 Short Confidence: <b>{short_confidence:.1%}</b>

🎯 Decision: <b>{decision.upper()}</b>

Strategies:
{strategy_text}

⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
        """.strip()
        
        await self.send_message(message)
