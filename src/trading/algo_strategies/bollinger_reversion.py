"""Bollinger Bands Mean-Reversion strategy for LLM_TRADER.

Ported from Quantumbotx BollingerBandsStrategy, adapted to use
LLM_TRADER's native numba-optimised indicators (no pandas_ta dependency).

Logic:
- Price touches / crosses the lower band in an uptrend → BUY
- Price touches / crosses the upper band in a downtrend → SELL
- Trend filter: SMA(200) direction.
"""

import numpy as np

from src.indicators.volatility.volatility_indicators import bollinger_bands_numba
from src.indicators.overlap.overlap_indicators import sma_numba
from .base import AlgoStrategy, AlgoSignal


class BollingerReversionStrategy(AlgoStrategy):
    """Mean-reversion signals using Bollinger Bands + long-term SMA trend filter."""

    name = "Bollinger Reversion"

    def __init__(
        self,
        bb_length: int = 20,
        bb_std: float = 2.0,
        trend_period: int = 200,
    ) -> None:
        self.bb_length = bb_length
        self.bb_std = bb_std
        self.trend_period = trend_period

    @property
    def min_bars(self) -> int:
        return self.trend_period + self.bb_length

    def analyze(self, close, high, low, open_, volume) -> AlgoSignal:
        if len(close) < self.min_bars:
            return AlgoSignal(
                strategy_name=self.name,
                signal="HOLD",
                explanation=f"Insufficient data ({len(close)} / {self.min_bars} bars).",
                confidence=0.0,
            )

        upper, _mid, lower = bollinger_bands_numba(close, self.bb_length, self.bb_std)
        trend_sma = sma_numba(close, self.trend_period)

        price = close[-1]
        prev_low = low[-1]
        prev_high = high[-1]
        sma_val = trend_sma[-1]

        if np.isnan(upper[-1]) or np.isnan(sma_val):
            return AlgoSignal(
                strategy_name=self.name,
                signal="HOLD",
                explanation="Indicators still warming up.",
                confidence=0.0,
            )

        is_uptrend = price > sma_val
        is_downtrend = price < sma_val

        if is_uptrend and prev_low <= lower[-1]:
            return AlgoSignal(
                strategy_name=self.name,
                signal="BUY",
                explanation=(
                    f"Uptrend (price {price:.4f} > SMA200 {sma_val:.4f}) "
                    f"& price touched lower band ({lower[-1]:.4f}). Oversold reversion."
                ),
                confidence=0.70,
            )

        if is_downtrend and prev_high >= upper[-1]:
            return AlgoSignal(
                strategy_name=self.name,
                signal="SELL",
                explanation=(
                    f"Downtrend (price {price:.4f} < SMA200 {sma_val:.4f}) "
                    f"& price touched upper band ({upper[-1]:.4f}). Overbought reversion."
                ),
                confidence=0.70,
            )

        # ── OPT-IN: fade upper band in RANGING regime (even if uptrend) ──────
        # Classic mean-reversion: in a range, the upper Bollinger band is a
        # sell zone. Decomment to allow SELLs on upper-band touches when the
        # SMA200 trend filter would otherwise forbid them.
        # if is_uptrend and prev_high >= upper[-1]:
        #     return AlgoSignal(
        #         strategy_name=self.name,
        #         signal="SELL",
        #         explanation=(
        #             f"Range-fade (price {price:.4f}, SMA200 {sma_val:.4f}) "
        #             f"& price touched upper band ({upper[-1]:.4f}). Exhaustion short."
        #         ),
        #         confidence=0.55,
        #     )

        # Inside bands – no actionable signal
        band_pct = (price - lower[-1]) / (upper[-1] - lower[-1]) * 100 if upper[-1] != lower[-1] else 50
        return AlgoSignal(
            strategy_name=self.name,
            signal="HOLD",
            explanation=f"Price inside bands ({band_pct:.0f}% from lower). No reversion signal.",
            confidence=0.40,
        )
