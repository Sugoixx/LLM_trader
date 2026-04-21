"""Tests for DecisionGate — validation firewall between Brain and Execution.

Covers each check individually, combined failures, CLOSE/CANCEL passthrough,
circuit-breaker state tracking, rate limiting, and backward compatibility.
"""

import asyncio
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

from src.execution.signal_bus import Signal, SignalType
from src.execution.decision_gate import DecisionGate
from src.contracts.decision_gate_contract import GateVerdict


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_config(**overrides):
    """Create a mock config with sensible defaults for gate testing."""
    cfg = MagicMock()
    defaults = {
        "DAILY_PNL_LIMIT": -3.0,
        "CONSECUTIVE_LOSS_LIMIT": 3,
        "CONSECUTIVE_LOSS_COOLDOWN": 7200,
        "FAST_TRADE_MIN_INTERVAL": 900,
        "DOUBLE_TRADE_ENABLED": False,
        "TRANSACTION_FEE_PERCENT": 0.1,
        "SL_MIN_PCT": 0.5,
        "SL_MAX_PCT": 10.0,
        "MIN_RR_RATIO": 1.0,
    }
    defaults.update(overrides)
    for k, v in defaults.items():
        setattr(cfg, k, v)
    return cfg


def _make_signal(**overrides) -> Signal:
    """Create a valid OPEN LONG signal with sensible defaults."""
    defaults = dict(
        signal_type=SignalType.OPEN,
        symbol="BTC/USDT",
        direction="LONG",
        confidence="HIGH",
        price_at_signal=50000.0,
        stop_loss=48000.0,       # 4% SL
        take_profit=55000.0,     # 10% TP → R:R = 2.5
        position_size=0.1,       # 10% of capital
        reasoning="test signal",
        rating="BUY",
        source="ai",
    )
    defaults.update(overrides)
    return Signal(**defaults)


def _make_gate(
    config=None,
    positions=None,
    capital=10000.0,
) -> DecisionGate:
    """Create a DecisionGate with mocked dependencies."""
    logger = MagicMock()
    cfg = config or _make_config()
    pos_dict = positions if positions is not None else {}
    get_positions = lambda: pos_dict
    get_capital = AsyncMock(return_value=capital)

    return DecisionGate(
        logger=logger,
        config=cfg,
        get_positions=get_positions,
        get_capital=get_capital,
    )


# ===========================================================================
# 1. SIGNAL_VALID
# ===========================================================================


class TestSignalValid:
    """Check 1: signal_type recognised, direction valid."""

    @pytest.mark.asyncio
    async def test_valid_open_long(self):
        gate = _make_gate()
        signal = _make_signal(direction="LONG")
        verdict = await gate.evaluate(signal)
        assert verdict.approved
        assert not verdict.rejections

    @pytest.mark.asyncio
    async def test_valid_open_short(self):
        gate = _make_gate()
        signal = _make_signal(direction="SHORT")
        verdict = await gate.evaluate(signal)
        assert verdict.approved

    @pytest.mark.asyncio
    async def test_invalid_direction_rejected(self):
        gate = _make_gate()
        signal = _make_signal(direction="SIDEWAYS")
        verdict = await gate.evaluate(signal)
        assert not verdict.approved
        assert any("SIGNAL_VALID" in r for r in verdict.rejections)

    @pytest.mark.asyncio
    async def test_empty_direction_rejected(self):
        gate = _make_gate()
        signal = _make_signal(direction="")
        verdict = await gate.evaluate(signal)
        assert not verdict.approved
        assert any("SIGNAL_VALID" in r for r in verdict.rejections)


# ===========================================================================
# 2. CIRCUIT_BREAKER
# ===========================================================================


