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
from src.indicators.trend.trend_indicators import adx_numba
from .base import AlgoStrategy, AlgoSignal


class RSICrossoverStrategy(AlgoStrategy):
    """Momentum signal from RSI crossing its own moving average."""

    name = "RSI Crossover"

    def __init__(
        self,
        rsi_period: int = 10,  # 14 → 10 for faster crypto reaction
        rsi_ma_period: int = 8,  # 10 → 8
        trend_period: int = 50,
    ) -> None:
        self.rsi_period = rsi_period
        self.rsi_ma_period = rsi_ma_period
        self.trend_period = trend_period

    @property
    def min_bars(self) -> int:
        # Reduced: RSI(14) + RSI_MA(10) + trend SMA(50) need ~75 bars,
        # but we only need 2 valid RSI_MA bars for crossover detection.
        return self.rsi_period + self.rsi_ma_period + 2

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
            rsi_momentum = abs(rsi[-1] - rsi_ma[-1])
            confidence = min(0.85, 0.60 + rsi_momentum * 0.02)
            return AlgoSignal(
                strategy_name=self.name,
                signal="BUY",
                explanation=(
                    f"Uptrend (price {price:.4f} > SMA{self.trend_period} {trend_sma[-1]:.4f}) "
                    f"& RSI({self.rsi_period}) {rsi[-1]:.1f} crossed above RSI-MA {rsi_ma[-1]:.1f}."
                ),
                confidence=round(confidence, 2),
            )

        if is_downtrend and bearish_cross:
            rsi_momentum = abs(rsi[-1] - rsi_ma[-1])
            confidence = min(0.85, 0.60 + rsi_momentum * 0.02)
            return AlgoSignal(
                strategy_name=self.name,
                signal="SELL",
                explanation=(
                    f"Downtrend (price {price:.4f} < SMA{self.trend_period} {trend_sma[-1]:.4f}) "
                    f"& RSI({self.rsi_period}) {rsi[-1]:.1f} crossed below RSI-MA {rsi_ma[-1]:.1f}."
                ),
                confidence=round(confidence, 2),
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
