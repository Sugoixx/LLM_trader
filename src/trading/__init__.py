"""Trading module for automated trading decisions and position management."""

from .data_models import Position, TradeDecision, TradingMemory, Rating, DebateResult, DebateArgument
from .brain import TradingBrainService
from .memory import TradingMemoryService
from .statistics import TradingStatisticsService
from .position_extractor import PositionExtractor
from .trading_strategy import TradingStrategy
from .statistics_calculator import TradingStatistics, StatisticsCalculator
from .vector_memory import VectorMemoryService
from .debate_service import DebateService
from .backtest_engine import BacktestEngine, BacktestResult
from .order_executor import OrderExecutorProtocol, DemoExecutor, LiveExecutor, OrderResult

__all__ = [
    'Position',
    'TradeDecision',
    'TradingMemory',
    'TradingStatistics',
    'StatisticsCalculator',
    'TradingBrainService',
    'TradingMemoryService',
    'TradingStatisticsService',
    'PositionExtractor',
    'TradingStrategy',
    'VectorMemoryService',
    'Rating',
    'DebateResult',
    'DebateArgument',
    'DebateService',
    'BacktestEngine',
    'BacktestResult',
    'OrderExecutorProtocol',
    'DemoExecutor',
    'LiveExecutor',
    'OrderResult',
]
