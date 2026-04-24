"""Execution Engine — Layer 2 orchestrator.

Runs as an independent asyncio task alongside the main bot loop.
Manages PriceStream, PositionMonitor, and Signal consumption.

Lifecycle:
    1. CompositionRoot creates ExecutionEngine and injects deps
    2. app.py starts engine as background task
    3. Engine opens WebSocket, registers position if one exists
    4. On each tick: PositionMonitor checks SL/TP/trailing/partial
    5. On signal from Layer 1: register/unregister position
    6. On shutdown: close WebSocket, clean up
"""

import asyncio
from typing import Optional, Any, TYPE_CHECKING

from src.logger.logger import Logger
from .signal_bus import SignalBus, Signal, SignalType
from .price_stream import PriceStream
from .position_monitor import PositionMonitor, TrailingStopState, PartialCloseState

if TYPE_CHECKING:
    from src.config.protocol import ConfigProtocol
    from src.contracts.decision_gate_contract import DecisionGateProtocol
    from src.trading.trading_strategy import TradingStrategy
    from src.trading.order_executor import OrderExecutorProtocol


class ExecutionEngine:
    """Layer 2 — real-time execution engine.

    Consumes signals from Layer 1 via SignalBus and monitors open
    positions with sub-second latency via WebSocket price streaming.
    """

    def __init__(
        self,
        logger: Logger,
        config: "ConfigProtocol",
        signal_bus: SignalBus,
        price_stream: PriceStream,
        position_monitor: PositionMonitor,
        trading_strategy: "TradingStrategy",
        decision_gate: Optional["DecisionGateProtocol"] = None,
    ):
        self.logger = logger
        self.config = config
        self.signal_bus = signal_bus
        self.price_stream = price_stream
        self.gate = decision_gate
        # Multi-slot monitors: the injected monitor becomes the 'ai' slot;
        # the 'fast' slot is lazily created on-demand (same deps, source tag).
        self.monitor = position_monitor  # legacy alias, treated as 'ai'
        self.monitor.source = "ai"
        self.monitors: dict = {"ai": position_monitor}
        self.strategy = trading_strategy

        self._running = False
        self._stream_task: Optional[asyncio.Task] = None
        self._signal_task: Optional[asyncio.Task] = None

        # Wire: monitor → strategy close (source-aware callback)
        self.monitor.set_close_callback(self._on_monitor_close)

        # Wire: price stream → each monitor's tick handler.
        # PriceStream.on_tick supports multiple callbacks; we also register
        # lazily-created monitors below.
        self.price_stream.on_tick(self.monitor.on_tick)

    def _get_or_create_monitor(self, source: str) -> PositionMonitor:
        """Lazy-create a PositionMonitor for the given slot (idempotent)."""
        if source in self.monitors:
            return self.monitors[source]
        mon = PositionMonitor(
            logger=self.logger,
            config=self.config,
            order_executor=self.strategy.order_executor,
            source=source,
        )
        mon.set_close_callback(self._on_monitor_close)
        self.price_stream.on_tick(mon.on_tick)
        self.monitors[source] = mon
        # Inherit dashboard state if the primary monitor has one
        ds = getattr(self.monitor, "_dashboard_state", None)
        if ds is not None:
            mon.set_dashboard_state(ds)
        return mon

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the engine as background tasks."""
        if self._running:
            return

        self._running = True
        self.logger.info("[Engine] Starting Layer 2 Execution Engine")

        # If strategy already has position(s), register each with its slot monitor
        self._register_all_positions()

        # Start WebSocket stream in background
        self._stream_task = asyncio.create_task(
            self.price_stream.start(), name="price_stream"
        )

        # Start signal consumer in background
        self._signal_task = asyncio.create_task(
            self._signal_consumer_loop(), name="signal_consumer"
        )

        self.logger.info("[Engine] Layer 2 running — real-time monitoring active")

    async def stop(self) -> None:
        """Stop the engine gracefully."""
        if not self._running:
            return

        self._running = False
        self.logger.info("[Engine] Stopping Layer 2 Execution Engine")

        # Stop price stream
        await self.price_stream.stop()

        # Cancel signal consumer
        if self._signal_task and not self._signal_task.done():
            self._signal_task.cancel()
            try:
                await self._signal_task
            except asyncio.CancelledError:
                pass

        # Wait for stream task
        if self._stream_task and not self._stream_task.done():
            self._stream_task.cancel()
            try:
                await self._stream_task
            except asyncio.CancelledError:
                pass

        self.logger.info("[Engine] Layer 2 stopped")

    async def close(self) -> None:
        """Full cleanup including exchange connection."""
        await self.stop()
        await self.price_stream.close()

    # ------------------------------------------------------------------
    # Signal consumer
    # ------------------------------------------------------------------

    async def _signal_consumer_loop(self) -> None:
        """Continuously consume signals from Layer 1."""
        while self._running:
            try:
                signal = await self.signal_bus.consume(timeout=1.0)
                if signal is None:
                    continue
                await self._handle_signal(signal)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error("[Engine] Signal consumer error: %s", e)
                await asyncio.sleep(1.0)

    async def _handle_signal(self, signal: Signal) -> None:
        """Process a signal from Layer 1."""
        self.logger.info(
            "[Engine] Signal received: %s %s %s (confidence=%s, rating=%s)",
            signal.signal_type.name,
            signal.direction,
            signal.symbol,
            signal.confidence,
            signal.rating,
        )

        # ── DecisionGate validation firewall ──────────────────────────
        if self.gate is not None:
            verdict = await self.gate.evaluate(signal)
            if not verdict.approved:
                self.logger.warning(
                    "[Engine] Signal REJECTED by DecisionGate: %s",
                    verdict.rejections,
                )
                return
            # Use the (possibly auto-corrected) signal from the verdict
            signal = verdict.signal

        if signal.signal_type == SignalType.OPEN:
            # Layer 1 opened a new position — register it for monitoring
            src = getattr(signal, "source", "ai") or "ai"
            pos = self.strategy.positions.get(src)
            if pos:
                self._register_position(src, pos)
                # Track trade open time for gate rate-limiting
                if self.gate is not None:
                    self.gate.record_trade_opened()

        elif signal.signal_type == SignalType.CLOSE:
            # Layer 1 requested a close — unregister the target slot
            src = getattr(signal, "source", "ai") or "ai"
            mon = self.monitors.get(src)
            if mon:
                mon.unregister_position()

        elif signal.signal_type == SignalType.UPDATE:
            # Layer 1 updated SL/TP — re-register with new levels
            src = getattr(signal, "source", "ai") or "ai"
            pos = self.strategy.positions.get(src)
            if pos:
                self._register_position(src, pos)

            # Push new SL/TP to the broker (MT5 journals / platform display)
            if signal.stop_loss and signal.take_profit and self.strategy.order_executor:
                try:
                    await self.strategy.order_executor.modify_position(
                        symbol=signal.symbol,
                        sl=signal.stop_loss,
                        tp=signal.take_profit,
                        source=src,
                    )
                except Exception as e:
                    self.logger.error(
                        "[Engine] Failed to push SL/TP update to broker for %s: %s",
                        signal.symbol,
                        e,
                    )

        elif signal.signal_type == SignalType.CANCEL:
            src = getattr(signal, "source", "ai") or "ai"
            mon = self.monitors.get(src)
            if mon:
                mon.unregister_position()

    # ------------------------------------------------------------------
    # Position registration
    # ------------------------------------------------------------------

    def _register_all_positions(self) -> None:
        """Register every open slot with its dedicated monitor."""
        for src, pos in self.strategy.positions.items():
            self._register_position(src, pos)

    def _register_position(self, source: str, pos) -> None:
        """Register a single slot's position with its monitor."""
        if not pos:
            return

        mon = self._get_or_create_monitor(source)

        # Build trailing stop config from position ATR
        trailing = None
        if self.config.EXECUTION_TRAILING_ENABLED:
            trailing = TrailingStopState(
                enabled=True,
                atr_multiplier=self.config.EXECUTION_TRAILING_ATR_MULT,
                atr_multiplier_after_tp1=self.config.EXECUTION_TRAILING_ATR_MULT_AFTER_TP1,
                atr_value=pos.atr_at_entry if pos.atr_at_entry > 0 else 0.0,
                breakeven_on_tp1=self.config.EXECUTION_TRAILING_BREAKEVEN,
            )
            # Disable if ATR is missing
            if trailing.atr_value <= 0:
                self.logger.warning(
                    "[Engine] [%s] ATR not available — trailing stop disabled",
                    source.upper(),
                )
                trailing = None

        # Build partial close config
        partial = None
        if self.config.EXECUTION_PARTIAL_ENABLED:
            targets = self._build_partial_targets(pos)
            if targets:
                partial = PartialCloseState(enabled=True, targets=targets)

        mon.register_position(pos, trailing, partial)

    # Legacy shim — kept for any lingering callers
    def _register_current_position(self) -> None:
        self._register_all_positions()

    def _build_partial_targets(self, pos) -> list:
        """Build partial close targets from config.

        Config format: list of (distance_fraction, close_fraction) tuples.
        distance_fraction = how far toward full TP (0.5 = halfway).
        close_fraction = what % of remaining position to close.
        """
        entry = pos.entry_price
        tp = pos.take_profit
        targets = []

        for idx, (dist_frac, close_frac) in enumerate(
            self.config.EXECUTION_PARTIAL_TARGETS, start=1
        ):
            if pos.direction == "LONG":
                tp_distance = tp - entry
                target_price = entry + (tp_distance * dist_frac)
            else:
                tp_distance = entry - tp
                target_price = entry - (tp_distance * dist_frac)

            label = f"TP{idx}"
            targets.append((target_price, close_frac, label))

        return targets

    # ------------------------------------------------------------------
    # Callback from PositionMonitor
    # ------------------------------------------------------------------

    async def _on_monitor_close(
        self, source: str, reason: str, price: float, quantity_closed: float
    ) -> None:
        """Called when a PositionMonitor closes its slot's position.

        Delegates to TradingStrategy._finalize_close for brain learning
        and persistence.  `source` identifies the 'ai' or 'fast' slot.
        """
        pos = self.strategy.positions.get(source)

        if reason.startswith("partial_"):
            # Partial close — position stays open, just update size
            self.logger.info(
                "[Engine] [%s] Partial close: %s @ $%.2f, qty=%.6f",
                source.upper(),
                reason,
                price,
                quantity_closed,
            )
            if pos:
                pos.size -= quantity_closed
                await self.strategy._persist_positions()
            return

        # Full close — delegate to strategy for brain learning + cleanup
        self.logger.info(
            "[Engine] [%s] Full close: %s @ $%.2f — delegating to TradingStrategy",
            source.upper(),
            reason,
            price,
        )

        if pos:
            conditions = self.strategy._build_conditions_from_position(pos)
            await self.strategy._finalize_close(
                reason, price, conditions, source=source
            )

        mon = self.monitors.get(source)
        if mon:
            mon.unregister_position()
