"""Base class and signal dataclass for algo strategies."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

import numpy as np


@dataclass
class AlgoSignal:
    """Result produced by an AlgoStrategy."""
    strategy_name: str
    signal: str           # "BUY" | "SELL" | "HOLD"
    explanation: str
    confidence: float = 0.5   # 0.0 – 1.0


class AlgoStrategy(ABC):
    """Abstract base for all LLM_TRADER algo strategies.

    Strategies receive raw OHLCV numpy arrays (CCXT format) and produce
    an AlgoSignal.  They must be stateless and side-effect-free.
    """

    name: str = "Unnamed"

    @abstractmethod
    def analyze(
        self,
        close: np.ndarray,
        high: np.ndarray,
        low: np.ndarray,
        open_: np.ndarray,
        volume: np.ndarray,
    ) -> AlgoSignal:
        """Run the strategy on the provided OHLCV arrays.

        Args:
            close:  Closing prices (shape N,).
            high:   High prices (shape N,).
            low:    Low prices (shape N,).
            open_:  Opening prices (shape N,).
            volume: Trading volume (shape N,).

        Returns:
            AlgoSignal with signal, explanation and confidence.
        """

    # ------------------------------------------------------------------ #
    # Helper – minimum bars required before the strategy can fire         #
    # ------------------------------------------------------------------ #
    @property
    def min_bars(self) -> int:
        """Minimum number of candles needed to produce a valid signal."""
        return 50
