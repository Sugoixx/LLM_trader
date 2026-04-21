"""RSI × MA Crossover strategy for LLM_TRADER.

Ported from Quantumbotx RSICrossoverStrategy, adapted for LLM_TRADER's
numba-optimised indicators.

Logic:
- RSI crosses above its own SMA in an uptrend → BUY (bullish momentum)
- RSI crosses below its own SMA in a downtrend → SELL (bearish momentum)
- Trend filter: SMA(50) of price.
"""

import numpy as np

from src.indicators.momentum.momentum_indicators import rsi_numba
from src.indicators.overlap.overlap_indicators import sma_numba
from .base import AlgoStrategy, AlgoSignal


class RSICrossoverStrategy(AlgoStrategy):
    """Momentum signal from RSI crossing its own moving average."""

    name = "RSI Crossover"

    def __init__(
        self,
        rsi_period: int = 14,
        rsi_ma_period: int = 10,
        trend_period: int = 50,
    ) -> None:
        self.rsi_period = rsi_period
        self.rsi_ma_period = rsi_ma_period
        self.trend_period = trend_period

    @property
    def min_bars(self) -> int:
        return self.trend_period + self.rsi_period + self.rsi_ma_period + 2

    def analyze(self, close, high, low, open_, volume) -> AlgoSignal:
        if len(close) < self.min_bars:
            return AlgoSignal(
                strategy_name=self.name,
                signal="HOLD",
                explanation=f"Insufficient data ({len(close)} / {self.min_bars} bars).",
                confidence=0.0,
            )

        rsi = rsi_numba(close, self.rsi_period)
        rsi_ma = sma_numba(rsi, self.rsi_ma_period)
        trend_sma = sma_numba(close, self.trend_period)

        # Need at least 2 valid bars for crossover detection
        if np.isnan(rsi_ma[-1]) or np.isnan(rsi_ma[-2]) or np.isnan(trend_sma[-1]):
            return AlgoSignal(
                strategy_name=self.name,
                signal="HOLD",
                explanation="Indicators still warming up.",
                confidence=0.0,
            )

        price = close[-1]
        is_uptrend = price > trend_sma[-1]
        is_downtrend = price < trend_sma[-1]

        # Crossover: RSI went from below → above its MA
        bullish_cross = rsi[-2] <= rsi_ma[-2] and rsi[-1] > rsi_ma[-1]
        bearish_cross = rsi[-2] >= rsi_ma[-2] and rsi[-1] < rsi_ma[-1]

        if is_uptrend and bullish_cross:
            return AlgoSignal(
                strategy_name=self.name,
                signal="BUY",
                explanation=(
                    f"Uptrend (price {price:.4f} > SMA{self.trend_period} {trend_sma[-1]:.4f}) "
                    f"& RSI({self.rsi_period}) {rsi[-1]:.1f} crossed above RSI-MA {rsi_ma[-1]:.1f}."
                ),
                confidence=0.65,
            )

        if is_downtrend and bearish_cross:
            return AlgoSignal(
                strategy_name=self.name,
                signal="SELL",
                explanation=(
                    f"Downtrend (price {price:.4f} < SMA{self.trend_period} {trend_sma[-1]:.4f}) "
                    f"& RSI({self.rsi_period}) {rsi[-1]:.1f} crossed below RSI-MA {rsi_ma[-1]:.1f}."
                ),
                confidence=0.65,
            )

        return AlgoSignal(
            strategy_name=self.name,
            signal="HOLD",
            explanation=(
                f"RSI {rsi[-1]:.1f} vs MA {rsi_ma[-1]:.1f}, "
                f"trend {'UP' if is_uptrend else 'DOWN' if is_downtrend else 'FLAT'}. No cross."
            ),
            confidence=0.35,
        )
