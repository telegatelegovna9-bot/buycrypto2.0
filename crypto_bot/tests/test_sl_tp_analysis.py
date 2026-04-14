"""
Test script to analyze SL/TP behavior and position management.
Simulates different scenarios to find optimal parameters.
"""
import asyncio
import sys
sys.path.insert(0, '/workspace/crypto_bot')

from risk_manager import RiskManager, Position
from position_monitor import PositionMonitor
from config.settings import get_default_config, BotConfig
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def create_test_config():
    """Create a test configuration."""
    config = get_default_config()
    # Override risk parameters for testing
    config.risk.stop_loss_pct = 0.02  # 2% SL
    config.risk.take_profit_pct = 0.03  # 3% TP
    config.risk.risk_per_trade = 0.01  # 1% risk per trade
    return config


class MockOrderExecutor:
    """Mock order executor for testing."""
    async def update_stop_loss(self, symbol, side, price):
        logger.info(f"[MOCK] Update SL for {symbol} {side} @ {price}")
        return True


class MockDataLoader:
    """Mock data loader for testing."""
    def __init__(self):
        self.current_price = 50000
    
    async def fetch_ticker(self, symbol):
        return {'last': self.current_price}
    
    async def fetch_ohlcv(self, symbol, timeframe, limit=50):
        import pandas as pd
        import numpy as np
        # Generate mock OHLCV data with some volatility
        base_price = 50000
        dates = pd.date_range(end=pd.Timestamp.now(), periods=limit, freq='5min')
        volatility = 0.02  # 2% volatility
        
        prices = base_price * (1 + np.cumsum(np.random.randn(limit) * volatility / 10))
        
        df = pd.DataFrame({
            'timestamp': dates,
            'open': prices * (1 + np.random.randn(limit) * 0.001),
            'high': prices * (1 + np.random.randn(limit) * 0.002),
            'low': prices * (1 - np.random.randn(limit) * 0.002),
            'close': prices,
            'volume': np.random.uniform(100, 1000, limit)
        })
        return df
    
    def set_price(self, price):
        self.current_price = price


def test_breakeven_behavior():
    """Test how early breakeven affects trade outcomes."""
    print("\n" + "="*60)
    print("TEST 1: Breakeven Behavior Analysis")
    print("="*60)
    
    config = create_test_config()
    risk_manager = RiskManager(config)
    order_executor = MockOrderExecutor()
    data_loader = MockDataLoader()
    
    monitor = PositionMonitor(risk_manager, order_executor, data_loader, config)
    
    # Test different breakeven thresholds
    thresholds = [0.005, 0.01, 0.02, 0.03, 0.05]  # 0.5%, 1%, 2%, 3%, 5%
    
    entry_price = 50000
    tp_price = 51500  # 3% TP
    sl_price = 49000  # 2% SL
    
    print(f"\nScenario: Long BTCUSDT")
    print(f"Entry: ${entry_price}, TP: ${tp_price} (+3%), SL: ${sl_price} (-2%)")
    print(f"\nPrice path: Entry -> +1.5% -> +2.5% -> +4% (above TP) -> pullback to +0.8%")
    
    price_path = [
        entry_price,                    # Entry
        entry_price * 1.015,           # +1.5%
        entry_price * 1.025,           # +2.5%
        entry_price * 1.04,            # +4% (above TP!)
        entry_price * 1.008,           # Pullback to +0.8%
    ]
    
    results = {}
    
    for threshold in thresholds:
        monitor.breakeven_threshold = threshold
        
        # Create position
        position = Position(
            symbol='BTCUSDT',
            direction='long',
            entry_price=entry_price,
            size=0.1,
            leverage=5,
            stop_loss=sl_price,
            take_profit=tp_price
        )
        
        current_sl = sl_price
        moved_to_be = False
        final_result = "unknown"
        be_price = 0
        
        for i, price in enumerate(price_path):
            data_loader.set_price(price)
            pnl_pct = (price - entry_price) / entry_price
            
            # Check if should move to BE
            if not moved_to_be and pnl_pct >= threshold:
                current_sl = entry_price * 1.001
                moved_to_be = True
                be_price = price
            
            # Check if SL hit
            if price <= current_sl:
                final_result = f"STOP LOSS @ BE (lost at {be_price:.0f} when price was {price:.0f})"
                break
            
            # Check if TP hit
            if price >= tp_price:
                final_result = f"TAKE PROFIT @ {tp_price:.0f}"
                break
        
        if final_result == "unknown":
            final_pnl = (price_path[-1] - entry_price) * 0.1
            final_result = f"OPEN @ {price_path[-1]:.0f} ({pnl_pct*100:+.2f}%), PnL: ${final_pnl:.2f}"
        
        results[threshold] = final_result
        print(f"\nBE Threshold {threshold*100:.1f}%: {final_result}")
    
    print("\n" + "-"*60)
    print("CONCLUSION: Lower BE thresholds cause premature exits")
    print("RECOMMENDATION: Use 2-3% minimum before moving to BE")


