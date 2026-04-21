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
    "Bollinger_Reversion": "RANGING",
    "BollingerReversion":  "RANGING",
    "RSI_Crossover":       "RANGING",
    "RSICrossover":        "RANGING",
    "MA_Crossover":        "TRENDING",
    "MACrossover":         "TRENDING",
}


class AlgoFastTrader:
    """Convert strategy signal consensus into a trade decision.

    Applies majority-vote across regime-appropriate strategies only, and
    blocks trades when conditions indicate no directional edge.
    """

    #: Minimum fraction of strategies that must agree (strict majority).
    MIN_AGREE_RATIO: float = 0.5

    #: ADX below this → market is choppy, trend-following has no edge.
    MIN_ADX_FOR_TREND: float = 20.0

    #: Chop + high vol = worst conditions for any strategy.
    BLOCK_HIGH_VOL_CHOP: bool = True

    def decide(
        self,
        signals: List[Dict[str, Any]],
        market_condition: Optional[Dict[str, Any]] = None,
    ) -> Tuple[str, str, str]:
        """Derive a trade signal from strategy consensus with regime filtering.

        Returns:
            Tuple ``(signal, confidence, reasoning)``.
        """
        if not signals:
            return "HOLD", "LOW", "Fast trader: no algo signals available"

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
                "HOLD", "LOW",
                f"Fast trader: no regime-appropriate strategies for {regime} "
                f"(have: [{names}])",
            )

        # ─── Majority vote ───────────────────────────────────────────────
        n_total = len(eligible)
        buy_sigs = [s for s in eligible if s.get("signal") == "BUY"]
        sell_sigs = [s for s in eligible if s.get("signal") == "SELL"]
        n_buy = len(buy_sigs)
        n_sell = len(sell_sigs)
        min_agree = math.floor(n_total * self.MIN_AGREE_RATIO) + 1

        if n_buy > n_sell and n_buy >= min_agree:
            consensus, agreeing, n_agree = "BUY", buy_sigs, n_buy
        elif n_sell > n_buy and n_sell >= min_agree:
            consensus, agreeing, n_agree = "SELL", sell_sigs, n_sell
        else:
            names = ", ".join(s.get("strategy_name", "?") for s in eligible)
            return (
                "HOLD", "LOW",
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
        reasoning = (
            f"[Fast] {n_agree}/{n_total} eligible agree {consensus} "
            f"[{agreeing_names}] | regime={regime} vol={vol} ADX={adx:.1f}"
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

    def _check_regime_quality(
        self, regime: str, vol: str, adx: float
    ) -> Optional[str]:
        """Return a blocking reason if regime is too low-quality."""
        if self.BLOCK_HIGH_VOL_CHOP and vol == "HIGH" and adx < self.MIN_ADX_FOR_TREND:
            return (
                f"Fast trader BLOCKED: HIGH vol + chop (ADX={adx:.1f} < "
                f"{self.MIN_ADX_FOR_TREND}) — no directional edge"
            )
        if regime == "UNKNOWN" and adx == 0.0:
            return "Fast trader BLOCKED: unknown regime, insufficient data"
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
"""Derives a BUY/SELL/HOLD decision from algo strategy signal consensus.

Used by Fast Trading Mode to execute trades directly on classical strategy
signals without waiting for LLM analysis each candle.
"""
import math
from typing import Optional, Tuple, List, Dict, Any


class AlgoFastTrader:
    """Convert strategy signal consensus into a trade decision.

    Applies a simple majority-vote across all running strategies.
    Confidence is mapped to HIGH/MEDIUM/LOW based on agreement strength.
    """

    #: Minimum fraction of strategies that must agree to act (exclusive majority).
    MIN_AGREE_RATIO: float = 0.5

    def decide(
        self,
        signals: List[Dict[str, Any]],
        market_condition: Optional[Dict[str, Any]] = None,
    ) -> Tuple[str, str, str]:
        """Derive a trade signal from strategy consensus.

        Args:
            signals: List of signal dicts with keys ``signal``, ``strategy_name``,
                     ``confidence``, ``explanation`` as produced by StrategySignalLayer.
            market_condition: Optional market condition dict from MarketConditionDetector.

        Returns:
            Tuple ``(signal, confidence, reasoning)`` where:
            - signal: ``"BUY"``, ``"SELL"``, or ``"HOLD"``
            - confidence: ``"HIGH"``, ``"MEDIUM"``, or ``"LOW"``
            - reasoning: Human-readable explanation of the decision
        """
        if not signals:
            return "HOLD", "LOW", "Fast trader: no algo signals available"

        n_total = len(signals)
        buy_sigs = [s for s in signals if s.get("signal") == "BUY"]
        sell_sigs = [s for s in signals if s.get("signal") == "SELL"]
        n_buy = len(buy_sigs)
        n_sell = len(sell_sigs)

        # Strict majority: more than half must agree
        min_agree = math.floor(n_total * self.MIN_AGREE_RATIO) + 1

        if n_buy > n_sell and n_buy >= min_agree:
            consensus = "BUY"
            agreeing = buy_sigs
            n_agree = n_buy
        elif n_sell > n_buy and n_sell >= min_agree:
            consensus = "SELL"
            agreeing = sell_sigs
            n_agree = n_sell
        else:
            strategy_names = ", ".join(s.get("strategy_name", "?") for s in signals)
            return (
                "HOLD",
                "LOW",
                f"Fast trader: no consensus — {n_buy} BUY vs {n_sell} SELL "
                f"across [{strategy_names}]",
            )

        # Map agreement strength → confidence
        if n_agree == n_total:
            confidence = "HIGH"
        elif n_agree >= math.ceil(n_total * 0.67):
            confidence = "MEDIUM"
        else:
            confidence = "LOW"

        agreeing_names = ", ".join(s.get("strategy_name", "?") for s in agreeing)
        mc_note = ""
        if market_condition:
            mc = market_condition.get("market_condition", "")
            vol = market_condition.get("volatility_regime", "")
            mc_note = f" | regime={mc} vol={vol}"

        reasoning = (
            f"[Fast] {n_agree}/{n_total} strategies agree {consensus} "
            f"[{agreeing_names}]{mc_note}"
        )
        return consensus, confidence, reasoning
