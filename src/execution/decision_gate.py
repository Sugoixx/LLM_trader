"""DecisionGate — Validation firewall between Brain (Layer 1) and Execution (Layer 2).

Consolidates all pre-execution validation into a single, ordered pipeline:

    1. SIGNAL_VALID      — signal_type recognised, direction valid
    2. CIRCUIT_BREAKER   — daily PnL limit, consecutive-loss cooldown
    3. RISK_LIMITS       — SL/TP present & within bounds, R:R acceptable
    4. POSITION_CONFLICT — no duplicate slot, double-trade gate, netting
    5. CAPITAL_CHECK     — sufficient capital for the position size
    6. RATE_LIMIT        — minimum time between trades

Each check is a separate private async method for testability.
The gate is STATEFUL: it tracks daily PnL, consecutive losses, and last
trade time, resetting at UTC midnight.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any, Callable, Awaitable, Optional, Dict, TYPE_CHECKING

from src.contracts.decision_gate_contract import GateVerdict
from .signal_bus import Signal, SignalType

if TYPE_CHECKING:
    from src.config.protocol import ConfigProtocol
    from src.logger.logger import Logger
    from src.trading.data_models import Position


# ---------------------------------------------------------------------------
# Configuration defaults (used when config properties are missing)
# ---------------------------------------------------------------------------

_DEFAULT_DAILY_PNL_LIMIT: float = -3.0          # percent
_DEFAULT_CONSECUTIVE_LOSS_LIMIT: int = 3
_DEFAULT_CONSECUTIVE_LOSS_COOLDOWN: int = 7200   # seconds (2 h)
_DEFAULT_FAST_TRADE_MIN_INTERVAL: int = 900      # seconds (15 min)
_DEFAULT_SL_MIN_PCT: float = 0.5                 # 0.5 %
_DEFAULT_SL_MAX_PCT: float = 10.0                # 10 %
_DEFAULT_MIN_RR_RATIO: float = 1.0               # risk:reward >= 1:1

_VALID_DIRECTIONS = {"LONG", "SHORT"}
_VALID_CONFIDENCES = {"HIGH", "MEDIUM", "LOW"}


class DecisionGate:
    """Validation firewall that evaluates every Signal before execution.

    Injected into ``ExecutionEngine`` via constructor.  When the gate is
    ``None`` the engine falls back to its previous behaviour (no validation).

    The gate is **stateful**: it tracks daily realised PnL, consecutive
    losses, and the timestamp of the last opened trade.  State resets
    automatically at UTC midnight.
    """

    def __init__(
        self,
        logger: "Logger",
        config: "ConfigProtocol",
        get_positions: Callable[[], Dict[str, "Position"]],
        get_capital: Callable[[], Awaitable[float]],
    ) -> None:
        self.logger = logger
        self.config = config
        self._get_positions = get_positions
        self._get_capital = get_capital

        # ── Stateful counters (reset daily) ───────────────────────────
        self._daily_pnl_pct: float = 0.0
        self._consecutive_losses: int = 0
        self._cooldown_until: Optional[datetime] = None
        self._last_trade_time: Optional[datetime] = None
        self._state_date: Optional[datetime] = None  # UTC date of current state

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def evaluate(self, signal: Signal) -> GateVerdict:
        """Run the full check pipeline on *signal*.

        CLOSE and CANCEL signals receive only the SIGNAL_VALID check.
        OPEN (and UPDATE) signals go through the full pipeline.
        """
        self._maybe_reset_daily_state()

        # CLOSE / CANCEL — minimal validation, must always be allowed through
        if signal.signal_type in (SignalType.CLOSE, SignalType.CANCEL):
            rejections = self._check_signal_valid(signal, require_direction=False)
            verdict = GateVerdict(
                approved=len(rejections) == 0,
                signal=signal,
                rejections=rejections,
            )
            self._log_verdict(signal, verdict)
            return verdict

        # OPEN / UPDATE — full pipeline
        rejections: list[str] = []
        warnings: list[str] = []
        modified: dict[str, Any] = {}

        # 1. SIGNAL_VALID
        rejections.extend(self._check_signal_valid(signal))

        # 2. CIRCUIT_BREAKER
        rejections.extend(self._check_circuit_breaker())

        # 3. RISK_LIMITS (may auto-correct SL/TP → populates modified)
        risk_rej, risk_warn, risk_mod = self._check_risk_limits(signal)
        rejections.extend(risk_rej)
        warnings.extend(risk_warn)
        modified.update(risk_mod)

        # 4. POSITION_CONFLICT
        rejections.extend(self._check_position_conflict(signal))

        # 5. CAPITAL_CHECK
        cap_rej = await self._check_capital(signal)
        rejections.extend(cap_rej)

        # 6. RATE_LIMIT
        rejections.extend(self._check_rate_limit())

        verdict = GateVerdict(
            approved=len(rejections) == 0,
            signal=signal,
            rejections=rejections,
            warnings=warnings,
            modified_fields=modified,
        )
        self._log_verdict(signal, verdict)
        return verdict

    def record_trade_result(self, pnl_pct: float) -> None:
        """Record a closed trade's PnL for circuit-breaker tracking."""
        self._maybe_reset_daily_state()
        self._daily_pnl_pct += pnl_pct

        if pnl_pct < 0:
            self._consecutive_losses += 1
            cooldown_secs = self._cfg(
                "CONSECUTIVE_LOSS_COOLDOWN", _DEFAULT_CONSECUTIVE_LOSS_COOLDOWN
            )
            loss_limit = self._cfg(
                "CONSECUTIVE_LOSS_LIMIT", _DEFAULT_CONSECUTIVE_LOSS_LIMIT
            )
            if self._consecutive_losses >= loss_limit:
                self._cooldown_until = datetime.now(timezone.utc) + timedelta(
                    seconds=cooldown_secs
                )
                self.logger.warning(
                    "[Gate] Consecutive-loss cooldown activated: %d losses, "
                    "cooldown %ds",
                    self._consecutive_losses,
                    cooldown_secs,
                )
        else:
            # Winning trade resets the streak
            self._consecutive_losses = 0
            self._cooldown_until = None

        self.logger.info(
            "[Gate] Trade result recorded: PnL=%.2f%%, daily=%.2f%%, "
            "consecutive_losses=%d",
            pnl_pct,
            self._daily_pnl_pct,
            self._consecutive_losses,
        )

    def record_trade_opened(self) -> None:
        """Mark that a trade was just opened (for rate-limit tracking)."""
        self._last_trade_time = datetime.now(timezone.utc)

    def reset_daily_state(self) -> None:
        """Manually reset all daily counters."""
        self._daily_pnl_pct = 0.0
        self._consecutive_losses = 0
        self._cooldown_until = None
        self._last_trade_time = None
        self._state_date = datetime.now(timezone.utc).date()
        self.logger.info("[Gate] Daily state reset")

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-serialisable snapshot of the gate state."""
        return {
            "daily_pnl_pct": round(self._daily_pnl_pct, 4),
            "consecutive_losses": self._consecutive_losses,
            "cooldown_until": (
                self._cooldown_until.isoformat() if self._cooldown_until else None
            ),
            "last_trade_time": (
                self._last_trade_time.isoformat() if self._last_trade_time else None
            ),
            "state_date": str(self._state_date) if self._state_date else None,
        }

    # ------------------------------------------------------------------
    # Individual checks (private, one per gate)
    # ------------------------------------------------------------------

    def _check_signal_valid(
        self, signal: Signal, require_direction: bool = True
    ) -> list[str]:
        """1. SIGNAL_VALID — signal_type recognised, direction valid."""
        rejections: list[str] = []

        if signal.signal_type not in SignalType:
            rejections.append("SIGNAL_VALID: unrecognised signal_type")

        if require_direction:
            direction = signal.direction.upper() if signal.direction else ""
            if direction not in _VALID_DIRECTIONS:
                rejections.append(
                    f"SIGNAL_VALID: invalid direction '{signal.direction}'"
                )

        return rejections

    def _check_circuit_breaker(self) -> list[str]:
        """2. CIRCUIT_BREAKER — daily PnL limit, consecutive-loss cooldown."""
        rejections: list[str] = []

        # Daily PnL limit
        daily_limit = self._cfg("DAILY_PNL_LIMIT", _DEFAULT_DAILY_PNL_LIMIT)
        if self._daily_pnl_pct <= daily_limit:
            rejections.append(
                f"CIRCUIT_BREAKER: daily PnL {self._daily_pnl_pct:+.2f}% "
                f"<= limit {daily_limit}%"
            )

        # Consecutive-loss cooldown
        if self._cooldown_until is not None:
            now = datetime.now(timezone.utc)
            if now < self._cooldown_until:
                remaining = int((self._cooldown_until - now).total_seconds())
                rejections.append(
                    f"CIRCUIT_BREAKER: consecutive-loss cooldown active, "
                    f"{remaining}s remaining"
                )

        return rejections

    def _check_risk_limits(
        self, signal: Signal
    ) -> tuple[list[str], list[str], dict[str, Any]]:
        """3. RISK_LIMITS — SL/TP present & within bounds, R:R acceptable.

        Returns (rejections, warnings, modified_fields).
        May auto-clamp SL/TP and record the modification.
        """
        rejections: list[str] = []
        warnings: list[str] = []
        modified: dict[str, Any] = {}

        price = signal.price_at_signal
        if price <= 0:
            rejections.append("RISK_LIMITS: price_at_signal is zero or negative")
            return rejections, warnings, modified

        # --- Stop Loss ---
        sl = signal.stop_loss
        if sl <= 0:
            rejections.append("RISK_LIMITS: stop_loss is missing or zero")
        else:
            sl_pct = abs(price - sl) / price * 100
            sl_min = self._cfg("SL_MIN_PCT", _DEFAULT_SL_MIN_PCT)
            sl_max = self._cfg("SL_MAX_PCT", _DEFAULT_SL_MAX_PCT)

            if sl_pct < sl_min:
                warnings.append(
                    f"RISK_LIMITS: SL distance {sl_pct:.2f}% < min {sl_min}%"
                )
            if sl_pct > sl_max:
                # Auto-clamp SL to max distance
                if signal.direction.upper() == "LONG":
                    new_sl = price * (1 - sl_max / 100)
                else:
                    new_sl = price * (1 + sl_max / 100)
                signal.stop_loss = round(new_sl, 8)
                modified["stop_loss"] = {
                    "original": sl,
                    "clamped": signal.stop_loss,
                    "reason": f"SL distance {sl_pct:.2f}% > max {sl_max}%",
                }
                warnings.append(
                    f"RISK_LIMITS: SL auto-clamped from {sl:.8f} to "
                    f"{signal.stop_loss:.8f}"
                )

        # --- Take Profit ---
        tp = signal.take_profit
        if tp <= 0:
            rejections.append("RISK_LIMITS: take_profit is missing or zero")

        # --- Risk:Reward ratio ---
        if sl > 0 and tp > 0 and price > 0:
            sl_dist = abs(price - signal.stop_loss)
            tp_dist = abs(tp - price)
            if sl_dist > 0:
                rr = tp_dist / sl_dist
                min_rr = self._cfg("MIN_RR_RATIO", _DEFAULT_MIN_RR_RATIO)
                if rr < min_rr:
                    warnings.append(
                        f"RISK_LIMITS: R:R ratio {rr:.2f} < minimum {min_rr}"
                    )

        return rejections, warnings, modified

    def _check_position_conflict(self, signal: Signal) -> list[str]:
        """4. POSITION_CONFLICT — duplicate slot, double-trade, netting."""
        rejections: list[str] = []
        positions = self._get_positions()
        source = getattr(signal, "source", "ai") or "ai"

        existing = positions.get(source)
        if existing is not None:
            double_trade = self._cfg("DOUBLE_TRADE_ENABLED", False)
            if not double_trade:
                rejections.append(
                    f"POSITION_CONFLICT: slot '{source}' already occupied "
                    f"(double_trade disabled)"
                )
            else:
                # Netting check: same direction = rejected (would double exposure)
                if (
                    hasattr(existing, "direction")
                    and existing.direction.upper() == signal.direction.upper()
                ):
                    rejections.append(
                        f"POSITION_CONFLICT: netting — same direction "
                        f"'{signal.direction}' already open in slot '{source}'"
                    )

        return rejections

    async def _check_capital(self, signal: Signal) -> list[str]:
        """5. CAPITAL_CHECK — sufficient capital for the position size."""
        rejections: list[str] = []

        try:
            capital = await self._get_capital()
        except Exception as e:
            self.logger.warning("[Gate] Failed to fetch capital: %s", e)
            # Cannot verify — let it through with a warning (fail-open)
            return rejections

        if capital <= 0:
            rejections.append("CAPITAL_CHECK: capital is zero or negative")
            return rejections

        price = signal.price_at_signal
        size_pct = signal.position_size  # fraction 0.0-1.0
        if price > 0 and size_pct > 0:
            required = capital * size_pct
            fee_pct = self._cfg("TRANSACTION_FEE_PERCENT", 0.1)
            required_with_fees = required * (1 + fee_pct / 100)
            if required_with_fees > capital:
                rejections.append(
                    f"CAPITAL_CHECK: required ${required_with_fees:.2f} "
                    f"(incl. fees) > available ${capital:.2f}"
                )

        return rejections

    def _check_rate_limit(self) -> list[str]:
        """6. RATE_LIMIT — minimum time between trades."""
        rejections: list[str] = []

        if self._last_trade_time is not None:
            min_interval = self._cfg(
                "FAST_TRADE_MIN_INTERVAL", _DEFAULT_FAST_TRADE_MIN_INTERVAL
            )
            elapsed = (
                datetime.now(timezone.utc) - self._last_trade_time
            ).total_seconds()
            if elapsed < min_interval:
                remaining = int(min_interval - elapsed)
                rejections.append(
                    f"RATE_LIMIT: {remaining}s remaining "
                    f"(last trade {int(elapsed)}s ago, min interval {min_interval}s)"
                )

        return rejections

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _cfg(self, name: str, default: Any) -> Any:
        """Safely read a config property, falling back to *default*."""
        return getattr(self.config, name, default)

    def _maybe_reset_daily_state(self) -> None:
        """Auto-reset counters if the UTC date has changed."""
        today = datetime.now(timezone.utc).date()
        if self._state_date != today:
            self._daily_pnl_pct = 0.0
            self._consecutive_losses = 0
            self._cooldown_until = None
            self._last_trade_time = None
            self._state_date = today
            self.logger.info("[Gate] New UTC day — daily state auto-reset")

    def _log_verdict(self, signal: Signal, verdict: GateVerdict) -> None:
        """Log every verdict for observability."""
        status = "APPROVED" if verdict.approved else "REJECTED"
        self.logger.info(
            "[Gate] %s %s %s %s | rejections=%s | warnings=%s | modified=%s",
            status,
            signal.signal_type.name,
            signal.direction,
            signal.symbol,
            verdict.rejections or "none",
            verdict.warnings or "none",
            list(verdict.modified_fields.keys()) or "none",
        )
