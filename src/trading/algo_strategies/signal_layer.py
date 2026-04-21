"""Strategy Signal Layer – orchestrates all algo strategies for LLM_TRADER.

Runs every registered strategy on the current OHLCV candles, aggregates
the results, and builds a formatted section that is injected into the
AI prompt so the LLM sees the classical-strategy consensus alongside its
own technical-indicator analysis.

Usage (from AnalysisEngine):
    section = self.signal_layer.build_signals_section(ohlcv, symbol)
    # Pass section to prompt_builder.build_prompt(algo_signals=section)
"""

from __future__ import annotations

import traceback
from typing import List, Optional, Dict, Any

import numpy as np

from src.logger.logger import Logger
from .base import AlgoStrategy, AlgoSignal
from .bollinger_reversion import BollingerReversionStrategy
from .rsi_crossover import RSICrossoverStrategy
from .ma_crossover import MACrossoverStrategy
from .market_condition_detector import MarketConditionDetector


def _default_strategies() -> List[AlgoStrategy]:
    return [
        BollingerReversionStrategy(),
        RSICrossoverStrategy(),
        MACrossoverStrategy(),
    ]


class StrategySignalLayer:
    """Runs multiple algo strategies and formats their output for the AI prompt.

    This layer is intentionally read-only and stateless between calls:
    it does not store positions, history or state.  All learning remains
    in the vector memory / brain service.
    """

    def __init__(
        self,
        logger: Logger,
        strategies: Optional[List[AlgoStrategy]] = None,
        market_detector: Optional[MarketConditionDetector] = None,
    ) -> None:
        self.logger = logger
        self.strategies: List[AlgoStrategy] = strategies if strategies is not None else _default_strategies()
        self.market_detector = market_detector or MarketConditionDetector()

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def build_signals_section(
        self,
        ohlcv: np.ndarray,
        symbol: str,
    ) -> Optional[str]:
        """Run all strategies + market detector and return a formatted prompt section.

        Args:
            ohlcv:  OHLCV numpy array in CCXT format [N, 6]:
                    columns: [timestamp, open, high, low, close, volume]
            symbol: Trading symbol (e.g. "BTC/USDT").

        Returns:
            Formatted multi-line string for injection into the AI prompt,
            or None if there is insufficient data.
        """
        result = self.run(ohlcv, symbol)
        if result is None:
            return None
        return self._format_section(result["signals"], result["market_condition"])

    def run(
        self,
        ohlcv: np.ndarray,
        symbol: str,
    ) -> Optional[Dict[str, Any]]:
        """Run all strategies and return JSON-serialisable dict (used by dashboard).

        Returns:
            dict with keys ``signals`` (list of dicts), ``market_condition`` (dict)
            and ``symbol`` (str), or None on insufficient data.
        """
        unpacked = self._unpack_ohlcv(ohlcv)
        if unpacked is None:
            return None
        open_, high, low, close, volume = unpacked
        signals = self._run_strategies(close, high, low, open_, volume)
        market_cond = self._run_market_detector(close, high, low, symbol)

        return {
            "signals": signals,
            "market_condition": market_cond,
            "symbol": symbol,
        }

    # ------------------------------------------------------------------ #
    # Internal helpers                                                     #
    # ------------------------------------------------------------------ #

    def _unpack_ohlcv(
        self,
        ohlcv: np.ndarray,
    ) -> Optional[tuple]:
        """Validate and unpack OHLCV array. Returns (open, high, low, close, volume) or None."""
        if ohlcv is None or len(ohlcv) < 2:
            return None
        try:
            open_  = ohlcv[:, 1].astype(np.float64)
            high   = ohlcv[:, 2].astype(np.float64)
            low    = ohlcv[:, 3].astype(np.float64)
            close  = ohlcv[:, 4].astype(np.float64)
            volume = ohlcv[:, 5].astype(np.float64)
            return open_, high, low, close, volume
        except (IndexError, ValueError):
            self.logger.warning("[AlgoStrategies] Could not unpack OHLCV array – skipping.")
            return None

    def _run_strategies(
        self,
        close: np.ndarray,
        high: np.ndarray,
        low: np.ndarray,
        open_: np.ndarray,
        volume: np.ndarray,
    ) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        for strat in self.strategies:
            try:
                sig = strat.analyze(close, high, low, open_, volume)
                results.append({
                    "strategy_name": sig.strategy_name,
                    "signal": sig.signal,
                    "confidence": round(sig.confidence, 3),
                    "explanation": sig.explanation,
                })
            except Exception:  # pragma: no cover
                self.logger.warning(
                    "[AlgoStrategies] Strategy %s raised an exception:\n%s",
                    strat.name,
                    traceback.format_exc(),
                )
        return results

    def _run_market_detector(
        self,
        close: np.ndarray,
        high: np.ndarray,
        low: np.ndarray,
        symbol: str,
    ) -> Optional[Dict[str, Any]]:
        try:
            return self.market_detector.detect(close, high, low, symbol)
        except Exception:  # pragma: no cover
            self.logger.warning(
                "[AlgoStrategies] MarketConditionDetector failed:\n%s",
                traceback.format_exc(),
            )
            return None

    # ------------------------------------------------------------------ #
    # Formatting                                                           #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _format_section(
        signals: List[Dict[str, Any]],
        market_cond: Optional[Dict[str, Any]],
    ) -> str:
        lines = ["## ALGO STRATEGY SIGNALS (classical technical strategies)"]

        # Market regime
        if market_cond:
            cond      = market_cond.get("market_condition", "unknown").upper()
            vol_reg   = market_cond.get("volatility_regime", "normal").upper()
            adx_val   = market_cond.get("adx", 0.0)
            conf      = market_cond.get("confidence", 0.0)
            inst_type = market_cond.get("instrument_type", "CRYPTO")
            lines.append(
                f"**Market Regime [{inst_type}]:** {cond} "
                f"(confidence {conf:.0%}, ADX={adx_val:.1f}, volatility={vol_reg})"
            )
            lines.append("")

        if not signals:
            lines.append("No strategy signals available.")
            return "\n".join(lines)

        # Individual strategy results
        lines.append("| Strategy | Signal | Confidence | Explanation |")
        lines.append("|----------|--------|------------|-------------|")
        for s in signals:
            conf_str = f"{s['confidence']:.0%}"
            lines.append(f"| {s['strategy_name']} | **{s['signal']}** | {conf_str} | {s['explanation']} |")

        # Consensus
        actionable = [s for s in signals if s['signal'] != "HOLD"]
        if actionable:
            buy_count  = sum(1 for s in actionable if s['signal'] == "BUY")
            sell_count = sum(1 for s in actionable if s['signal'] == "SELL")
            total = len(actionable)
            if buy_count > sell_count:
                consensus = f"BULLISH ({buy_count}/{total} strategies signal BUY)"
            elif sell_count > buy_count:
                consensus = f"BEARISH ({sell_count}/{total} strategies signal SELL)"
            else:
                consensus = f"MIXED ({buy_count} BUY / {sell_count} SELL – conflicting signals)"
        else:
            consensus = "NEUTRAL (all strategies HOLD)"

        lines.append("")
        lines.append(f"**Algo Consensus:** {consensus}")
        lines.append(
            "_Note: These are classical indicator-based signals. "
            "Integrate them as one data point alongside the full technical and sentiment analysis._"
        )

        return "\n".join(lines)
