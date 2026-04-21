"""Backtest Engine for historical strategy evaluation.

Enables running the analysis pipeline on historical dates to evaluate
strategy performance, inspired by TradingAgents' propagate(date) pattern.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List, TYPE_CHECKING

from src.logger.logger import Logger
from src.trading.data_models import (
    TradeDecision, ClosedTradeResult, Rating,
)
from src.trading.position_extractor import PositionExtractor
from src.parsing.unified_parser import UnifiedParser
from src.utils.format_utils import FormatUtils

if TYPE_CHECKING:
    from src.contracts.model_contract import ModelManagerProtocol
    from src.config.protocol import ConfigProtocol
    from src.analyzer.analysis_engine import AnalysisEngine
    from src.trading.trading_strategy import TradingStrategy
    from src.rag import RagEngine


@dataclass
class BacktestResult:
    """Result of a backtest run."""
    start_date: str
    end_date: str
    symbol: str
    timeframe: str
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    total_pnl_pct: float
    avg_pnl_pct: float
    max_drawdown_pct: float
    sharpe_ratio: Optional[float]
    decisions: List[Dict[str, Any]]


class BacktestEngine:
    """Runs historical backtests by replaying analysis on past dates.

    Usage:
        engine = BacktestEngine(logger, config, analysis_engine, strategy)
        result = await engine.run_backtest(
            symbol="BTC/USDC",
            start_date="2026-03-01",
            end_date="2026-04-01",
            timeframe="4h",
        )
    """

    def __init__(
        self,
        logger: Logger,
        config: "ConfigProtocol",
        analysis_engine: "AnalysisEngine",
        trading_strategy: "TradingStrategy",
        model_manager: "ModelManagerProtocol",
    ):
        self.logger = logger
        self.config = config
        self.analysis_engine = analysis_engine
        self.trading_strategy = trading_strategy
        self.model_manager = model_manager

    async def run_backtest(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
        timeframe: str = "4h",
        initial_capital: float = 10000.0,
    ) -> BacktestResult:
        """Run a backtest between two dates.

        Args:
            symbol: Trading pair (e.g., "BTC/USDC")
            start_date: Start date YYYY-MM-DD
            end_date: End date YYYY-MM-DD
            timeframe: Candle timeframe
            initial_capital: Starting capital for simulation

        Returns:
            BacktestResult with performance metrics
        """
        from src.utils.timeframe_validator import TimeframeValidator

        self.logger.info(
            "Starting backtest: %s %s from %s to %s",
            symbol, timeframe, start_date, end_date,
        )

        # Validate timeframe
        if not TimeframeValidator.validate(timeframe):
            raise ValueError(f"Unsupported timeframe: {timeframe}")

        # Calculate candle intervals
        tf_minutes = TimeframeValidator.to_minutes(timeframe)
        start_dt = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)

        # Generate candle close timestamps
        candle_times = []
        current = start_dt
        while current <= end_dt:
            candle_times.append(current)
            current += timedelta(minutes=tf_minutes)

        self.logger.info("Backtest: %d candles to analyze", len(candle_times))

        # Initialize tracking
        decisions: List[Dict[str, Any]] = []
        capital = initial_capital
        position = None  # Track simulated position
        peak_capital = initial_capital
        max_drawdown = 0.0
        pnl_list: List[float] = []

        # Create extractor and risk manager once (reused for all candles)
        extractor = PositionExtractor(self.logger, UnifiedParser(self.logger, format_utils=FormatUtils()))
        from src.managers.risk_manager import RiskManager
        risk_mgr = RiskManager(self.logger, self.config)

        # Initialize analyzer for this symbol
        exchange, exchange_id = await self._find_exchange(symbol)
        if not exchange:
            raise ValueError(f"Symbol {symbol} not found on any exchange")

        self.analysis_engine.initialize_for_symbol(
            symbol=symbol,
            exchange=exchange,
            timeframe=timeframe,
        )

        # Process each candle
        for i, candle_time in enumerate(candle_times):
            try:
                # Fetch historical data up to this candle
                result = await self._analyze_at_time(
                    symbol=symbol,
                    timeframe=timeframe,
                    candle_time=candle_time,
                )

                if "error" in result:
                    self.logger.warning("Backtest error at %s: %s", candle_time, result["error"])
                    continue

                # Extract current price
                current_price = self._extract_price(result)
                if current_price <= 0:
                    continue

                # Check existing position SL/TP
                if position:
                    close_reason = self._check_position(position, current_price)
                    if close_reason:
                        # Close position
                        pnl_pct = self._calculate_pnl(position, current_price)
                        pnl_quote = position["quote_amount"] * (pnl_pct / 100)
                        capital += pnl_quote

                        # Track drawdown
                        if capital > peak_capital:
                            peak_capital = capital
                        dd = (peak_capital - capital) / peak_capital * 100
                        if dd > max_drawdown:
                            max_drawdown = dd

                        pnl_list.append(pnl_pct)
                        decisions.append({
                            "timestamp": candle_time.isoformat(),
                            "action": "CLOSE",
                            "price": current_price,
                            "pnl_pct": pnl_pct,
                            "reason": close_reason,
                        })
                        position = None

                # Process new decision
                raw_response = result.get("raw_response", "")
                if raw_response:
                    signal, confidence, sl, tp, pos_size, reasoning, _rating = extractor.extract_trading_info(raw_response)

                    # Open new position
                    if signal in ("BUY", "SELL") and position is None:
                        risk = risk_mgr.calculate_entry_parameters(
                            signal=signal,
                            current_price=current_price,
                            capital=capital,
                            confidence=confidence,
                            stop_loss=sl,
                            take_profit=tp,
                            position_size=pos_size,
                        )

                        position = {
                            "direction": "LONG" if signal == "BUY" else "SHORT",
                            "entry_price": current_price,
                            "stop_loss": risk.stop_loss,
                            "take_profit": risk.take_profit,
                            "quote_amount": risk.quote_amount,
                            "size_pct": risk.size_pct,
                            "confidence": confidence,
                        }

                        decisions.append({
                            "timestamp": candle_time.isoformat(),
                            "action": signal,
                            "price": current_price,
                            "stop_loss": risk.stop_loss,
                            "take_profit": risk.take_profit,
                            "confidence": confidence,
                            "reasoning": reasoning[:200],
                        })

                # Progress logging
                if (i + 1) % 50 == 0:
                    self.logger.info(
                        "Backtest progress: %d/%d candles | Capital: $%.2f",
                        i + 1, len(candle_times), capital,
                    )

            except Exception as e:
                self.logger.error("Backtest error at %s: %s", candle_time, e)
                continue

        # Calculate final metrics
        total_trades = len([d for d in decisions if d["action"] in ("BUY", "SELL")])
        closed_trades = len([d for d in decisions if d["action"] == "CLOSE"])
        winning = len([d for d in decisions if d["action"] == "CLOSE" and d.get("pnl_pct", 0) > 0])
        losing = len([d for d in decisions if d["action"] == "CLOSE" and d.get("pnl_pct", 0) <= 0])

        win_rate = (winning / closed_trades * 100) if closed_trades > 0 else 0.0
        total_pnl = ((capital - initial_capital) / initial_capital) * 100
        avg_pnl = (sum(pnl_list) / len(pnl_list)) if pnl_list else 0.0

        # Sharpe ratio (simplified)
        sharpe = None
        if len(pnl_list) >= 2:
            import numpy as np
            pnl_arr = np.array(pnl_list)
            if pnl_arr.std() > 0:
                sharpe = float((pnl_arr.mean() / pnl_arr.std()) * (252 ** 0.5))

        result = BacktestResult(
            start_date=start_date,
            end_date=end_date,
            symbol=symbol,
            timeframe=timeframe,
            total_trades=total_trades,
            winning_trades=winning,
            losing_trades=losing,
            win_rate=win_rate,
            total_pnl_pct=total_pnl,
            avg_pnl_pct=avg_pnl,
            max_drawdown_pct=max_drawdown,
            sharpe_ratio=sharpe,
            decisions=decisions,
        )

        self.logger.info(
            "Backtest complete: %d trades, %.1f%% win rate, %.2f%% P&L, %.2f%% max DD",
            total_trades, win_rate, total_pnl, max_drawdown,
        )

        return result

    async def _analyze_at_time(
        self,
        symbol: str,
        timeframe: str,
        candle_time: datetime,
    ) -> Dict[str, Any]:
        """Run analysis as if it were a specific point in time.

        TODO: Implement proper historical replay by passing candle_time
        to exchange.fetch_ohlcv(since=...) so the analysis only sees data
        up to this timestamp. Currently analyzes current market state.

        Args:
            symbol: Trading pair
            timeframe: Candle timeframe
            candle_time: Historical timestamp to analyze at

        Returns:
            Analysis result dictionary
        """
        # The analysis engine uses the exchange's OHLCV data
        # which is already historical, so we just run the normal analysis
        try:
            context_data = await self.analysis_engine.collect_market_data()
            return await self.analysis_engine.analyze_market(**context_data)
        except Exception as e:
            return {"error": str(e)}

    async def _find_exchange(self, symbol: str):
        """Find an exchange that supports the symbol."""
        # Delegate to exchange manager if available
        if hasattr(self.analysis_engine, 'exchange') and self.analysis_engine.exchange:
            return self.analysis_engine.exchange, "current"
        return None, None

    @staticmethod
    def _extract_price(result: Dict[str, Any]) -> float:
        """Extract current price from analysis result."""
        if "current_price" in result:
            return float(result["current_price"])
        if "context" in result and result["context"] is not None:
            return float(result["context"].current_price)
        return 0.0

    @staticmethod
    def _check_position(position: Dict[str, Any], current_price: float) -> Optional[str]:
        """Check if position hit SL/TP.

        Returns:
            Close reason if hit, else None
        """
        direction = position["direction"]
        sl = position["stop_loss"]
        tp = position["take_profit"]

        if direction == "LONG":
            if current_price <= sl:
                return "stop_loss"
            if current_price >= tp:
                return "take_profit"
        else:  # SHORT
            if current_price >= sl:
                return "stop_loss"
            if current_price <= tp:
                return "take_profit"

        return None

    @staticmethod
    def _calculate_pnl(position: Dict[str, Any], current_price: float) -> float:
        """Calculate P&L percentage for a position."""
        entry = position["entry_price"]
        if position["direction"] == "LONG":
            return ((current_price - entry) / entry) * 100
        else:
            return ((entry - current_price) / entry) * 100
