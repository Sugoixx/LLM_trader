"""Fast Trading Mode safety guards.

Runtime checks that block algo trades when conditions are unsafe:
  - Minimum interval between trades (anti-flip-flop)
  - Daily realised-loss limit (circuit breaker)
  - Consecutive-losses cooldown (losing streak pause)
  - Entry-quality metadata exposed to the dashboard

These are hard gates — evaluated BEFORE any order is placed.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Dict, Any

from src.logger.logger import Logger


@dataclass
class FastTradingConfig:
    """Tunable thresholds for the safety guards."""
    #: Minimum seconds between two fast trades on the same symbol.
    min_interval_seconds: int = 900  # 15 min

    #: Stop trading for the rest of the UTC day if realised PnL% <= this.
    daily_loss_pct_limit: float = -3.0

    #: Pause trading after N consecutive losing trades.
    consecutive_loss_threshold: int = 3

    #: Cooldown duration after consecutive-loss trip (seconds).
    consecutive_loss_cooldown_seconds: int = 7200  # 2h

    #: Minimum consensus confidence required for new fast entries.
    min_confidence: str = "MEDIUM"

    #: Minimum net reward/risk after fees required for new fast entries.
    min_rr_after_fees: float = 0.0

    #: Maximum allowed age for cached algo signals used to open entries.
    max_signal_age_seconds: int = 900


@dataclass
class GuardState:
    """Current state of the safety guards (for dashboard display)."""
    last_trade_utc: Optional[datetime] = None
    daily_pnl_pct: float = 0.0
    consecutive_losses: int = 0
    cooldown_until_utc: Optional[datetime] = None
    blocked_reason: Optional[str] = None
    # Manual override flags (set via reset_cooldown / admin endpoint)
    cooldown_reset_at_utc: Optional[datetime] = None
    # Last fast-mode consensus snapshot (for dashboard diagnostics)
    last_consensus: Optional[str] = None
    last_consensus_at_utc: Optional[datetime] = None
    recent_decisions: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class GuardCheckResult:
    """Outcome of a guard check."""
    allowed: bool
    reason: str = ""
    state: GuardState = field(default_factory=GuardState)


class FastTradingSafetyGuard:
    """Evaluates safety gates before a fast-mode trade is placed.

    Uses persistence trade history (read-only) to compute:
      - Minutes since last closed trade (min-interval)
      - Today's realised PnL % vs initial capital
      - Consecutive loss streak
    """

    #: Actions that represent a closed trade in history.
    CLOSE_ACTIONS = {"CLOSE", "CLOSE_LONG", "CLOSE_SHORT"}

    def __init__(
        self,
        logger: Logger,
        persistence,
        statistics_service,
        config: Optional[FastTradingConfig] = None,
    ):
        self.logger = logger
        self.persistence = persistence
        self.statistics_service = statistics_service
        self.config = config or FastTradingConfig()
        self._state = GuardState()

    @property
    def state(self) -> GuardState:
        """Last computed state (for dashboard / introspection)."""
        return self._state

    # ── Manual admin controls ─────────────────────────────────────────────
    def reset_cooldown(self) -> Dict[str, Any]:
        """Manually clear the consecutive-loss cooldown.

        Marks ``cooldown_reset_at_utc`` = now, forces the in-memory
        ``cooldown_until_utc`` and ``consecutive_losses`` to zero, and
        returns a snapshot so the caller can broadcast it. Subsequent
        ``_recompute_state`` calls preserve the override until a newer
        close-trade appears in history.
        """
        now = datetime.now(timezone.utc)
        self._state.cooldown_reset_at_utc = now
        self._state.cooldown_until_utc = None
        self._state.consecutive_losses = 0
        self._state.blocked_reason = None
        self.logger.warning(
            "[SafetyGuard] Cooldown manually reset at %s", now.isoformat()
        )
        return self.snapshot()

    def record_consensus(self, consensus: str) -> None:
        """Record the latest fast-mode consensus string for diagnostics."""
        self._state.last_consensus = consensus
        self._state.last_consensus_at_utc = datetime.now(timezone.utc)

    def record_decision(self, decision: Dict[str, Any]) -> None:
        """Append a detailed fast-mode decision snapshot for UI diagnostics."""
        if not isinstance(decision, dict):
            return
        entry = dict(decision)
        entry.setdefault("timestamp_utc", datetime.now(timezone.utc).isoformat())
        history = list(getattr(self._state, "recent_decisions", []) or [])
        history.insert(0, entry)
        self._state.recent_decisions = history[:20]

    def clear_recent_decisions(self) -> None:
        """Clear the in-memory fast-mode decision history (UI inspector only)."""
        self._state.recent_decisions = []

    def check(
        self,
        now: Optional[datetime] = None,
        has_open_position: bool = False,
    ) -> GuardCheckResult:
        """Evaluate all guards; returns (allowed, reason, state_snapshot).

        Args:
            now: Current UTC time (defaults to datetime.now(utc)).
            has_open_position: If True, we're closing/updating an existing
                               position — min-interval and daily-loss still
                               apply but consecutive-loss cooldown is bypassed
                               (we must be allowed to exit losing trades).
        """
        now = now or datetime.now(timezone.utc)
        self._state = self._recompute_state(now)

        # Active cooldown from prior consecutive-loss trip
        if not has_open_position and self._state.cooldown_until_utc:
            if now < self._state.cooldown_until_utc:
                remaining = int((self._state.cooldown_until_utc - now).total_seconds())
                reason = (
                    f"Consecutive-loss cooldown active — "
                    f"{remaining}s remaining (streak={self._state.consecutive_losses})"
                )
                self._state.blocked_reason = reason
                return GuardCheckResult(False, reason, self._state)

        # Daily loss circuit breaker
        if self._state.daily_pnl_pct <= self.config.daily_loss_pct_limit:
            reason = (
                f"Daily loss limit hit: {self._state.daily_pnl_pct:+.2f}% "
                f"≤ {self.config.daily_loss_pct_limit}% — trading paused for today"
            )
            self._state.blocked_reason = reason
            return GuardCheckResult(False, reason, self._state)

        # Min-interval between trades (only for new entries, not closes)
        if not has_open_position and self._state.last_trade_utc:
            elapsed = (now - self._state.last_trade_utc).total_seconds()
            if elapsed < self.config.min_interval_seconds:
                remaining = int(self.config.min_interval_seconds - elapsed)
                reason = (
                    f"Min interval: {remaining}s remaining "
                    f"(last trade {int(elapsed)}s ago, limit "
                    f"{self.config.min_interval_seconds}s)"
                )
                self._state.blocked_reason = reason
                return GuardCheckResult(False, reason, self._state)

        self._state.blocked_reason = None
        return GuardCheckResult(True, "all guards passed", self._state)

    # ── Internal ──────────────────────────────────────────────────────────

    def _recompute_state(self, now: datetime) -> GuardState:
        """Rebuild guard state from trade history."""
        state = GuardState()

        try:
            history = self.persistence.load_trade_history() or []
        except Exception as e:
            self.logger.warning("[SafetyGuard] Failed to load history: %s", e)
            history = []

        # --- Last fast-trade timestamp (any action) ---
        last_ts = None
        for row in reversed(history):
            if not self._is_fast_trade(row):
                continue
            ts_str = row.get("timestamp")
            if not ts_str:
                continue
            try:
                ts = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                last_ts = ts
                break
            except (ValueError, TypeError):
                continue
        state.last_trade_utc = last_ts

        # --- Today's closed-trade PnL % ---
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        daily_pnl_pct_sum = 0.0
        for row in history:
            if not self._is_fast_trade(row):
                continue
            if row.get("action") not in self.CLOSE_ACTIONS:
                continue
            ts_str = row.get("timestamp")
            if not ts_str:
                continue
            try:
                ts = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                continue
            if ts < today_start:
                continue
            pnl_pct = self._extract_pnl_pct(row)
            if pnl_pct is not None:
                daily_pnl_pct_sum += pnl_pct
        state.daily_pnl_pct = daily_pnl_pct_sum

        # --- Consecutive-loss streak ---
        streak = 0
        for row in reversed(history):
            if not self._is_fast_trade(row):
                continue
            if row.get("action") not in self.CLOSE_ACTIONS:
                continue
            pnl_pct = self._extract_pnl_pct(row)
            if pnl_pct is None:
                break
            if pnl_pct < 0:
                streak += 1
            else:
                break
        state.consecutive_losses = streak

        # --- Cooldown window when streak trips ---
        if streak >= self.config.consecutive_loss_threshold:
            # Anchor cooldown to the last loss timestamp
            last_close_ts = self._last_close_timestamp(history)
            if last_close_ts:
                state.cooldown_until_utc = last_close_ts + timedelta(
                    seconds=self.config.consecutive_loss_cooldown_seconds
                )

        # --- Carry over manual overrides + diagnostics from previous state ---
        reset_at = getattr(self._state, "cooldown_reset_at_utc", None)
        state.cooldown_reset_at_utc = reset_at
        state.last_consensus = getattr(self._state, "last_consensus", None)
        state.last_consensus_at_utc = getattr(self._state, "last_consensus_at_utc", None)
        state.recent_decisions = list(getattr(self._state, "recent_decisions", []) or [])

        # If user pressed "Reset Cooldown" AFTER the last recorded loss,
        # suppress the derived cooldown + streak until a new close appears.
        if reset_at is not None:
            last_close_ts = self._last_close_timestamp(history)
            if last_close_ts is None or reset_at >= last_close_ts:
                state.cooldown_until_utc = None
                state.consecutive_losses = 0

        return state

    @staticmethod
    def _is_fast_trade(row: Dict[str, Any]) -> bool:
        """Best-effort source filter for fast-mode history rows."""
        source = row.get("source")
        if source is not None:
            return str(source).lower() == "fast"

        # Backward compatibility for old rows written before the source field:
        # fast entries usually include the algo reasoning prefix.
        reasoning = row.get("reasoning", "")
        return isinstance(reasoning, str) and (
            "[Fast]" in reasoning or "Fast trader" in reasoning
        )

    @staticmethod
    def _extract_pnl_pct(row: Dict[str, Any]) -> Optional[float]:
        """Best-effort extraction of P&L% from a close-trade history row."""
        # Direct field if present
        for key in ("pnl_pct", "pnl_percent", "pnl_percentage"):
            if key in row and row[key] is not None:
                try:
                    return float(row[key])
                except (TypeError, ValueError):
                    pass
        # Parse from reasoning: "P&L: +1.23%"
        reasoning = row.get("reasoning", "")
        if isinstance(reasoning, str) and "P&L:" in reasoning:
            try:
                frag = reasoning.split("P&L:")[1].strip()
                num = frag.split("%")[0].strip().replace("+", "")
                return float(num)
            except (IndexError, ValueError):
                pass
        return None

    def _last_close_timestamp(
        self, history: List[Dict[str, Any]]
    ) -> Optional[datetime]:
        for row in reversed(history):
            if not self._is_fast_trade(row):
                continue
            if row.get("action") not in self.CLOSE_ACTIONS:
                continue
            ts_str = row.get("timestamp")
            if not ts_str:
                continue
            try:
                ts = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                return ts
            except (ValueError, TypeError):
                continue
        return None

    def snapshot(self) -> Dict[str, Any]:
        """Serialisable state snapshot for the dashboard."""
        s = self._state
        return {
            "last_trade_utc":       s.last_trade_utc.isoformat() if s.last_trade_utc else None,
            "daily_pnl_pct":        round(s.daily_pnl_pct, 3),
            "consecutive_losses":   s.consecutive_losses,
            "cooldown_until_utc":   s.cooldown_until_utc.isoformat() if s.cooldown_until_utc else None,
            "cooldown_reset_at_utc": s.cooldown_reset_at_utc.isoformat() if s.cooldown_reset_at_utc else None,
            "blocked_reason":       s.blocked_reason,
            "last_consensus":       s.last_consensus,
            "last_consensus_at_utc": s.last_consensus_at_utc.isoformat() if s.last_consensus_at_utc else None,
            "recent_decisions":    s.recent_decisions,
            "config": {
                "min_interval_seconds":              self.config.min_interval_seconds,
                "daily_loss_pct_limit":              self.config.daily_loss_pct_limit,
                "consecutive_loss_threshold":        self.config.consecutive_loss_threshold,
                "consecutive_loss_cooldown_seconds": self.config.consecutive_loss_cooldown_seconds,
                "min_confidence":                    self.config.min_confidence,
                "min_rr_after_fees":                  self.config.min_rr_after_fees,
                "max_signal_age_seconds":             self.config.max_signal_age_seconds,
            },
        }