class TestCircuitBreaker:
    """Check 2: daily PnL limit, consecutive-loss cooldown."""

    @pytest.mark.asyncio
    async def test_daily_pnl_within_limit_approved(self):
        gate = _make_gate()
        gate.record_trade_result(-1.0)  # -1% — within -3% limit
        signal = _make_signal()
        verdict = await gate.evaluate(signal)
        assert verdict.approved

    @pytest.mark.asyncio
    async def test_daily_pnl_exceeded_rejected(self):
        gate = _make_gate()
        gate.record_trade_result(-2.0)
        gate.record_trade_result(-1.5)  # cumulative -3.5% > -3% limit
        signal = _make_signal()
        verdict = await gate.evaluate(signal)
        assert not verdict.approved
        assert any("CIRCUIT_BREAKER" in r and "daily PnL" in r for r in verdict.rejections)

    @pytest.mark.asyncio
    async def test_consecutive_losses_trigger_cooldown(self):
        gate = _make_gate()
        # 3 consecutive losses → cooldown activated
        gate.record_trade_result(-0.5)
        gate.record_trade_result(-0.5)
        gate.record_trade_result(-0.5)
        signal = _make_signal()
        verdict = await gate.evaluate(signal)
        assert not verdict.approved
        assert any("CIRCUIT_BREAKER" in r and "cooldown" in r for r in verdict.rejections)

    @pytest.mark.asyncio
    async def test_winning_trade_resets_streak(self):
        gate = _make_gate()
        gate.record_trade_result(-0.5)
        gate.record_trade_result(-0.5)
        gate.record_trade_result(2.0)  # Win resets streak
        gate.record_trade_result(-0.5)
        signal = _make_signal()
        verdict = await gate.evaluate(signal)
        # Only 1 consecutive loss after the win — should be approved
        assert verdict.approved

    @pytest.mark.asyncio
    async def test_cooldown_expires(self):
        gate = _make_gate(config=_make_config(CONSECUTIVE_LOSS_COOLDOWN=1))
        gate.record_trade_result(-0.5)
        gate.record_trade_result(-0.5)
        gate.record_trade_result(-0.5)
        # Force cooldown to be in the past
        gate._cooldown_until = datetime.now(timezone.utc) - timedelta(seconds=10)
        signal = _make_signal()
        verdict = await gate.evaluate(signal)
        # Cooldown expired — should pass circuit breaker
        # (may still fail daily PnL if cumulative is too negative)
        cb_rejections = [r for r in verdict.rejections if "cooldown" in r]
        assert not cb_rejections


# ===========================================================================
# 3. RISK_LIMITS
# ===========================================================================


class TestRiskLimits:
    """Check 3: SL/TP present & within bounds, R:R acceptable."""

    @pytest.mark.asyncio
    async def test_valid_sl_tp_approved(self):
        gate = _make_gate()
        signal = _make_signal()
        verdict = await gate.evaluate(signal)
        assert verdict.approved

    @pytest.mark.asyncio
    async def test_missing_sl_rejected(self):
        gate = _make_gate()
        signal = _make_signal(stop_loss=0.0)
        verdict = await gate.evaluate(signal)
        assert not verdict.approved
        assert any("RISK_LIMITS" in r and "stop_loss" in r for r in verdict.rejections)

    @pytest.mark.asyncio
    async def test_missing_tp_rejected(self):
        gate = _make_gate()
        signal = _make_signal(take_profit=0.0)
        verdict = await gate.evaluate(signal)
        assert not verdict.approved
        assert any("RISK_LIMITS" in r and "take_profit" in r for r in verdict.rejections)

    @pytest.mark.asyncio
    async def test_zero_price_rejected(self):
        gate = _make_gate()
        signal = _make_signal(price_at_signal=0.0)
        verdict = await gate.evaluate(signal)
        assert not verdict.approved
        assert any("RISK_LIMITS" in r and "price_at_signal" in r for r in verdict.rejections)

    @pytest.mark.asyncio
    async def test_sl_too_wide_auto_clamped(self):
        gate = _make_gate()
        # SL at 20% distance — exceeds 10% max
        signal = _make_signal(
            price_at_signal=50000.0,
            stop_loss=40000.0,  # 20% away
            take_profit=60000.0,
        )
        verdict = await gate.evaluate(signal)
        assert verdict.approved
        assert "stop_loss" in verdict.modified_fields
        # SL should be clamped to 10% distance for LONG: 50000 * 0.9 = 45000
        assert signal.stop_loss == pytest.approx(45000.0, rel=1e-4)

    @pytest.mark.asyncio
    async def test_sl_too_tight_warning(self):
        gate = _make_gate()
        # SL at 0.1% distance — below 0.5% min
        signal = _make_signal(
            price_at_signal=50000.0,
            stop_loss=49950.0,  # 0.1% away
            take_profit=55000.0,
        )
        verdict = await gate.evaluate(signal)
        # Should still approve but with a warning
        assert verdict.approved
        assert any("SL distance" in w for w in verdict.warnings)

    @pytest.mark.asyncio
    async def test_low_rr_ratio_warning(self):
        gate = _make_gate()
        # R:R < 1.0
        signal = _make_signal(
            price_at_signal=50000.0,
            stop_loss=48000.0,   # 2000 risk
            take_profit=51000.0,  # 1000 reward → R:R = 0.5
        )
        verdict = await gate.evaluate(signal)
        assert verdict.approved  # Warning, not rejection
        assert any("R:R ratio" in w for w in verdict.warnings)

    @pytest.mark.asyncio
    async def test_short_sl_clamped_correctly(self):
        gate = _make_gate()
        # SHORT with SL 20% above entry
        signal = _make_signal(
            direction="SHORT",
            price_at_signal=50000.0,
            stop_loss=60000.0,  # 20% above
            take_profit=40000.0,
        )
        verdict = await gate.evaluate(signal)
        assert "stop_loss" in verdict.modified_fields
        # For SHORT, clamped SL = 50000 * 1.10 = 55000
        assert signal.stop_loss == pytest.approx(55000.0, rel=1e-4)


