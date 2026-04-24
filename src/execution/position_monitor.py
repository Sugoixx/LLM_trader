"""Position Monitor — real-time SL/TP check, trailing stop, partial close.

Called on every price tick from PriceStream.  Replaces the candle-close-only
SL/TP logic with sub-second monitoring.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List, TYPE_CHECKING

from src.logger.logger import Logger

if TYPE_CHECKING:
    from src.trading.data_models import Position
    from src.trading.order_executor import OrderExecutorProtocol
    from src.config.protocol import ConfigProtocol


@dataclass(slots=True, kw_only=True)
class TrailingStopState:
    """Runtime state for trailing stop on the active position."""

    enabled: bool = False
    atr_multiplier: float = 2.0  # SL trails at entry_ATR * multiplier below peak
    atr_multiplier_after_tp1: float = 0.2  # Tighter multiplier after breakeven
    highest_price: float = 0.0  # LONG: peak since entry
    lowest_price: float = float("inf")  # SHORT: trough since entry
    trailing_sl: float = 0.0  # Current trailing stop level
    atr_value: float = 0.0  # ATR at position entry
    # Breakeven mode: move SL to entry price once first partial TP is hit
    breakeven_on_tp1: bool = False
    breakeven_activated: bool = False
    entry_price: float = 0.0  # Stored for breakeven reference


@dataclass(slots=True, kw_only=True)
class PartialCloseState:
    """Runtime state for multi-target partial position closing."""

    enabled: bool = False
    # Each target: (price_level, fraction_of_remaining, label)
    targets: List[tuple] = field(default_factory=list)
    # Track which targets have been hit (by index)
    hit_indices: List[int] = field(default_factory=list)
    original_quantity: float = 0.0
    remaining_quantity: float = 0.0


class PositionMonitor:
    """Monitors the active position on every tick.

    Responsibilities:
        1. Real-time SL/TP detection (replaces candle-close polling)
        2. Trailing stop — tightens SL as price moves favorably
        3. Partial close — closes fractions at TP1, TP2, etc.
        4. MAE/MFE metric updates
    """

    def __init__(
        self,
        logger: Logger,
        config: "ConfigProtocol",
        order_executor: "OrderExecutorProtocol",
        source: str = "ai",
    ):
        self.logger = logger
        self.config = config
        self.order_executor = order_executor
        # Multi-slot tag: 'ai' or 'fast'. Injected into close callbacks.
        self.source = source

        # Mutable state — set when a position is registered
        self._position: Optional["Position"] = None
        self._trailing: Optional[TrailingStopState] = None
        self._partial: Optional[PartialCloseState] = None

        # Callback invoked when position is closed by the monitor
        # Signature: async callback(source: str, reason: str, price: float, quantity_closed: float)
        self._close_callback = None

        # Optional dashboard state for real-time snapshot broadcasting
        self._dashboard_state = None
        self._snapshot_interval = 5  # Push snapshot every N ticks
        self._tick_counter = 0

        # Debounce: prevent multiple close triggers on the same tick
        self._close_in_progress = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_dashboard_state(self, dashboard_state) -> None:
        """Inject dashboard state for real-time snapshot broadcasting."""
        self._dashboard_state = dashboard_state

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def register_position(
        self,
        position: "Position",
        trailing_config: Optional[TrailingStopState] = None,
        partial_config: Optional[PartialCloseState] = None,
    ) -> None:
        """Attach a position for real-time monitoring."""
        self._position = position
        # Sync source tag with the position's source (ai/fast) when available
        pos_source = getattr(position, "source", None)
        if pos_source:
            self.source = pos_source
        self._close_in_progress = False

        # Trailing stop
        if trailing_config and trailing_config.enabled:
            self._trailing = trailing_config
            self._trailing.highest_price = position.entry_price
            self._trailing.lowest_price = position.entry_price
            self._trailing.trailing_sl = position.stop_loss  # Start at fixed SL
            self._trailing.entry_price = position.entry_price
            self.logger.info(
                "[Monitor] Trailing stop enabled: ATR=%.2f, multiplier=%.1f (→%.1f after TP1), breakeven_on_tp1=%s",
                self._trailing.atr_value,
                self._trailing.atr_multiplier,
                self._trailing.atr_multiplier_after_tp1,
                self._trailing.breakeven_on_tp1,
            )
        else:
            self._trailing = None

        # Partial close
        if partial_config and partial_config.enabled and partial_config.targets:
            self._partial = partial_config
            self._partial.original_quantity = position.size
            self._partial.remaining_quantity = position.size
            self._partial.hit_indices = []
            self.logger.info(
                "[Monitor] Partial close enabled: %d targets",
                len(self._partial.targets),
            )
        else:
            self._partial = None

        self.logger.info(
            "[Monitor] Watching %s %s @ $%.2f  SL=$%.2f  TP=$%.2f",
            position.direction,
            position.symbol,
            position.entry_price,
            position.stop_loss,
            position.take_profit,
        )

    def unregister_position(self) -> None:
        """Detach the position (called after close)."""
        self._position = None
        self._trailing = None
        self._partial = None
        self._close_in_progress = False

    def set_close_callback(self, callback) -> None:
        """Set async callback(source, reason, price, qty_closed) for position close events."""
        self._close_callback = callback

    @property
    def has_position(self) -> bool:
        return self._position is not None

    # ------------------------------------------------------------------
    # Tick handler — called by PriceStream on every tick
    # ------------------------------------------------------------------

    async def on_tick(self, symbol: str, price: float, ticker: Dict[str, Any]) -> None:
        """Process a price tick.  This is the hot path — keep it fast."""
        if not self._position or self._close_in_progress:
            return

        if symbol != self._position.symbol:
            return

        # 1. Update MAE/MFE
        self._position.update_metrics(price)

        # 2. Check partial take-profit targets (before full TP)
        if self._partial:
            await self._check_partial_targets(price)

        # 3. Update trailing stop
        effective_sl = self._position.stop_loss
        trailing_sl_changed = False
        if self._trailing and self._trailing.enabled:
            trailing_sl_changed = self._update_trailing(price)
            effective_sl = self._trailing.trailing_sl

        # 3a. Push updated trailing SL to broker (MT5 journals)
        if trailing_sl_changed:
            try:
                await self.order_executor.modify_position(
                    symbol=self._position.symbol,
                    sl=self._trailing.trailing_sl,
                    tp=self._position.take_profit,
                    source=self.source,
                )
            except Exception as exc:
                self.logger.warning("[Trail] Broker SL update failed: %s", exc)

        # 4. Check hard stop loss (fixed or trailing)
        if self._is_sl_hit(price, effective_sl):
            await self._trigger_close("stop_loss", price, self._get_remaining_qty())
            return

        # 5. Check full take profit (only if partial close didn't already close 100%)
        if self._is_tp_hit(price):
            await self._trigger_close("take_profit", price, self._get_remaining_qty())
            return

        # 6. Push snapshot to dashboard periodically
        self._tick_counter += 1
        if self._dashboard_state and self._tick_counter % self._snapshot_interval == 0:
            await self._push_dashboard_snapshot(price, effective_sl)

    # ------------------------------------------------------------------
    # Trailing stop logic
    # ------------------------------------------------------------------

    def _update_trailing(self, price: float) -> bool:
        """Ratchet the trailing stop based on price movement.

        Returns:
            True if the trailing SL was raised (change needs pushing to broker).
        """
        ts = self._trailing
        pos = self._position

        if pos.direction == "LONG":
            if price > ts.highest_price:
                ts.highest_price = price
                # New trailing SL = peak - ATR * multiplier
                new_sl = ts.highest_price - (ts.atr_value * ts.atr_multiplier)
                if new_sl > ts.trailing_sl:
                    ts.trailing_sl = new_sl
                    self.logger.debug(
                        "[Trail] New SL $%.2f (peak $%.2f)", new_sl, ts.highest_price
                    )
                    return True
        else:  # SHORT
            if price < ts.lowest_price:
                ts.lowest_price = price
                new_sl = ts.lowest_price + (ts.atr_value * ts.atr_multiplier)
                if new_sl < ts.trailing_sl:
                    ts.trailing_sl = new_sl
                    self.logger.debug(
                        "[Trail] New SL $%.2f (trough $%.2f)", new_sl, ts.lowest_price
                    )
                    return True
        return False

    # ------------------------------------------------------------------
    # Partial close logic
    # ------------------------------------------------------------------

    async def _check_partial_targets(self, price: float) -> None:
        """Check if any partial take-profit levels have been hit."""
        pc = self._partial
        pos = self._position
        if not pc or not pos:
            return

        for idx, (target_price, fraction, label) in enumerate(pc.targets):
            if idx in pc.hit_indices:
                continue

            hit = (pos.direction == "LONG" and price >= target_price) or (
                pos.direction == "SHORT" and price <= target_price
            )
            if not hit:
                continue

            # Calculate quantity to close
            qty_to_close = pc.remaining_quantity * fraction
            if qty_to_close <= 0:
                continue

            pc.hit_indices.append(idx)
            pc.remaining_quantity -= qty_to_close

            self.logger.info(
                "[Partial] %s hit @ $%.2f — closing %.6f (%.0f%% of remaining)",
                label,
                price,
                qty_to_close,
                fraction * 100,
            )

            # Place partial close order
            close_side = "sell" if pos.direction == "LONG" else "buy"
            order_type = getattr(self.config, "LIVE_ORDER_TYPE", "limit")
            result = await self.order_executor.close_order(
                symbol=pos.symbol,
                side=close_side,
                quantity=qty_to_close,
                price=price,
                order_type=order_type,
                source=self.source,
            )
            if not result.success:
                if getattr(result, "already_closed", False):
                    # Broker has no open position — user (or broker SL/TP) closed it externally.
                    # Stop monitoring and delegate cleanup to the strategy.
                    self.logger.warning(
                        "[Partial] %s on %s — broker reports no open position; finalizing close.",
                        label,
                        pos.symbol,
                    )
                    await self._handle_external_close(price)
                    return
                self.logger.error("[Partial] Order failed: %s", result.error)
                # Revert tracking
                pc.hit_indices.remove(idx)
                pc.remaining_quantity += qty_to_close

            # Notify via callback
            if self._close_callback and result.success:
                await self._close_callback(
                    self.source, f"partial_{label}", price, qty_to_close
                )

            # Breakeven mode: move trailing SL to entry price after first partial hit
            if (
                result.success
                and self._trailing
                and self._trailing.breakeven_on_tp1
                and not self._trailing.breakeven_activated
            ):
                old_sl = self._trailing.trailing_sl
                old_mult = self._trailing.atr_multiplier
                self._trailing.trailing_sl = max(
                    self._trailing.trailing_sl, self._trailing.entry_price
                )
                self._trailing.atr_multiplier = self._trailing.atr_multiplier_after_tp1
                self._trailing.breakeven_activated = True
                self.logger.info(
                    "[Trail] Breakeven activated after %s: SL $%.2f → $%.2f (entry), ATR mult %.1f → %.1f",
                    label,
                    old_sl,
                    self._trailing.trailing_sl,
                    old_mult,
                    self._trailing.atr_multiplier,
                )
                self._trailing.breakeven_activated = True
                self.logger.info(
                    "[Trail] Breakeven activated after %s: SL $%.2f → $%.2f (entry)",
                    label,
                    old_sl,
                    self._trailing.trailing_sl,
                )

    # ------------------------------------------------------------------
    # Full close helpers
    # ------------------------------------------------------------------

    def _is_sl_hit(self, price: float, effective_sl: float) -> bool:
        pos = self._position
        if pos.direction == "LONG":
            return price <= effective_sl
        return price >= effective_sl

    def _is_tp_hit(self, price: float) -> bool:
        pos = self._position
        if pos.direction == "LONG":
            return price >= pos.take_profit
        return price <= pos.take_profit

    def _get_remaining_qty(self) -> float:
        if self._partial:
            return self._partial.remaining_quantity
        return self._position.size if self._position else 0.0

    async def _trigger_close(self, reason: str, price: float, quantity: float) -> None:
        """Execute a full close via the order executor."""
        if self._close_in_progress or not self._position:
            return
        self._close_in_progress = True

        pos = self._position
        close_side = "sell" if pos.direction == "LONG" else "buy"
        order_type = getattr(self.config, "LIVE_ORDER_TYPE", "limit")

        trailing_info = ""
        if self._trailing and reason == "stop_loss":
            trailing_info = f" (trailing SL=${self._trailing.trailing_sl:.2f})"

        self.logger.info(
            "[Monitor] %s triggered @ $%.2f for %.6f %s%s",
            reason.upper(),
            price,
            quantity,
            pos.symbol,
            trailing_info,
        )

        result = await self.order_executor.close_order(
            symbol=pos.symbol,
            side=close_side,
            quantity=quantity,
            price=price,
            order_type=order_type,
            source=self.source,
        )

        if not result.success:
            if getattr(result, "already_closed", False):
                # Broker has no open position (user closed manually or broker-side SL/TP hit)
                self.logger.warning(
                    "[Monitor] %s — broker reports no open %s position; finalizing close.",
                    pos.symbol,
                    close_side.upper(),
                )
                await self._handle_external_close(price)
                return
            self.logger.error(
                "[Monitor] Close order FAILED: %s — retrying as market", result.error
            )
            # Fallback to market order for urgent closes
            result = await self.order_executor.close_order(
                symbol=pos.symbol,
                side=close_side,
                quantity=quantity,
                price=price,
                order_type="market",
                source=self.source,
            )
            if not result.success:
                if getattr(result, "already_closed", False):
                    self.logger.warning(
                        "[Monitor] %s — broker reports no open %s position on fallback; finalizing close.",
                        pos.symbol,
                        close_side.upper(),
                    )
                    await self._handle_external_close(price)
                    return
                self.logger.error(
                    "[Monitor] Market fallback also FAILED: %s", result.error
                )
                self._close_in_progress = False
                return

        if self._close_callback:
            await self._close_callback(self.source, reason, price, quantity)

    async def _handle_external_close(self, price: float) -> None:
        """Finalize monitor state when the broker reports no open position.

        Triggered when a close attempt fails with `already_closed=True` — i.e.
        the user closed manually in the MT5 terminal or a broker-side SL/TP
        fired.  We mark the close as `external_close`, invoke the callback so
        the strategy can persist P&L / update the brain, then unregister.
        """
        if not self._position:
            self._close_in_progress = False
            return

        qty = self._get_remaining_qty()
        if self._close_callback:
            try:
                await self._close_callback(self.source, "external_close", price, qty)
            except Exception as e:
                self.logger.error("[Monitor] external_close callback failed: %s", e)

        self.unregister_position()

    # ------------------------------------------------------------------
    # Dashboard snapshot
    # ------------------------------------------------------------------

    async def _push_dashboard_snapshot(self, price: float, effective_sl: float) -> None:
        """Push a lightweight snapshot to dashboard state for real-time display."""
        pos = self._position
        if not pos:
            return

        snapshot = {
            "symbol": pos.symbol,
            "direction": pos.direction,
            "entry_price": pos.entry_price,
            "current_price": price,
            "stop_loss_fixed": pos.stop_loss,
            "stop_loss_effective": effective_sl,
            "take_profit": pos.take_profit,
            "pnl_pct": pos.calculate_pnl(price),
            "mae_pct": pos.max_drawdown_pct,
            "mfe_pct": pos.max_profit_pct,
        }

        if self._trailing and self._trailing.enabled:
            snapshot["trailing"] = {
                "enabled": True,
                "trailing_sl": self._trailing.trailing_sl,
                "highest_price": self._trailing.highest_price,
                "lowest_price": self._trailing.lowest_price,
                "atr_value": self._trailing.atr_value,
                "atr_multiplier": self._trailing.atr_multiplier,
                "atr_multiplier_after_tp1": self._trailing.atr_multiplier_after_tp1,
                "breakeven_activated": self._trailing.breakeven_activated,
            }

        if self._partial and self._partial.enabled:
            snapshot["partial"] = {
                "enabled": True,
                "targets_total": len(self._partial.targets),
                "targets_hit": len(self._partial.hit_indices),
                "original_qty": self._partial.original_quantity,
                "remaining_qty": self._partial.remaining_quantity,
            }

        try:
            await self._dashboard_state.update_monitor_snapshot(snapshot)
        except Exception:
            pass  # Never let dashboard errors affect trading
