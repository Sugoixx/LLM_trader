"""Market Condition Detector for LLM_TRADER.

Adapted from Quantumbotx MarketConditionDetector to use LLM_TRADER's
existing numba-optimised ADX and SMA indicators.

Detects:
- trending vs ranging market regime
- volatility level (high / normal / low)
- instrument type from symbol name
"""

from datetime import datetime
from typing import Dict, Any

import numpy as np

from src.indicators.trend.trend_indicators import adx_numba
from src.indicators.overlap.overlap_indicators import sma_numba
from src.indicators.volatility.volatility_indicators import atr_numba


# Instrument-specific thresholds ported from Quantumbotx
_INSTRUMENT_CONFIGS: Dict[str, Dict[str, Any]] = {
    "INDICES": {
        "trend_sensitivity": 0.55,
        "volatility_threshold": 1.5,
    },
    "FOREX": {
        "trend_sensitivity": 0.50,
        "volatility_threshold": 1.2,
    },
    "GOLD": {
        "trend_sensitivity": 0.60,
        "volatility_threshold": 2.0,
    },
    "CRYPTO": {
        "trend_sensitivity": 0.45,
        "volatility_threshold": 3.0,
    },
    "COMMODITIES": {
        "trend_sensitivity": 0.55,
        "volatility_threshold": 2.5,
    },
}

_INDICES_KEYWORDS = {"US30", "US100", "US500", "DE30", "UK100", "JP225", "NAS100", "SPX500", "DAX"}
_GOLD_KEYWORDS    = {"XAU", "XAG", "GOLD"}
_FOREX_MAJORS     = {"EUR", "GBP", "USD", "JPY", "CHF", "CAD", "AUD", "NZD"}
_OIL_KEYWORDS     = {"CRUDOIL", "CRUDE", "WTI", "BRENT", "XTI", "XBR", "OIL", "ENERGY", "NGAS", "NATURALGAS"}


def _classify_instrument(symbol: str) -> str:
    upper = symbol.upper().replace("/", "").replace("-", "")
    if any(k in upper for k in _INDICES_KEYWORDS):
        return "INDICES"
    if any(k in upper for k in _GOLD_KEYWORDS):
        return "GOLD"
    if any(k in upper for k in _OIL_KEYWORDS):
        return "COMMODITIES"
    # Heuristic: short alphanumeric pairs that look like forex pairs
    # e.g. EURUSD, GBPJPY — typically 6 chars
    if any(upper.startswith(k) for k in _FOREX_MAJORS) and len(upper) <= 8:
        return "FOREX"
    return "CRYPTO"


class MarketConditionDetector:
    """Detects trending vs ranging regime from raw OHLCV numpy arrays.

    All heavy lifting is delegated to the existing Numba-accelerated
    indicator functions already in LLM_TRADER's indicator layer.
    """

    def __init__(self, adx_period: int = 14, sma_fast: int = 20, sma_slow: int = 50) -> None:
        self.adx_period = adx_period
        self.sma_fast = sma_fast
        self.sma_slow = sma_slow

    # ------------------------------------------------------------------ #

    def detect(
        self,
        close: np.ndarray,
        high: np.ndarray,
        low: np.ndarray,
        symbol: str = "BTC/USDT",
    ) -> Dict[str, Any]:
        """Run market condition analysis.

        Args:
            close:  Closing prices.
            high:   High prices.
            low:    Low prices.
            symbol: Trading pair name (used to pick instrument config).

        Returns:
            dict with keys: instrument_type, market_condition, confidence,
            volatility_regime, adx, trend_score.
        """
        min_bars = self.adx_period * 3
        if len(close) < min_bars:
            return self._default(symbol)

        instrument_type = _classify_instrument(symbol)
        cfg = _INSTRUMENT_CONFIGS.get(instrument_type, _INSTRUMENT_CONFIGS["CRYPTO"])

        adx_arr, _, _ = adx_numba(high, low, close, self.adx_period)
        ma_fast = sma_numba(close, self.sma_fast)
        ma_slow = sma_numba(close, self.sma_slow)
        atr_arr = atr_numba(high, low, close, self.adx_period)

        adx_val = adx_arr[-1] if not np.isnan(adx_arr[-1]) else 20.0
        adx_trend = min(1.0, adx_val / 60.0)

        # MA alignment score over last 20 bars
        window = 20
        fast_slice = ma_fast[-window:]
        slow_slice = ma_slow[-window:]
        valid = ~(np.isnan(fast_slice) | np.isnan(slow_slice))
        ma_trend = abs((fast_slice[valid] > slow_slice[valid]).mean() - 0.5) * 2 if valid.any() else 0.5

        # Return-consistency score
        returns = np.diff(close[-window:]) / (close[-window - 1:-2] + 1e-12)
        consistency = min(1.0, abs(returns.mean()) / (returns.std() + 1e-10)) if len(returns) > 1 else 0.5

        trend_score = adx_trend * 0.4 + ma_trend * 0.3 + consistency * 0.3

        if trend_score > cfg["trend_sensitivity"]:
            condition = "trending"
            confidence = min(1.0, trend_score)
        else:
            condition = "ranging"
            confidence = min(1.0, 1.0 - trend_score)

        # Volatility regime
        current_atr = atr_arr[-1]
        avg_atr = np.nanmean(atr_arr[-50:]) if len(atr_arr) >= 50 else current_atr
        if not np.isnan(current_atr) and avg_atr > 0:
            vol_ratio = current_atr / avg_atr
            if vol_ratio > cfg["volatility_threshold"]:
                vol_regime = "high"
            elif vol_ratio < (1.0 / cfg["volatility_threshold"]):
                vol_regime = "low"
            else:
                vol_regime = "normal"
        else:
            vol_regime = "normal"

        return {
            "instrument_type": instrument_type,
            "market_condition": condition,
            "confidence": round(confidence, 3),
            "volatility_regime": vol_regime,
            "adx": round(float(adx_val), 2),
            "trend_score": round(float(trend_score), 3),
            "timestamp": datetime.now().isoformat(),
        }

    def _default(self, symbol: str) -> Dict[str, Any]:
        return {
            "instrument_type": _classify_instrument(symbol),
            "market_condition": "unknown",
            "confidence": 0.0,
            "volatility_regime": "normal",
            "adx": 0.0,
            "trend_score": 0.0,
            "timestamp": datetime.now().isoformat(),
        }