# ===========================================================================
# 4. POSITION_CONFLICT
# ===========================================================================


class TestPositionConflict:
    """Check 4: duplicate slot, double-trade gate, netting."""

    @pytest.mark.asyncio
    async def test_no_existing_position_approved(self):
        gate = _make_gate(positions={})
        signal = _make_signal()
        verdict = await gate.evaluate(signal)
        assert verdict.approved

    @pytest.mark.asyncio
    async def test_existing_position_double_trade_disabled_rejected(self):
        existing = MagicMock()
        existing.direction = "LONG"
        gate = _make_gate(
            positions={"ai": existing},
            config=_make_config(DOUBLE_TRADE_ENABLED=False),
        )
        signal = _make_signal(source="ai")
        verdict = await gate.evaluate(signal)
        assert not verdict.approved
        assert any("POSITION_CONFLICT" in r for r in verdict.rejections)

    @pytest.mark.asyncio
    async def test_existing_position_double_trade_enabled_opposite_direction(self):
        existing = MagicMock()
        existing.direction = "LONG"
        gate = _make_gate(
            positions={"ai": existing},
            config=_make_config(DOUBLE_TRADE_ENABLED=True),
        )
        signal = _make_signal(source="ai", direction="SHORT")
        verdict = await gate.evaluate(signal)
        # Opposite direction with double_trade enabled → allowed
        conflict_rejections = [r for r in verdict.rejections if "POSITION_CONFLICT" in r]
        assert not conflict_rejections

    @pytest.mark.asyncio
    async def test_netting_same_direction_rejected(self):
        existing = MagicMock()
        existing.direction = "LONG"
        gate = _make_gate(
            positions={"ai": existing},
            config=_make_config(DOUBLE_TRADE_ENABLED=True),
        )
        signal = _make_signal(source="ai", direction="LONG")
        verdict = await gate.evaluate(signal)
        assert not verdict.approved
        assert any("netting" in r for r in verdict.rejections)

    @pytest.mark.asyncio
    async def test_different_slot_no_conflict(self):
        existing = MagicMock()
        existing.direction = "LONG"
        gate = _make_gate(positions={"fast": existing})
        signal = _make_signal(source="ai")
        verdict = await gate.evaluate(signal)
        # Different slot — no conflict
        conflict_rejections = [r for r in verdict.rejections if "POSITION_CONFLICT" in r]
        assert not conflict_rejections


# ===========================================================================
# 5. CAPITAL_CHECK
# ===========================================================================


class TestCapitalCheck:
    """Check 5: sufficient capital for the position size."""

    @pytest.mark.asyncio
    async def test_sufficient_capital_approved(self):
        gate = _make_gate(capital=10000.0)
        signal = _make_signal(position_size=0.1)  # 10% of 10k = 1000
        verdict = await gate.evaluate(signal)
        assert verdict.approved

    @pytest.mark.asyncio
    async def test_insufficient_capital_rejected(self):
        gate = _make_gate(capital=100.0)
        signal = _make_signal(position_size=1.1)  # 110% of 100 = 110 > 100
        verdict = await gate.evaluate(signal)
        assert not verdict.approved
        assert any("CAPITAL_CHECK" in r for r in verdict.rejections)

    @pytest.mark.asyncio
    async def test_zero_capital_rejected(self):
        gate = _make_gate(capital=0.0)
        signal = _make_signal(position_size=0.1)
        verdict = await gate.evaluate(signal)
        assert not verdict.approved
        assert any("CAPITAL_CHECK" in r for r in verdict.rejections)

    @pytest.mark.asyncio
    async def test_capital_fetch_failure_passes_through(self):
        """If capital fetch fails, gate should fail-open (not reject)."""
        gate = _make_gate(capital=10000.0)
        gate._get_capital = AsyncMock(side_effect=Exception("network error"))
        signal = _make_signal()
        verdict = await gate.evaluate(signal)
        # Should not have CAPITAL_CHECK rejection
        cap_rejections = [r for r in verdict.rejections if "CAPITAL_CHECK" in r]
        assert not cap_rejections