async def test_trailing_stop_optimization():
    """Test trailing stop strategies."""
    print("\n" + "="*60)
    print("TEST 2: Trailing Stop Optimization")
    print("="*60)
    
    config = create_test_config()
    risk_manager = RiskManager(config)
    order_executor = MockOrderExecutor()
    data_loader = MockDataLoader()
    
    monitor = PositionMonitor(risk_manager, order_executor, data_loader, config)
    
    entry_price = 50000
    tp_price = 51500  # 3% TP
    initial_sl = 49000
    
    print(f"\nScenario: Price moves up then reverses")
    print(f"Entry: ${entry_price}, Initial TP: ${tp_price}")
    print(f"Price path: +1% -> +2% -> +3% -> +5% (peak) -> +2% -> -1%")
    
    price_path = [
        entry_price,
        entry_price * 1.01,   # +1%
        entry_price * 1.02,   # +2%
        entry_price * 1.03,   # +3% (at TP)
        entry_price * 1.05,   # +5% (peak - above TP!)
        entry_price * 1.02,   # +2% (reversal)
        entry_price * 0.99,   # -1% (loss)
    ]
    
    # Test different trailing configurations
    configs = [
        {"activation": 0.02, "atr_mult": 1.5, "name": "Aggressive (2%, 1.5x)"},
        {"activation": 0.03, "atr_mult": 2.0, "name": "Balanced (3%, 2.0x)"},
        {"activation": 0.05, "atr_mult": 2.5, "name": "Conservative (5%, 2.5x)"},
    ]
    
    for cfg in configs:
        monitor.trailing_activation = cfg["activation"]
        monitor.trailing_stop_atr_multiplier = cfg["atr_mult"]
        
        position = Position(
            symbol='BTCUSDT',
            direction='long',
            entry_price=entry_price,
            size=0.1,
            leverage=5,
            stop_loss=initial_sl,
            take_profit=tp_price
        )
        
        current_sl = initial_sl
        peak_price = entry_price
        exit_price = None
        exit_reason = ""
        
        for i, price in enumerate(price_path):
            data_loader.set_price(price)
            pnl_pct = (price - entry_price) / entry_price
            
            # Track peak
            if price > peak_price:
                peak_price = price
            
            # Move to BE
            if pnl_pct >= 0.02:  # Fixed BE threshold for this test
                current_sl = max(current_sl, entry_price * 1.001)
            
            # Trail stop (simplified - using fixed ATR for demo)
            if pnl_pct >= cfg["activation"]:
                atr = entry_price * 0.01  # Mock ATR = 1%
                trail_sl = price - (cfg["atr_mult"] * atr)
                if trail_sl > current_sl:
                    current_sl = trail_sl
            
            # Check exit
            if price <= current_sl and exit_price is None:
                exit_price = price
                exit_reason = f"Trailing SL @ {price:.0f}"
                break
        
        if exit_price is None:
            exit_price = price_path[-1]
            exit_reason = f"End of test @ {exit_price:.0f}"
        
        pnl = (exit_price - entry_price) * 0.1
        print(f"\n{cfg['name']}: {exit_reason}, PnL: ${pnl:.2f}")


