"""Bull/Bear Debate Service for reducing cognitive biases in trading decisions.

Inspired by TradingAgents' multi-agent debate pattern. Runs a structured
Bull vs Bear debate after the initial analysis to challenge the AI's
first-impression decision and reduce confirmation bias.
"""

import asyncio
from typing import Optional, TYPE_CHECKING
from src.logger.logger import Logger
from src.trading.data_models import (
    DebateArgument,
    DebateResult,
    Rating,
)

if TYPE_CHECKING:
    from src.contracts.model_contract import ModelManagerProtocol
    from src.config.protocol import ConfigProtocol


# Prompt templates for debate participants
BULL_SYSTEM = """You are a BULLISH crypto analyst. Your job is to construct the STRONGEST possible argument FOR the proposed trade.

Rules:
- Be intellectually honest: only use REAL evidence from the analysis data
- Acknowledge risks but explain why they are manageable
- Focus on catalysts, momentum, and favorable conditions
- Rate your conviction: HIGH / MEDIUM / LOW
- Keep your argument concise (max 200 words)
- Output format:
  ARGUMENT: <your bull case>
  KEY_POINTS: <comma-separated, max 3>
  CONVICTION: <HIGH/MEDIUM/LOW>"""

BEAR_SYSTEM = """You are a BEARISH crypto analyst. Your job is to construct the STRONGEST possible argument AGAINST the proposed trade.

Rules:
- Be intellectually honest: only use REAL evidence from the analysis data
- Acknowledge strengths but explain why they may not hold
- Focus on risks, invalidation levels, and adverse scenarios
- Rate your conviction: HIGH / MEDIUM / LOW
- Keep your argument concise (max 200 words)
- Output format:
  ARGUMENT: <your bear case>
  KEY_POINTS: <comma-separated, max 3>
  CONVICTION: <HIGH/MEDIUM/LOW>"""

JUDGE_SYSTEM = """You are a neutral JUDGE evaluating a Bull vs Bear debate on a crypto trade.

Based on the arguments, determine:
1. Which side presented stronger EVIDENCE (not just conviction)?
2. Should the original signal be maintained, downgraded, or reversed?
3. What is the adjusted confidence level?

Output format (strict JSON):
```json
{
  "verdict": "BULL_WINS|BEAR_WINS|NEUTRAL",
  "final_signal": "BUY|SELL|HOLD|CLOSE",
  "final_confidence": "HIGH|MEDIUM|LOW",
  "confidence_delta": -20 to +20,
  "summary": "1-2 sentence explanation"
}
```"""


