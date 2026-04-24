"""Derives a BUY/SELL/HOLD decision from algo strategy signal consensus.

Used by Fast Trading Mode to execute trades directly on classical strategy
signals without waiting for LLM analysis each candle.

Safety layer (Tier-S):
  - ADX gate — block trades in chop (ADX < 20 and HIGH volatility)
  - Strategy-regime matching — reversion strategies only vote in RANGING,
    trend-following strategies only vote in TRENDING
  - Confidence degradation when ADX is weak
"""

import math
from typing import Optional, Tuple, List, Dict, Any


#: Strategy name → preferred market regime.
#: Reversion = good in RANGING.  Trend-following = good in TRENDING.
#: Names not listed are regime-agnostic (always eligible).
STRATEGY_REGIME_PREFERENCE: Dict[str, str] = {
    "Bollinger Reversion": "RANGING",
    "RSI Crossover": "TRENDING",
    "MA Crossover": "TRENDING",
}


class AlgoFastTrader:
    """Convert strategy signal consensus into a trade decision.

    Applies majority-vote across regime-appropriate strategies only, and
    blocks trades when conditions indicate no directional edge.
    """

    #: Minimum fraction of strategies that must agree.
    MIN_AGREE_RATIO: float = 0.67

    #: ADX below this → market is choppy, trend-following has no edge.
    MIN_ADX_FOR_TREND: float = 25.0

    #: Strategies with confidence below this are excluded from the vote.
    MIN_STRATEGY_CONFIDENCE: float = 0.55

    #: Maximum allowed drawdown (pause trading if exceeded)
    MAX_DRAWDOWN_PCT: float = -5.0

    #: Chop + high vol = worst conditions for any strategy.
    #: Safe to keep True — MarketConditionDetector calibrates "HIGH" per instrument:
    #: CRYPTO=3.0x ATR (rare), FOREX=1.2x (frequent during news), OIL=2.5x.
    BLOCK_HIGH_VOL_CHOP: bool = True

    def decide(
        self,
        signals: List[Dict[str, Any]],
        market_condition: Optional[Dict[str, Any]] = None,
        llm_signal: Optional[str] = None,
    ) -> Tuple[str, str, str]:
        """Derive a trade signal from strategy consensus with regime filtering.

        Returns:
            Tuple ``(signal, confidence, reasoning)``.
        """
        # Include LLM signal if provided
        if llm_signal:
            signals = signals + [{"strategy_name": "LLM", "signal": llm_signal}]

        if not signals:
            return "HOLD", "LOW", "Fast trader: no signals available"

        regime, vol, adx = self._extract_regime(market_condition)

        # ─── Regime quality gate ──────────────────────────────────────────
        block = self._check_regime_quality(regime, vol, adx)
        if block:
            return "HOLD", "LOW", block

        # ─── Filter by regime appropriateness ────────────────────────────
        eligible = self._filter_regime_appropriate(signals, regime)
        if not eligible:
            names = ", ".join(s.get("strategy_name", "?") for s in signals)
            return (
                "HOLD",
                "LOW",
                f"Fast trader: no regime-appropriate strategies for {regime} "
                f"(have: [{names}])",
            )

        # ─── Min confidence filter ────────────────────────────────────────
        eligible = [
            s
            for s in eligible
            if s.get("confidence", 0.0) >= self.MIN_STRATEGY_CONFIDENCE
        ]
        if not eligible:
            return (
                "HOLD",
                "LOW",
                "Fast trader: all strategies below min confidence threshold",
            )

        # ─── Confidence-weighted majority vote ───────────────────────────
        n_total = len(eligible)
        buy_sigs = [s for s in eligible if s.get("signal") == "BUY"]
        sell_sigs = [s for s in eligible if s.get("signal") == "SELL"]
        w_buy = sum(s.get("confidence", 0.5) for s in buy_sigs)
        w_sell = sum(s.get("confidence", 0.5) for s in sell_sigs)
        w_total = sum(s.get("confidence", 0.5) for s in eligible)
        min_agree_weight = w_total * self.MIN_AGREE_RATIO

        if w_buy > w_sell and w_buy >= min_agree_weight:
            consensus, agreeing, n_agree = "BUY", buy_sigs, len(buy_sigs)
        elif w_sell > w_buy and w_sell >= min_agree_weight:
            consensus, agreeing, n_agree = "SELL", sell_sigs, len(sell_sigs)
        else:
            n_buy, n_sell = len(buy_sigs), len(sell_sigs)
            names = ", ".join(s.get("strategy_name", "?") for s in eligible)
            return (
                "HOLD",
                "LOW",
                f"Fast trader: no consensus — {n_buy} BUY vs {n_sell} SELL "
                f"across [{names}] (regime={regime})",
            )

        # ─── Confidence mapping ──────────────────────────────────────────
        if n_agree == n_total:
            confidence = "HIGH"
        elif n_agree >= math.ceil(n_total * 0.67):
            confidence = "MEDIUM"
        else:
            confidence = "LOW"

        # Degrade if ADX is weak even in trending regime
        if regime == "TRENDING" and adx < 25.0:
            confidence = self._degrade_confidence(confidence)

        agreeing_names = ", ".join(s.get("strategy_name", "?") for s in agreeing)
        llm_note = " (incl. LLM)" if llm_signal else ""
        reasoning = (
            f"[Fast] {n_agree}/{n_total} eligible agree {consensus} "
            f"[{agreeing_names}] | regime={regime} vol={vol} ADX={adx:.1f}{llm_note}"
        )
        return consensus, confidence, reasoning

    # ── Helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _extract_regime(mc: Optional[Dict[str, Any]]) -> Tuple[str, str, float]:
        if not isinstance(mc, dict):
            return "UNKNOWN", "NORMAL", 0.0
        regime = str(mc.get("market_condition", "unknown")).upper()
        vol = str(mc.get("volatility_regime", "normal")).upper()
        try:
            adx = float(mc.get("adx", 0.0) or 0.0)
        except (TypeError, ValueError):
            adx = 0.0
        return regime, vol, adx

    def _check_regime_quality(self, regime: str, vol: str, adx: float) -> Optional[str]:
        """Return a blocking reason if regime is too low-quality."""
        if self.BLOCK_HIGH_VOL_CHOP and vol == "HIGH" and adx < self.MIN_ADX_FOR_TREND:
            return (
                f"Fast trader BLOCKED: HIGH vol + chop (ADX={adx:.1f} < "
                f"{self.MIN_ADX_FOR_TREND}) — no directional edge"
            )
        # UNKNOWN regime (no detector data) is not a blocker — proceed without regime info
        return None

    @staticmethod
    def _filter_regime_appropriate(
        signals: List[Dict[str, Any]], regime: str
    ) -> List[Dict[str, Any]]:
        """Keep only signals whose preferred regime matches (or agnostic)."""
        if regime not in ("TRENDING", "RANGING"):
            return list(signals)
        eligible = []
        for s in signals:
            name = s.get("strategy_name", "")
            pref = STRATEGY_REGIME_PREFERENCE.get(name)
            if pref is None or pref == regime:
                eligible.append(s)
        return eligible

    @staticmethod
    def _degrade_confidence(c: str) -> str:
        return {"HIGH": "MEDIUM", "MEDIUM": "LOW", "LOW": "LOW"}.get(c, "LOW")

    def _check_drawdown(
        self, current_capital: float, initial_capital: float
    ) -> Optional[str]:
        """Return block reason if drawdown exceeds allowed limit."""
        if initial_capital <= 0:
            return None
        drawdown_pct = ((current_capital - initial_capital) / initial_capital) * 100
        if drawdown_pct < self.MAX_DRAWDOWN_PCT:
            return (
                f"Fast trader BLOCKED: Drawdown {drawdown_pct:.1f}% "
                f"exceeds {self.MAX_DRAWDOWN_PCT}% limit"
            )
        return None