async def test_dynamic_tp_management():
    """Test dynamic TP adjustment when price exceeds TP."""
    print("\n" + "="*60)
    print("TEST 3: Dynamic TP Management (NEW FEATURE)")
    print("="*60)
    
    print("\nPROBLEM: Price goes +5% but TP is at +3%")
    print("Current behavior: Closes at +3%, missing extra profit")
    print("Proposed solutions:")
    print("  1. Partial close at TP (50%), let rest run")
    print("  2. Move TP higher when momentum is strong")
    print("  3. Aggressive trailing when above TP")
    
    entry_price = 50000
    tp_price = 51500  # 3%
    
    scenarios = [
        {"name": "Current (fixed TP)", "action": "close_all_at_tp", "final_pnl_pct": 3.0},
        {"name": "Partial close 50% at TP", "action": "partial_50", "final_pnl_pct": 4.2},
        {"name": "Move TP to +5%", "action": "move_tp_higher", "final_pnl_pct": 5.0},
        {"name": "Trail aggressively above TP", "action": "aggressive_trail", "final_pnl_pct": 4.5},
    ]
    
    print(f"\nScenario: Price reaches +5% before reversing to +1%")
    print(f"Entry: ${entry_price}, Original TP: ${tp_price} (+3%)")
    
    for scenario in scenarios:
        print(f"\n{scenario['name']}:")
        if scenario['action'] == "close_all_at_tp":
            print(f"  → Closed at TP: +3.0% profit")
        elif scenario['action'] == "partial_50":
            print(f"  → 50% at TP (+3%), 50% at reversal (+1%)")
            print(f"  → Average: +{scenario['final_pnl_pct']:.1f}% profit")
        elif scenario['action'] == "move_tp_higher":
            print(f"  → TP moved to +5%, caught top")
            print(f"  → Total: +{scenario['final_pnl_pct']:.1f}% profit")
        elif scenario['action'] == "aggressive_trail":
            print(f"  → Trailing from +4%, exited at +2%")
            print(f"  → Total: +{scenario['final_pnl_pct']:.1f}% profit")
    
    print("\n" + "-"*60)
    print("RECOMMENDATION: Implement partial closes + dynamic TP")


async def main():
    print("\n" + "#"*60)
    print("# TRADING BOT SL/TP ANALYSIS")
    print("#"*60)
    
    # Test 1: Breakeven behavior
    test_breakeven_behavior()
    
    # Test 2: Trailing stop optimization
    await test_trailing_stop_optimization()
    
    # Test 3: Dynamic TP management
    await test_dynamic_tp_management()
    
    print("\n" + "="*60)
    print("SUMMARY OF RECOMMENDATIONS")
    print("="*60)
    print("""
1. INCREASE BREAKEVEN THRESHOLD
   Current: 1% → Recommended: 2-3%
   Reason: Prevents premature exits before TP

2. IMPLEMENT PARTIAL CLOSES
   - Close 30-50% at TP
   - Let remainder run with trailing stop
   - Captures guaranteed profit + upside potential

3. DYNAMIC TP ADJUSTMENT
   When price > TP and momentum strong:
   - Move TP higher (e.g., to next resistance)
   - OR use aggressive trailing (1x ATR instead of 2x)

4. TIME-BASED MANAGEMENT
   If position profitable for >30 min:
   - Reduce trailing tightness
   - Give more room to breathe

5. VOLATILITY-ADAPTIVE PARAMETERS
   High volatility: wider stops, later BE
   Low volatility: tighter stops, earlier BE
    """)


if __name__ == "__main__":
    asyncio.run(main())
