"""Protocol definition for DecisionGate interface.

The DecisionGate sits between Layer 1 (AI Brain) and Layer 2 (Execution),
acting as a validation firewall that can approve or reject signals before
any order is placed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable, TYPE_CHECKING

if TYPE_CHECKING:
    from src.execution.signal_bus import Signal


@dataclass(slots=True, kw_only=True)
class GateVerdict:
    """Outcome of a DecisionGate evaluation.

    Attributes:
        approved: Whether the signal passed all checks.
        signal: The original (or auto-corrected) signal.
        rejections: Names of checks that hard-failed.
        warnings: Non-blocking advisory messages.
        modified_fields: Fields that were auto-corrected (e.g. SL clamped).
        timestamp: UTC timestamp of the verdict.
    """

    approved: bool
    signal: Any  # Signal — avoid import for slots compat
    rejections: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    modified_fields: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


@runtime_checkable
class DecisionGateProtocol(Protocol):
    """Structural contract for the DecisionGate.

    Any object satisfying this protocol can be injected into
    ExecutionEngine as the validation firewall.
    """

    async def evaluate(self, signal: "Signal") -> GateVerdict:
        """Evaluate a signal against all gate checks.

        Returns a GateVerdict indicating approval/rejection with details.
        """
        ...

    def record_trade_result(self, pnl_pct: float) -> None:
        """Record a closed trade's PnL for circuit-breaker state tracking.

        Args:
            pnl_pct: Realised PnL as a percentage (positive = profit).
        """
        ...

    def reset_daily_state(self) -> None:
        """Reset daily counters (called at UTC midnight or on demand)."""
        ...
