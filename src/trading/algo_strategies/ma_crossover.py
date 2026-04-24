"""Moving Average Crossover strategy for LLM_TRADER.

Ported from Quantumbotx MACrossoverStrategy, adapted for LLM_TRADER's
numba-optimised indicators.

Logic:
- Fast SMA crosses above Slow SMA → BUY (golden cross)
- Fast SMA crosses below Slow SMA → SELL (death cross)
"""

import numpy as np

from src.indicators.overlap.overlap_indicators import sma_numba
from src.indicators.trend.trend_indicators import adx_numba
from .base import AlgoStrategy, AlgoSignal


class MACrossoverStrategy(AlgoStrategy):
    """Trend-following signal from golden / death SMA crossovers."""

    name = "MA Crossover"

    def __init__(
        self,
        fast_period: int = 20,
        slow_period: int = 50,
    ) -> None:
        self.fast_period = fast_period
        self.slow_period = slow_period

    @property
    def min_bars(self) -> int:
        return self.fast_period + self.slow_period + 2

    def analyze(self, close, high, low, open_, volume) -> AlgoSignal:
        if len(close) < self.min_bars:
            return AlgoSignal(
                strategy_name=self.name,
                signal="HOLD",
                explanation=f"Insufficient data ({len(close)} / {self.min_bars} bars).",
                confidence=0.0,
            )

        # ADX filter - block choppy markets
        adx_arr, _, _ = adx_numba(high, low, close, 14)
        if adx_arr[-1] < 25:  # Market too choppy
            return AlgoSignal(
                strategy_name=self.name,
                signal="HOLD",
                explanation=f"ADX {adx_arr[-1]:.1f} < 25 - no trend",
                confidence=0.0,
            )

        # Volume filter - confirm with 1.3x average 20-period volume
        if len(volume) >= 20:
            vol_avg = np.mean(volume[-20:])
            if volume[-1] < vol_avg * 1.3:
                return AlgoSignal(
                    strategy_name=self.name,
                    signal="HOLD",
                    explanation="Insufficient volume for confirmation",
                    confidence=0.0,
                )

        ma_fast = sma_numba(close, self.fast_period)
        ma_slow = sma_numba(close, self.slow_period)

        if (
            np.isnan(ma_fast[-1])
            or np.isnan(ma_slow[-1])
            or np.isnan(ma_fast[-2])
            or np.isnan(ma_slow[-2])
        ):
            return AlgoSignal(
                strategy_name=self.name,
                signal="HOLD",
                explanation="Indicators still warming up.",
                confidence=0.0,
            )

        # Crossover detection
        golden_cross = ma_fast[-2] <= ma_slow[-2] and ma_fast[-1] > ma_slow[-1]
        death_cross = ma_fast[-2] >= ma_slow[-2] and ma_fast[-1] < ma_slow[-1]

        if golden_cross:
            ma_diff_pct = abs(ma_fast[-1] - ma_slow[-1]) / ma_slow[-1]
            confidence = min(0.85, 0.55 + ma_diff_pct * 10)
            return AlgoSignal(
                strategy_name=self.name,
                signal="BUY",
                explanation=(
                    f"Golden Cross: SMA{self.fast_period} ({ma_fast[-1]:.4f}) "
                    f"crossed above SMA{self.slow_period} ({ma_slow[-1]:.4f})."
                ),
                confidence=round(confidence, 2),
            )

        if death_cross:
            ma_diff_pct = abs(ma_fast[-1] - ma_slow[-1]) / ma_slow[-1]
            confidence = min(0.85, 0.55 + ma_diff_pct * 10)
            return AlgoSignal(
                strategy_name=self.name,
                signal="SELL",
                explanation=(
                    f"Death Cross: SMA{self.fast_period} ({ma_fast[-1]:.4f}) "
                    f"crossed below SMA{self.slow_period} ({ma_slow[-1]:.4f})."
                ),
                confidence=round(confidence, 2),
            )

        # No crossover this candle → wait for a new cross
        return AlgoSignal(
            strategy_name=self.name,
            signal="HOLD",
            explanation=(
                f"No crossover this candle. "
                f"SMA{self.fast_period}={ma_fast[-1]:.4f}, SMA{self.slow_period}={ma_slow[-1]:.4f}."
            ),
            confidence=0.35,
        )