# ===========================================================================
# 6. RATE_LIMIT
# ===========================================================================


class TestRateLimit:
    """Check 6: minimum time between trades."""

    @pytest.mark.asyncio
    async def test_no_previous_trade_approved(self):
        gate = _make_gate()
        signal = _make_signal()
        verdict = await gate.evaluate(signal)
        assert verdict.approved

    @pytest.mark.asyncio
    async def test_trade_too_soon_rejected(self):
        gate = _make_gate(config=_make_config(FAST_TRADE_MIN_INTERVAL=900))
        # Initialise state date so _maybe_reset_daily_state doesn't clear it
        gate._state_date = datetime.now(timezone.utc).date()
        gate._last_trade_time = datetime.now(timezone.utc) - timedelta(seconds=60)
        signal = _make_signal()
        verdict = await gate.evaluate(signal)
        assert not verdict.approved
        assert any("RATE_LIMIT" in r for r in verdict.rejections)

    @pytest.mark.asyncio
    async def test_trade_after_interval_approved(self):
        gate = _make_gate(config=_make_config(FAST_TRADE_MIN_INTERVAL=900))
        gate._last_trade_time = datetime.now(timezone.utc) - timedelta(seconds=1000)
        signal = _make_signal()
        verdict = await gate.evaluate(signal)
        # Rate limit should pass
        rate_rejections = [r for r in verdict.rejections if "RATE_LIMIT" in r]
        assert not rate_rejections

    @pytest.mark.asyncio
    async def test_record_trade_opened_updates_time(self):
        gate = _make_gate()
        assert gate._last_trade_time is None
        gate.record_trade_opened()
        assert gate._last_trade_time is not None


# ===========================================================================
# CLOSE / CANCEL passthrough
# ===========================================================================


class TestCloseAndCancelPassthrough:
    """CLOSE and CANCEL signals should pass with minimal checks."""

    @pytest.mark.asyncio
    async def test_close_signal_approved(self):
        gate = _make_gate()
        signal = _make_signal(signal_type=SignalType.CLOSE, direction="")
        verdict = await gate.evaluate(signal)
        assert verdict.approved

    @pytest.mark.asyncio
    async def test_cancel_signal_approved(self):
        gate = _make_gate()
        signal = _make_signal(signal_type=SignalType.CANCEL, direction="")
        verdict = await gate.evaluate(signal)
        assert verdict.approved

    @pytest.mark.asyncio
    async def test_close_bypasses_circuit_breaker(self):
        """Even with daily PnL exceeded, CLOSE must go through."""
        gate = _make_gate()
        gate.record_trade_result(-5.0)  # Way past -3% limit
        signal = _make_signal(signal_type=SignalType.CLOSE, direction="")
        verdict = await gate.evaluate(signal)
        assert verdict.approved

    @pytest.mark.asyncio
    async def test_close_bypasses_rate_limit(self):
        gate = _make_gate()
        gate._last_trade_time = datetime.now(timezone.utc)
        signal = _make_signal(signal_type=SignalType.CLOSE, direction="")
        verdict = await gate.evaluate(signal)
        assert verdict.approved


# ===========================================================================
# Combined checks
# ===========================================================================


class TestCombinedChecks:
    """Multiple failures should all be reported."""

    @pytest.mark.asyncio
    async def test_multiple_rejections_collected(self):
        gate = _make_gate(capital=0.0)
        gate.record_trade_result(-5.0)  # Circuit breaker
        gate._last_trade_time = datetime.now(timezone.utc)  # Rate limit
        signal = _make_signal(
            direction="INVALID",  # Signal valid
            stop_loss=0.0,        # Risk limits
        )
        verdict = await gate.evaluate(signal)
        assert not verdict.approved
        # Should have at least 3 different check categories
        categories = set()
        for r in verdict.rejections:
            cat = r.split(":")[0]
            categories.add(cat)
        assert len(categories) >= 3


