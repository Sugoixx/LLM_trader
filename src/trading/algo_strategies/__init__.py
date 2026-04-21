"""Algorithmic strategy layer for LLM_TRADER.

Provides classical technical strategies that run alongside the AI analysis
and inject a consensus signal section into the AI prompt for richer context.
"""

from .signal_layer import StrategySignalLayer
from .base import AlgoStrategy, AlgoSignal
from .bollinger_reversion import BollingerReversionStrategy
from .rsi_crossover import RSICrossoverStrategy
from .ma_crossover import MACrossoverStrategy
from .market_condition_detector import MarketConditionDetector
from .fast_trader import AlgoFastTrader
from .safety_guard import FastTradingSafetyGuard, FastTradingConfig, GuardCheckResult

__all__ = [
    "StrategySignalLayer",
    "AlgoStrategy",
    "AlgoSignal",
    "BollingerReversionStrategy",
    "RSICrossoverStrategy",
    "MACrossoverStrategy",
    "MarketConditionDetector",
    "AlgoFastTrader",
    "FastTradingSafetyGuard",
    "FastTradingConfig",
    "GuardCheckResult",
]