class DebateService:
    """Runs a structured Bull/Bear debate on trading decisions.

    Flow:
    1. Initial analysis produces a signal (BUY/SELL/HOLD)
    2. Bull analyst argues FOR the trade
    3. Bear analyst argues AGAINST the trade
    4. Judge evaluates and produces final verdict
    5. Verdict may adjust signal and confidence
    """

    def __init__(
        self,
        logger: Logger,
        model_manager: "ModelManagerProtocol",
        config: "ConfigProtocol",
    ):
        self.logger = logger
        self.model_manager = model_manager
        self.config = config

    async def debate(
        self,
        signal: str,
        confidence: str,
        analysis_context: str,
        reasoning: str,
    ) -> DebateResult:
        """Run a Bull/Bear debate on a trading decision.

        Args:
            signal: Original trading signal (BUY, SELL, HOLD, CLOSE)
            confidence: Original confidence (HIGH, MEDIUM, LOW)
            analysis_context: Formatted market analysis data
            reasoning: Original AI reasoning

        Returns:
            DebateResult with final verdict and adjusted signal/confidence
        """
        self.logger.info(
            "Starting Bull/Bear debate for signal: %s (%s)", signal, confidence
        )

        # Skip debate for HOLD or CLOSE signals (no trade to debate)
        if signal in ("HOLD", "CLOSE", "CLOSE_LONG", "CLOSE_SHORT", "UPDATE"):
            self.logger.info("Skipping debate for %s signal", signal)
            return DebateResult(
                original_signal=signal,
                original_confidence=confidence,
                final_signal=signal,
                final_confidence=confidence,
                verdict="SKIPPED",
                summary=f"Debate skipped for {signal} signal",
            )

        # Run bull/bear debate - use combined call if enabled to reduce API usage
        if self.config.DEBATE_COMBINED_ARGUMENTS:
            bull_arg, bear_arg = await self._get_combined_arguments(
                signal, confidence, analysis_context, reasoning
            )
        else:
            # Original parallel calls
            bull_task = self._get_argument(
                "bull", signal, confidence, analysis_context, reasoning
            )
            bear_task = self._get_argument(
                "bear", signal, confidence, analysis_context, reasoning
            )
            bull_arg, bear_arg = await asyncio.gather(bull_task, bear_task)

        # Judge evaluates
        verdict = await self._judge(
            signal, confidence, analysis_context, bull_arg, bear_arg
        )

    async def _get_combined_arguments(
        self,
        signal: str,
        confidence: str,
        analysis_context: str,
        reasoning: str,
    ) -> tuple[DebateArgument, DebateArgument]:
        """Get both bull and bear arguments in a single LLM call to reduce API usage.

        Args:
            signal: Original signal
            confidence: Original confidence
            analysis_context: Market analysis data
            reasoning: Original AI reasoning

        Returns:
            Tuple of (bull_argument, bear_argument)
        """
        combined_system = f"""You are a debate moderator. Generate both BULLISH and BEARISH arguments for the proposed trade in a single response.

BULLISH ANALYST RULES:
- Construct the STRONGEST possible argument FOR the proposed trade
- Be intellectually honest: only use REAL evidence from the analysis data
- Acknowledge risks but explain why they are manageable
- Focus on catalysts, momentum, and favorable conditions
- Rate conviction: HIGH / MEDIUM / LOW

BEARISH ANALYST RULES:
- Construct the STRONGEST possible argument AGAINST the proposed trade
- Be intellectually honest: only use REAL evidence from the analysis data
- Acknowledge strengths but explain why they may not hold
- Focus on risks, invalidation levels, and adverse scenarios
- Rate conviction: HIGH / MEDIUM / LOW

Output format (strict):
BULL_ARGUMENT: <bull case>
BULL_KEY_POINTS: <comma-separated, max 3>
BULL_CONVICTION: <HIGH/MEDIUM/LOW>

BEAR_ARGUMENT: <bear case>
BEAR_KEY_POINTS: <comma-separated, max 3>
BEAR_CONVICTION: <HIGH/MEDIUM/LOW>"""

        user_prompt = (
            f"## Proposed Trade\n"
            f"Signal: {signal}\n"
            f"Confidence: {confidence}\n"
            f"Original Reasoning: {reasoning}\n\n"
            f"## Market Analysis Data\n{analysis_context}\n\n"
            f"Generate both bull and bear arguments for this trade."
        )

        try:
            # Use quick model for debate arguments if configured
            if self.config.DEBATE_USE_QUICK_MODEL:
                response = await self.model_manager.send_quick_prompt(
                    prompt=user_prompt,
                    system_message=combined_system,
                )
            else:
                response = await self.model_manager.send_prompt(
                    prompt=user_prompt,
                    system_message=combined_system,
                )
            return self._parse_combined_arguments(response)
        except Exception as e:
            self.logger.error("Error getting combined debate arguments: %s", e)
            # Fallback: generate individual arguments
            self.logger.info("Falling back to individual debate arguments")
            bull_task = self._get_argument(
                "bull", signal, confidence, analysis_context, reasoning
            )
            bear_task = self._get_argument(
                "bear", signal, confidence, analysis_context, reasoning
            )
            bull_arg, bear_arg = await asyncio.gather(bull_task, bear_task)
            return bull_arg, bear_arg

    @staticmethod
    def _parse_combined_arguments(
        response: str,
    ) -> tuple[DebateArgument, DebateArgument]:
        """Parse combined bull/bear arguments from LLM response.

        Args:
            response: Raw LLM response

        Returns:
            Tuple of parsed DebateArgument objects
        """
        import re

        def extract_section(pattern: str, text: str) -> str:
            match = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
            return match.group(1).strip() if match else ""

        def extract_key_points(section: str) -> tuple:
            points_match = re.search(
                r"KEY_POINTS:\s*(.+?)(?:\n|$)", section, re.IGNORECASE
            )
            if points_match:
                points_str = points_match.group(1).strip()
                return tuple(p.strip() for p in points_str.split(",") if p.strip())
            return ()

        def extract_conviction(section: str) -> float:
            conv_match = re.search(
                r"CONVICTION:\s*(HIGH|MEDIUM|LOW)", section, re.IGNORECASE
            )
            if conv_match:
                conv_str = conv_match.group(1).upper()
                return {"HIGH": 1.0, "MEDIUM": 0.5, "LOW": 0.2}.get(conv_str, 0.5)
            return 0.5

        # Extract bull section
        bull_text = extract_section(
            r"BULL_ARGUMENT:\s*(.+?)(?=BEAR_ARGUMENT:|$)", response
        )
        bull_key_points = extract_key_points(bull_text)
        bull_conviction = extract_conviction(bull_text)

        # Extract bear section
        bear_text = extract_section(r"BEAR_ARGUMENT:\s*(.+?)(?=$)", response)
        bear_key_points = extract_key_points(bear_text)
        bear_conviction = extract_conviction(bear_text)

        # Apply sign for bear
        bear_conviction = -bear_conviction

        # Fallback if parsing failed
        if not bull_text:
            bull_text = "Combined debate parsing failed for bull argument"
        if not bear_text:
            bear_text = "Combined debate parsing failed for bear argument"

        bull_arg = DebateArgument(
            participant="bull",
            argument=bull_text,
            key_points=bull_key_points,
            confidence_impact=bull_conviction,
        )

        bear_arg = DebateArgument(
            participant="bear",
            argument=bear_text,
            key_points=bear_key_points,
            confidence_impact=bear_conviction,
        )

        return bull_arg, bear_arg

    async def _get_argument(
        self,
        side: str,
        signal: str,
        confidence: str,
        analysis_context: str,
        reasoning: str,
    ) -> DebateArgument:
        """Get a single debate argument from bull or bear side.

        Args:
            side: "bull" or "bear"
            signal: Original signal
            confidence: Original confidence
            analysis_context: Market analysis data
            reasoning: Original AI reasoning

        Returns:
            DebateArgument with the participant's case
        """
        system_prompt = BULL_SYSTEM if side == "bull" else BEAR_SYSTEM
        direction = "FOR" if side == "bull" else "AGAINST"

        user_prompt = (
            f"## Proposed Trade\n"
            f"Signal: {signal}\n"
            f"Confidence: {confidence}\n"
            f"Original Reasoning: {reasoning}\n\n"
            f"## Market Analysis Data\n{analysis_context}\n\n"
            f"Construct your strongest argument {direction} this trade."
        )

        try:
            # Use quick model for debate arguments if configured
            if self.config.DEBATE_USE_QUICK_MODEL:
                response = await self.model_manager.send_quick_prompt(
                    prompt=user_prompt,
                    system_message=system_prompt,
                )
            else:
                response = await self.model_manager.send_prompt(
                    prompt=user_prompt,
                    system_message=system_prompt,
                )
            return self._parse_argument(side, response)
        except Exception as e:
            self.logger.error("Error getting %s argument: %s", side, e)
            return DebateArgument(
                participant=side,
                argument=f"Error generating {side} argument: {e}",
                confidence_impact=0.0,
            )

    async def _judge(
        self,
        original_signal: str,
        original_confidence: str,
        analysis_context: str,
        bull_arg: DebateArgument,
        bear_arg: DebateArgument,
    ) -> DebateResult:
        """Judge the debate and produce a final verdict.

        Args:
            original_signal: Original trading signal
            original_confidence: Original confidence
            analysis_context: Market analysis data
            bull_arg: Bull side argument
            bear_arg: Bear side argument

        Returns:
            DebateResult with final verdict
        """
        user_prompt = (
            f"## Original Decision\n"
            f"Signal: {original_signal} | Confidence: {original_confidence}\n\n"
            f"## Bull Argument\n"
            f"{bull_arg.argument}\n"
            f"Key Points: {', '.join(bull_arg.key_points)}\n"
            f"Conviction: {bull_arg.confidence_impact:+.0f}\n\n"
            f"## Bear Argument\n"
            f"{bear_arg.argument}\n"
            f"Key Points: {', '.join(bear_arg.key_points)}\n"
            f"Conviction: {bear_arg.confidence_impact:+.0f}\n\n"
            f"Evaluate both sides and render your verdict."
        )

        try:
            response = await self.model_manager.send_prompt(
                prompt=user_prompt,
                system_message=JUDGE_SYSTEM,
            )
            return self._parse_verdict(
                original_signal,
                original_confidence,
                bull_arg,
                bear_arg,
                response,
            )
        except Exception as e:
            self.logger.error("Error in debate judge: %s", e)
            # Fallback: keep original decision
            return DebateResult(
                original_signal=original_signal,
                original_confidence=original_confidence,
                bull_arguments=(bull_arg,),
                bear_arguments=(bear_arg,),
                verdict="ERROR",
                final_signal=original_signal,
                final_confidence=original_confidence,
                confidence_delta=0.0,
                summary=f"Judge error: {e}. Keeping original decision.",
            )

    @staticmethod
    def _parse_argument(side: str, response: str) -> DebateArgument:
        """Parse a debate argument from LLM response.

        Args:
            side: "bull" or "bear"
            response: Raw LLM response

        Returns:
            Parsed DebateArgument
        """
        import re

        argument = ""
        key_points: tuple = ()
        conviction = 0.0

        # Extract argument
        arg_match = re.search(
            r"ARGUMENT:\s*(.+?)(?:\nKEY_POINTS:|$)", response, re.DOTALL
        )
        if arg_match:
            argument = arg_match.group(1).strip()

        # Extract key points
        points_match = re.search(
            r"KEY_POINTS:\s*(.+?)(?:\nCONVICTION:|$)", response, re.DOTALL
        )
        if points_match:
            points_str = points_match.group(1).strip()
            key_points = tuple(p.strip() for p in points_str.split(",") if p.strip())

        # Extract conviction
        conv_match = re.search(
            r"CONVICTION:\s*(HIGH|MEDIUM|LOW)", response, re.IGNORECASE
        )
        if conv_match:
            conv_str = conv_match.group(1).upper()
            conviction = {"HIGH": 1.0, "MEDIUM": 0.5, "LOW": 0.2}.get(conv_str, 0.5)

        # Apply sign based on side
        if side == "bear":
            conviction = -conviction

        # Fallback if parsing failed
        if not argument:
            argument = response.strip()[:500]

        return DebateArgument(
            participant=side,
            argument=argument,
            key_points=key_points,
            confidence_impact=conviction,
        )

    @staticmethod
    def _parse_verdict(
        original_signal: str,
        original_confidence: str,
        bull_arg: DebateArgument,
        bear_arg: DebateArgument,
        response: str,
    ) -> DebateResult:
        """Parse the judge's verdict from LLM response.

        Args:
            original_signal: Original signal
            original_confidence: Original confidence
            bull_arg: Bull argument
            bear_arg: Bear argument
            response: Raw judge response

        Returns:
            Parsed DebateResult
        """
        import json
        import re

        # Try to extract JSON from response
        json_match = re.search(r"```json\s*(\{.+?\})\s*```", response, re.DOTALL)
        if json_match:
            try:
                data = json.loads(json_match.group(1))
                verdict = data.get("verdict", "NEUTRAL")
                final_signal = data.get("final_signal", original_signal)
                final_confidence = data.get("final_confidence", original_confidence)
                confidence_delta = float(data.get("confidence_delta", 0))
                summary = data.get("summary", "")

                # Validate confidence
                if final_confidence not in ("HIGH", "MEDIUM", "LOW"):
                    final_confidence = original_confidence

                # Clamp delta
                confidence_delta = max(-20, min(20, confidence_delta))

                # NEVER change the signal — debate can only adjust confidence
                final_signal = original_signal

                return DebateResult(
                    original_signal=original_signal,
                    original_confidence=original_confidence,
                    bull_arguments=(bull_arg,),
                    bear_arguments=(bear_arg,),
                    verdict=verdict,
                    final_signal=final_signal,
                    final_confidence=final_confidence,
                    confidence_delta=confidence_delta,
                    summary=summary,
                )
            except (json.JSONDecodeError, ValueError, KeyError) as e:
                pass

        # Fallback: use net conviction impact to adjust confidence only
        # NEVER change the signal — debate adjusts sizing, not direction
        net_impact = bull_arg.confidence_impact + bear_arg.confidence_impact
        final_signal = original_signal
        if net_impact > 0.3:
            verdict = "BULL_WINS"
            final_confidence = original_confidence
        elif net_impact < -0.3:
            verdict = "BEAR_WINS"
            final_confidence = "MEDIUM" if original_confidence == "HIGH" else "LOW"
        else:
            verdict = "NEUTRAL"
            final_confidence = (
                "MEDIUM" if original_confidence == "HIGH" else original_confidence
            )

        return DebateResult(
            original_signal=original_signal,
            original_confidence=original_confidence,
            bull_arguments=(bull_arg,),
            bear_arguments=(bear_arg,),
            verdict=verdict,
            final_signal=final_signal,
            final_confidence=final_confidence,
            confidence_delta=net_impact * 10,
            summary=f"Fallback verdict based on net conviction: {net_impact:+.1f}",
        )