# ===========================================================================
# Daily state reset
# ===========================================================================


class TestDailyStateReset:
    """State should auto-reset on UTC date change."""

    @pytest.mark.asyncio
    async def test_auto_reset_on_new_day(self):
        gate = _make_gate()
        gate.record_trade_result(-5.0)
        # Force state_date to yesterday
        gate._state_date = (datetime.now(timezone.utc) - timedelta(days=1)).date()
        signal = _make_signal()
        verdict = await gate.evaluate(signal)
        # Daily PnL should have been reset — no circuit breaker rejection
        cb_daily = [r for r in verdict.rejections if "daily PnL" in r]
        assert not cb_daily

    def test_manual_reset(self):
        gate = _make_gate()
        gate.record_trade_result(-5.0)
        gate._last_trade_time = datetime.now(timezone.utc)
        gate.reset_daily_state()
        assert gate._daily_pnl_pct == 0.0
        assert gate._consecutive_losses == 0
        assert gate._cooldown_until is None
        assert gate._last_trade_time is None

    def test_snapshot(self):
        gate = _make_gate()
        gate.record_trade_result(-1.5)
        snap = gate.snapshot()
        assert snap["daily_pnl_pct"] == pytest.approx(-1.5, abs=0.01)
        assert snap["consecutive_losses"] == 1
        assert "state_date" in snap


# ===========================================================================
# GateVerdict dataclass
# ===========================================================================


class TestGateVerdict:
    """Verify GateVerdict structure."""

    def test_approved_verdict(self):
        signal = _make_signal()
        v = GateVerdict(approved=True, signal=signal)
        assert v.approved
        assert v.rejections == []
        assert v.warnings == []
        assert v.modified_fields == {}
        assert v.timestamp is not None

    def test_rejected_verdict(self):
        signal = _make_signal()
        v = GateVerdict(
            approved=False,
            signal=signal,
            rejections=["SIGNAL_VALID: bad direction"],
        )
        assert not v.approved
        assert len(v.rejections) == 1


# ===========================================================================
# Backward compatibility — gate=None in ExecutionEngine
# ===========================================================================


class TestBackwardCompatibility:
    """ExecutionEngine should work when decision_gate=None."""

    @pytest.mark.asyncio
    async def test_engine_works_without_gate(self):
        """Verify ExecutionEngine.__init__ accepts gate=None and _handle_signal works."""
        from src.execution.execution_engine import ExecutionEngine

        logger = MagicMock()
        config = _make_config(
            EXECUTION_TRAILING_ENABLED=False,
            EXECUTION_PARTIAL_ENABLED=False,
        )
        signal_bus = MagicMock()
        price_stream = MagicMock()
        position_monitor = MagicMock()
        position_monitor.source = "ai"
        trading_strategy = MagicMock()
        trading_strategy.positions = {}

        engine = ExecutionEngine(
            logger=logger,
            config=config,
            signal_bus=signal_bus,
            price_stream=price_stream,
            position_monitor=position_monitor,
            trading_strategy=trading_strategy,
            decision_gate=None,
        )
        assert engine.gate is None

        # _handle_signal should work without gate
        signal = _make_signal(signal_type=SignalType.CLOSE, direction="")
        await engine._handle_signal(signal)
        # No exception = success

    @pytest.mark.asyncio
    async def test_engine_works_with_gate(self):
        """Verify ExecutionEngine uses gate when provided."""
        from src.execution.execution_engine import ExecutionEngine

        logger = MagicMock()
        config = _make_config(
            EXECUTION_TRAILING_ENABLED=False,
            EXECUTION_PARTIAL_ENABLED=False,
        )
        signal_bus = MagicMock()
        price_stream = MagicMock()
        position_monitor = MagicMock()
        position_monitor.source = "ai"
        trading_strategy = MagicMock()
        trading_strategy.positions = {}

        gate = _make_gate()
        engine = ExecutionEngine(
            logger=logger,
            config=config,
            signal_bus=signal_bus,
            price_stream=price_stream,
            position_monitor=position_monitor,
            trading_strategy=trading_strategy,
            decision_gate=gate,
        )
        assert engine.gate is gate

        # OPEN signal with valid params should be approved
        signal = _make_signal()
        await engine._handle_signal(signal)
        # Gate should have been called (check logger was invoked with [Gate])
        assert any(
            "[Gate]" in str(call) for call in gate.logger.info.call_args_list
        )
