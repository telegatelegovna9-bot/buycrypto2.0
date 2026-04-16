"""
Meta Controller for Multi-Strategy Trading Bot.
Aggregates signals from multiple strategies, applies regime filtering,
and makes final trading decisions with adaptive weighting.
"""
import logging
from typing import List, Dict, Optional
import os
import json
from strategies.base_strategy import Signal, BaseStrategy
from strategies.market_regime import MarketRegimeDetector
from strategies.trend_breakout import TrendBreakoutStrategy
from strategies.volatility_breakout import VolatilityBreakoutStrategy
from strategies.volume_spike import VolumeSpikeStrategy
from strategies.liquidity_grab import LiquidityGrabStrategy
from strategies.mean_reversion import MeanReversionStrategy
from strategies.momentum import MomentumStrategy
from strategies.volume_oi_strategies import VolumeStrategy, OpenInterestStrategy
from strategies.range_trading import RangeTradingStrategy
import pandas as pd

logger = logging.getLogger(__name__)


class MetaController:
    """
    Meta-controller that manages multiple strategies and makes final trading decisions.
    
    Features:
    - Market regime detection
    - Strategy selection based on regime
    - Signal aggregation with weighted confidence
    - Adaptive strategy weighting based on performance
    """
    
    def __init__(self, config=None):
        self.config = config or {}
        self.regime_detector = MarketRegimeDetector()
        
        # Initialize all strategies
        self.strategies: List[BaseStrategy] = [
            TrendBreakoutStrategy({'ema_fast': 9, 'ema_slow': 21}),
            VolatilityBreakoutStrategy({'bb_window': 20, 'bb_std': 2.0}),
            VolumeSpikeStrategy({'volume_ma_period': 20, 'spike_threshold': 2.5}),
            LiquidityGrabStrategy({'lookback_period': 10, 'min_shadow_ratio': 2.0}),
            MeanReversionStrategy({'rsi_period': 14, 'bb_window': 20}),
            MomentumStrategy({'momentum_period': 5, 'min_momentum': 0.02}),
            VolumeStrategy({'volume_ma_period': 20, 'volume_spike_threshold': 2.0}),
            OpenInterestStrategy({'oi_ma_period': 20, 'oi_change_threshold': 0.15}),
            RangeTradingStrategy({'rsi_period': 14, 'range_window': 20}),
        ]
        
        # Initial equal weights for all strategies
        self.strategy_weights = {s.name: 1.0 for s in self.strategies}
        
        # Performance tracking
        self.strategy_stats = {
            s.name: {
                "wins": 0, 
                "losses": 0, 
                "total_pnl": 0.0,
                "total_trades": 0
            } 
            for s in self.strategies
        }
        
        # Regime to strategy mapping - ALL strategies active in ALL regimes
        # Each strategy will generate signals independently, MetaController aggregates them
        self.regime_map = {
            "TREND_UP": [s.name for s in self.strategies],      # All 9 strategies
            "TREND_DOWN": [s.name for s in self.strategies],    # All 9 strategies
            "RANGE": [s.name for s in self.strategies],         # All 9 strategies
            "LOW_VOL": [s.name for s in self.strategies],       # All 9 strategies
            "HIGH_VOL": [s.name for s in self.strategies],      # All 9 strategies
            "ACCUMULATION": [s.name for s in self.strategies],  # All 9 strategies
            "UNKNOWN": [s.name for s in self.strategies]        # All 9 strategies
        }
        
        # Minimum confidence threshold for trading
        self.min_confidence = 0.65  # Increased from 0.5 to reduce false signals

        # Persistent stats storage (survives bot restart)
        default_stats_file = os.path.join(os.path.dirname(__file__), "data", "strategy_stats.json")
        self.stats_file = getattr(config, "strategy_stats_file", default_stats_file) if config else default_stats_file
        self._load_strategy_stats()
        
        logger.info(f"MetaController initialized with {len(self.strategies)} strategies")

    def _load_strategy_stats(self):
        """Load strategy stats/weights from disk if present."""
        try:
            if not os.path.exists(self.stats_file):
                return

            with open(self.stats_file, "r", encoding="utf-8") as f:
                payload = json.load(f)

            persisted_stats = payload.get("strategy_stats", {})
            persisted_weights = payload.get("strategy_weights", {})

            for strategy_name in self.strategy_stats.keys():
                if strategy_name in persisted_stats and isinstance(persisted_stats[strategy_name], dict):
                    for key in ["wins", "losses", "total_pnl", "total_trades"]:
                        if key in persisted_stats[strategy_name]:
                            self.strategy_stats[strategy_name][key] = persisted_stats[strategy_name][key]
                if strategy_name in persisted_weights:
                    self.strategy_weights[strategy_name] = float(persisted_weights[strategy_name])

            logger.info(f"Loaded strategy stats from {self.stats_file}")
        except Exception as e:
            logger.warning(f"Failed to load strategy stats: {e}")

    def _save_strategy_stats(self):
        """Persist strategy stats/weights to disk."""
        try:
            os.makedirs(os.path.dirname(self.stats_file), exist_ok=True)
            payload = {
                "strategy_stats": self.strategy_stats,
                "strategy_weights": self.strategy_weights
            }
            temp_path = f"{self.stats_file}.tmp"
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, self.stats_file)
            logger.info(f"[STATS SAVED] Strategy stats saved to {self.stats_file}")
        except Exception as e:
            logger.error(f"[STATS SAVE ERROR] Failed to save strategy stats: {e}")

    def update_weights(self, strategy_name: str, is_win: bool, pnl: float, exit_reason: str = None):
        """Update strategy weights based on performance."""
        if strategy_name not in self.strategy_stats:
            return
            
        stats = self.strategy_stats[strategy_name]
        
        # Умное определение win/loss с учётом причины выхода
        actual_is_win = is_win
        if exit_reason == 'sl' and pnl > 0:
            # Закрылись по стопу, но в плюсе (трейлинг сработал) - считаем как win
            actual_is_win = True
            logger.info(f"[STRATEGY STATS] {strategy_name}: Closed by SL but profitable (pnl={pnl:.2f}) - counting as WIN")
        elif exit_reason == 'tp' and pnl <= 0:
            # Редкий случай: закрылись по TP, но вышли в ноль или минус (комиссии)
            actual_is_win = False
            logger.warning(f"[STRATEGY STATS] {strategy_name}: Closed by TP but unprofitable (pnl={pnl:.2f}) - counting as LOSS")
        
        if actual_is_win:
            stats["wins"] += 1
        else:
            stats["losses"] += 1
            
        stats["total_pnl"] += pnl
        stats["total_trades"] += 1
        
        # Adaptive weight adjustment (only after 5+ trades)
        total_trades = stats["total_trades"]
        if total_trades >= 5:
            winrate = stats["wins"] / total_trades
            
            if winrate > 0.6:
                self.strategy_weights[strategy_name] = min(2.0, self.strategy_weights[strategy_name] * 1.05)
                logger.debug(f"Increased weight for {strategy_name} to {self.strategy_weights[strategy_name]:.2f}")
            elif winrate < 0.4:
                self.strategy_weights[strategy_name] = max(0.1, self.strategy_weights[strategy_name] * 0.95)
                logger.debug(f"Decreased weight for {strategy_name} to {self.strategy_weights[strategy_name]:.2f}")

        # Принудительное сохранение с проверкой
        self._save_strategy_stats()
        
        # Проверка что файл действительно обновился
        try:
            with open(self.stats_file, "r", encoding="utf-8") as f:
                saved_data = json.load(f)
                saved_stats = saved_data.get("strategy_stats", {}).get(strategy_name, {})
                logger.info(
                    f"[STATS VERIFY] Файл обновлен: {strategy_name} -> wins={saved_stats.get('wins')}, "
                    f"losses={saved_stats.get('losses')}, total_pnl={saved_stats.get('total_pnl')}"
                )
        except Exception as e:
            logger.error(f"[STATS VERIFY ERROR] Не удалось проверить файл: {e}")
        
        logger.info(f"[STRATEGY STATS UPDATED] {strategy_name}: wins={stats['wins']}, losses={stats['losses']}, total_pnl={stats['total_pnl']:.2f}, weight={self.strategy_weights[strategy_name]:.3f}")

    def update_strategy_performance(self, strategy_name: str, pnl: float, is_winner: bool, exit_reason: str = None):
        """
        Backward-compatible alias used by TradingBot.
        Updates strategy stats and adaptive weight.
        """
        self.update_weights(strategy_name=strategy_name, is_win=is_winner, pnl=pnl, exit_reason=exit_reason)
    
    def update_strategy_stats(self, strategy_name: str, is_winner: bool, pnl: float, pnl_pct: float):
        """
        Обновление статистики стратегии при закрытии позиции.
        Используется PositionMonitor при ручном закрытии позиций.

        Args:
            strategy_name: Название стратегии
            is_winner: Была ли сделка прибыльной
            pnl: Абсолютная прибыль/убыток в долларах
            pnl_pct: Процентная прибыль/убыток
        """
        if strategy_name not in self.strategy_stats:
            logger.warning(f"[STATS] Стратегия {strategy_name} не найдена, пропускаем обновление")
            return

        # Обновляем счетчики
        stats = self.strategy_stats[strategy_name]
        stats["total_trades"] += 1

        if is_winner:
            stats["wins"] += 1
        else:
            stats["losses"] += 1

        stats["total_pnl"] += pnl

        # Обновляем веса стратегий (внутри уже есть _save_strategy_stats)
        self.update_weights(
            strategy_name=strategy_name,
            is_win=is_winner,
            pnl=pnl,
            exit_reason='manual_close'
        )

        logger.info(
            f"[STATS UPDATED] {strategy_name}: Trades={stats['total_trades']}, "
            f"Wins={stats['wins']}, Losses={stats['losses']}, PnL=${stats['total_pnl']:.2f}"
        )

    def adapt_strategy_weights(self):
        """
        Recalculate strategy weights periodically from historical stats.
        Kept for compatibility with TradingBot.check_adaptation().
        """
        for name, stats in self.strategy_stats.items():
            total_trades = stats["total_trades"]
            if total_trades < 5:
                continue

            winrate = stats["wins"] / total_trades if total_trades > 0 else 0.0
            avg_pnl = stats["total_pnl"] / total_trades

            if winrate >= 0.6 and avg_pnl > 0:
                self.strategy_weights[name] = min(2.0, self.strategy_weights[name] * 1.03)
            elif winrate <= 0.4 or avg_pnl < 0:
                self.strategy_weights[name] = max(0.1, self.strategy_weights[name] * 0.97)
        self._save_strategy_stats()

    def get_active_strategies(self, regime: str) -> List[str]:
        """Get list of active strategies for current regime."""
        return self.regime_map.get(regime, ["MeanReversion", "LiquidityGrab"])

    def aggregate_signals(self, df: pd.DataFrame, market_data: Dict) -> Dict[str, any]:
        """Aggregate signals from all active strategies."""
        # Detect market regime
        regime_info = self.regime_detector.detect(df)
        regime = regime_info["regime"]
        regime_confidence = regime_info["confidence"]
        
        logger.info(f"Market Regime: {regime} (confidence: {regime_confidence:.2f})")
        
        # Get active strategies for current regime
        active_strategy_names = self.get_active_strategies(regime)
        active_strategies = [s for s in self.strategies if s.name in active_strategy_names]
        
        if not active_strategies:
            logger.warning(f"No active strategies for regime {regime}, using defaults")
            active_strategies = self.strategies[:2]
            active_strategy_names = [s.name for s in active_strategies]
        
        # Collect signals from active strategies
        signals = []
        
        for strategy in active_strategies:
            try:
                signal = strategy.generate_signal(df, market_data)
                
                if signal.direction != 'neutral':
                    weight = self.strategy_weights[strategy.name]
                    weighted_confidence = signal.confidence * weight
                    
                    signals.append({
                        "strategy": strategy.name,
                        "direction": signal.direction,
                        "confidence": signal.confidence,
                        "weighted_confidence": weighted_confidence,
                        "entry_price": signal.entry_price,
                        "stop_loss": signal.stop_loss,
                        "take_profit": signal.take_profit,
                        "metadata": signal.metadata
                    })
                    
            except Exception as e:
                logger.error(f"Error in strategy {strategy.name}: {e}")
        
        # Make final decision
        final_decision = {
            "direction": "neutral",
            "confidence": 0.0,
            "source_strategies": [],
            "entry_price": None,
            "stop_loss": None,
            "take_profit": None
        }
        
        if signals:
            long_signals = [s for s in signals if s['direction'] == 'long']
            short_signals = [s for s in signals if s['direction'] == 'short']
            
            long_score = sum(s['weighted_confidence'] for s in long_signals)
            short_score = sum(s['weighted_confidence'] for s in short_signals)
            
            if long_score > short_score and long_score > self.min_confidence:
                final_decision["direction"] = "long"
                final_decision["confidence"] = min(long_score / len(long_signals), 1.0)
                final_decision["source_strategies"] = [s['strategy'] for s in long_signals]
                final_decision["entry_price"] = sum(s['entry_price'] for s in long_signals) / len(long_signals)
                final_decision["stop_loss"] = sum(s['stop_loss'] for s in long_signals) / len(long_signals)
                final_decision["take_profit"] = sum(s['take_profit'] for s in long_signals) / len(long_signals)
                
            elif short_score > long_score and short_score > self.min_confidence:
                final_decision["direction"] = "short"
                final_decision["confidence"] = min(short_score / len(short_signals), 1.0)
                final_decision["source_strategies"] = [s['strategy'] for s in short_signals]
                final_decision["entry_price"] = sum(s['entry_price'] for s in short_signals) / len(short_signals)
                final_decision["stop_loss"] = sum(s['stop_loss'] for s in short_signals) / len(short_signals)
                final_decision["take_profit"] = sum(s['take_profit'] for s in short_signals) / len(short_signals)
        
        return {
            "decision": final_decision,
            "regime": regime,
            "regime_confidence": regime_confidence,
            "active_strategies": active_strategy_names,
            "all_signals": signals,
            "strategy_weights": self.strategy_weights.copy()
        }

    def get_performance_summary(self) -> Dict:
        """Get performance summary for all strategies."""
        summary = {}
        for name, stats in self.strategy_stats.items():
            total = stats["total_trades"]
            if total > 0:
                winrate = stats["wins"] / total
                avg_pnl = stats["total_pnl"] / total
            else:
                winrate = 0.0
                avg_pnl = 0.0
                
            summary[name] = {
                "total_trades": total,
                "wins": stats["wins"],
                "losses": stats["losses"],
                "winrate": winrate,
                "avg_pnl": avg_pnl,
                "total_pnl": stats["total_pnl"],
                "weight": self.strategy_weights[name]
            }
        return summary

    def should_reduce_exposure(self) -> bool:
        """
        Determine if the bot should reduce exposure based on overall strategy performance.
        
        Returns True if:
        - Overall winrate across all strategies is below 40%
        - Recent drawdown exceeds threshold
        - Multiple strategies are underperforming
        """
        total_wins = sum(s["wins"] for s in self.strategy_stats.values())
        total_losses = sum(s["losses"] for s in self.strategy_stats.values())
        total_trades = total_wins + total_losses
        
        # Not enough data yet
        if total_trades < 10:
            return False
        
        overall_winrate = total_wins / total_trades
        
        # Reduce exposure if winrate is too low
        if overall_winrate < 0.35:
            logger.warning(f"Overall winrate {overall_winrate:.2f} is below threshold, reducing exposure")
            return True
        
        # Check if majority of strategies are underperforming
        underperforming_count = 0
        for name, stats in self.strategy_stats.items():
            if stats["total_trades"] >= 5:
                strategy_winrate = stats["wins"] / stats["total_trades"]
                if strategy_winrate < 0.4:
                    underperforming_count += 1
        
        # Reduce exposure if more than half of active strategies are failing
        active_strategy_count = len([s for s in self.strategy_stats.values() if s["total_trades"] > 0])
        if active_strategy_count > 0 and underperforming_count > active_strategy_count * 0.6:
            logger.warning(f"{underperforming_count}/{active_strategy_count} strategies underperforming, reducing exposure")
            return True
        
        return False
