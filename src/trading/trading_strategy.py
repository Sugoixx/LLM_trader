"""Trading strategy that wraps analysis with position management."""

import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional, Any, Dict, TYPE_CHECKING

from src.logger.logger import Logger
from src.contracts.risk_contract import RiskManagerProtocol
from .data_models import Position, TradeDecision, Rating
from .brain import TradingBrainService
from .statistics import TradingStatisticsService
from .memory import TradingMemoryService
from src.utils.profiler import profile_performance

if TYPE_CHECKING:
    from src.managers.persistence_manager import PersistenceManager
    from .debate_service import DebateService
    from .order_executor import OrderExecutorProtocol


class TradingStrategy:
    """Manages trading positions and decision execution based on AI analysis."""

    def __init__(
        self,
        logger: Logger,
        persistence: "PersistenceManager",
        brain_service: TradingBrainService,
        statistics_service: TradingStatisticsService,
        memory_service: TradingMemoryService,
        risk_manager: RiskManagerProtocol,
        config: Any = None,
        position_extractor=None,
        position_factory=None,
        debate_service: Optional["DebateService"] = None,
        order_executor: Optional["OrderExecutorProtocol"] = None,
    ):
        """Initialize the trading strategy with DI pattern.

        Args:
            logger: Logger instance
            persistence: Persistence service for loading/saving data
            brain_service: Brain service for learning and insights
            statistics_service: Statistics service for performance metrics
            memory_service: Memory service for recent decision context
            risk_manager: Risk Manager for position sizing and SL/TP
            config: Configuration module
            position_extractor: PositionExtractor instance (injected from app.py)
            position_factory: PositionFactory instance (injected from start.py)
            debate_service: Optional DebateService for Bull/Bear debate
            order_executor: Optional OrderExecutor for real/demo order placement
        """
        self.logger = logger
        self.persistence = persistence
        self.brain_service = brain_service
        self.statistics_service = statistics_service
        self.memory_service = memory_service
        self.risk_manager = risk_manager
        self.config = config
        self.extractor = position_extractor
        self.position_factory = position_factory
        self.debate_service = debate_service
        self.order_executor = order_executor
        self.dashboard_state = None  # Injected post-init if dashboard enabled
        self._live_initial_capital: Optional[float] = (
            None  # Cached MT5 balance at first fetch
        )
        # Legacy single-lock (kept for external uses). Multi-slot code uses
        # ``_position_locks[source]`` below.
        self._position_lock = asyncio.Lock()  # Prevents concurrent position open/close
        self._position_locks: Dict[str, asyncio.Lock] = {
            "ai": asyncio.Lock(),
            "fast": asyncio.Lock(),
        }

        # Multi-slot position storage. ``current_position`` below becomes a
        # property view over this dict for backward compatibility.
        self.positions: Dict[str, Position] = {}

        # Load any existing position(s). Prefers the new multi-slot format
        # but auto-migrates legacy single-position files.
        try:
            loaded = self.persistence.load_positions()
        except AttributeError:
            # Older persistence mock in tests — fall back to legacy API
            legacy = self.persistence.load_position()
            loaded = {"ai": legacy} if legacy is not None else {}
        if loaded:
            # Ensure each loaded Position carries the right source tag
            for src, pos in loaded.items():
                if pos is not None:
                    pos.source = src
            self.positions.update(loaded)

        for src, pos in self.positions.items():
            self.logger.info(
                "Loaded existing position [%s]: %s %s @ $%s",
                src,
                pos.direction,
                pos.symbol,
                f"{pos.entry_price:,.2f}",
            )

    # ── Multi-slot helpers ────────────────────────────────────────────────
    @property
    def current_position(self) -> Optional[Position]:
        """Backward-compat view: AI slot first, then Fast slot.

        Legacy code that reads ``self.current_position`` continues to work.
        New multi-slot code should use ``self.positions`` / ``get_position()``.
        """
        return self.positions.get("ai") or self.positions.get("fast")

    @current_position.setter
    def current_position(self, value: Optional[Position]) -> None:
        """Setter for backward compatibility.

        - ``self.current_position = None`` clears ALL slots (legacy single-slot
          semantics — safe because DOUBLE_TRADE_ENABLED=false means only one
          slot is ever open).
        - ``self.current_position = pos`` routes to the slot named by
          ``pos.source`` (defaults to 'ai').
        """
        if value is None:
            self.positions.clear()
            return
        src = getattr(value, "source", None) or "ai"
        value.source = src
        self.positions[src] = value

    def get_position(self, source: str) -> Optional[Position]:
        """Return the position for a specific source slot, or None."""
        return self.positions.get(source)

    def has_position(self, source: Optional[str] = None) -> bool:
        """Return True if any position is open (or specific slot if source given)."""
        if source is None:
            return bool(self.positions)
        return source in self.positions

    async def _persist_positions(self) -> None:
        """Save the full positions dict via the multi-slot API (best-effort)."""
        try:
            if hasattr(self.persistence, "async_save_positions"):
                await self.persistence.async_save_positions(dict(self.positions))
            else:  # Legacy fallback
                await self.persistence.async_save_position(self.current_position)
        except Exception as e:  # pylint: disable=broad-exception-caught
            self.logger.error("[strategy] Failed to persist positions: %s", e)

    async def sync_broker_positions(self, symbol: str, exchange) -> int:
        """Sync positions from broker at startup.

        If no local position but broker has open positions, load the net position.
        Detects and warns about opposing (hedged) positions on the broker.
        Returns count of positions found on broker.
        """
        if not exchange or not hasattr(exchange, "fetch_positions"):
            return 0

        # Skip broker sync if the exchange has no credentials — avoids
        # "kucoin requires apiKey" spam when running in paper/demo mode
        # or when we fell back to a symbol-matching exchange with no keys.
        if not getattr(exchange, "apiKey", None):
            return 0

        # Skip broker sync when running against a spot-only exchange.
        # Spot markets don't have "positions" — fetch_positions is a futures
        # endpoint and will fail (especially on Binance testnet where futures
        # sandbox is deprecated). Spot balance tracking lives elsewhere.
        try:
            default_type = (getattr(exchange, "options", {}) or {}).get(
                "defaultType", "spot"
            )
            if default_type == "spot":
                return 0
        except Exception:
            pass

        try:
            # Binance requires a list argument for fetch_positions
            try:
                broker_positions = await exchange.fetch_positions([symbol])
            except TypeError:
                broker_positions = await exchange.fetch_positions()
            if not broker_positions:
                return 0

            self.logger.info(
                "Found %d open position(s) on broker for %s",
                len(broker_positions),
                symbol,
            )

            # Log every position found on broker
            for p in broker_positions:
                p_type = p.get("type", "?").upper()
                p_price = float(p.get("price_open", 0))
                p_vol = float(p.get("volume", 0))
                p_pnl = float(p.get("profit", 0))
                p_sl = float(p.get("sl", 0))
                p_tp = float(p.get("tp", 0))
                p_ticket = p.get("ticket", "?")
                pnl_label = f"+{p_pnl:.2f}" if p_pnl >= 0 else f"{p_pnl:.2f}"
                level = self.logger.info if p_pnl >= 0 else self.logger.warning
                level(
                    "  → [#%s] %s %.2f lots @ %.5f  SL=%.5f  TP=%.5f  P&L=%s",
                    p_ticket,
                    p_type,
                    p_vol,
                    p_price,
                    p_sl,
                    p_tp,
                    pnl_label,
                )

            # Classify positions by direction
            buys = [p for p in broker_positions if p.get("type") == "buy"]
            sells = [p for p in broker_positions if p.get("type") == "sell"]

            if buys and sells:
                buy_vol = sum(float(p.get("volume", 0)) for p in buys)
                sell_vol = sum(float(p.get("volume", 0)) for p in sells)
                buy_pnl = sum(float(p.get("profit", 0)) for p in buys)
                sell_pnl = sum(float(p.get("profit", 0)) for p in sells)

                # Identify the losing side
                if buy_pnl < sell_pnl:
                    losing_side, losing_pnl = "BUY", buy_pnl
                else:
                    losing_side, losing_pnl = "SELL", sell_pnl

                self.logger.warning(
                    "⚠ HEDGED positions detected! %d BUY (%.2f lots, P&L=%+.2f) + %d SELL (%.2f lots, P&L=%+.2f)",
                    len(buys),
                    buy_vol,
                    buy_pnl,
                    len(sells),
                    sell_vol,
                    sell_pnl,
                )
                self.logger.warning(
                    "⚠ Losing side: %s (P&L=%+.2f) — close it on broker to stop bleeding!",
                    losing_side,
                    losing_pnl,
                )

                # Pick the winning side (better P&L) as the "net" position to track
                if buy_pnl >= sell_pnl:
                    candidates = buys
                else:
                    candidates = sells
            elif buys:
                candidates = buys
            else:
                candidates = sells

            # If no local position, load the largest candidate from broker
            if not self.current_position and candidates:
                best = max(candidates, key=lambda p: float(p.get("volume", 0)))
                direction = "LONG" if best.get("type") == "buy" else "SHORT"
                broker_pos = Position(
                    entry_price=float(best.get("price_open", 0.0)),
                    stop_loss=float(best.get("sl", 0.0)),
                    take_profit=float(best.get("tp", 0.0)),
                    size=float(best.get("volume", 0.0)),
                    entry_time=datetime.now(timezone.utc),
                    confidence="UNKNOWN",
                    direction=direction,
                    symbol=symbol,
                    confluence_factors=(),
                    entry_fee=0.0,
                    quote_amount=0.0,
                    size_pct=0.0,
                    atr_at_entry=0.0,
                    volatility_level="UNKNOWN",
                    sl_distance_pct=0.0,
                    tp_distance_pct=0.0,
                    rr_ratio_at_entry=0.0,
                    adx_at_entry=0.0,
                    rsi_at_entry=50.0,
                    trend_direction_at_entry="NEUTRAL",
                    macd_signal_at_entry="NEUTRAL",
                    bb_position_at_entry="MIDDLE",
                    volume_state_at_entry="NORMAL",
                    market_sentiment_at_entry="NEUTRAL",
                    max_drawdown_pct=0.0,
                    max_profit_pct=0.0,
                )
                # Tag broker-synced positions as the 'ai' slot by default.
                broker_pos.source = "ai"
                self.positions["ai"] = broker_pos
                await self._persist_positions()
                self.logger.info(
                    "Tracking %s %s @ $%s (ticket=%s) — winning side",
                    direction,
                    symbol,
                    f"{broker_pos.entry_price:,.2f}",
                    best.get("ticket", "unknown"),
                )

            return len(broker_positions)

        except Exception as e:
            self.logger.error("Error syncing broker positions: %s", e)
            return 0

    async def _get_capital(self) -> float:
        """Get current trading capital.

        In live mode, queries the real account balance from the order executor.
        In demo mode, uses DEMO_QUOTE_CAPITAL adjusted by historical P&L.
        """
        if self.order_executor and self.order_executor.is_live:
            try:
                balance = await self.order_executor.get_balance()
                if balance > 0:
                    # Cache first balance as initial capital reference
                    if self._live_initial_capital is None:
                        self._live_initial_capital = balance
                        self.logger.info("MT5 initial capital cached: %.2f", balance)
                    # Push live capital to dashboard state
                    if self.dashboard_state is not None:
                        self.dashboard_state.live_capital = balance
                    return balance
            except Exception as e:
                self.logger.warning(
                    "Failed to fetch live balance, falling back to stats: %s", e
                )
        return self.statistics_service.get_current_capital(
            self.config.DEMO_QUOTE_CAPITAL
        )

    def _get_initial_capital(self) -> float:
        """Get initial capital — MT5 cached balance if live, else config."""
        if self._live_initial_capital and self._live_initial_capital > 0:
            return self._live_initial_capital
        return self.config.DEMO_QUOTE_CAPITAL

    async def check_position(self, current_price: float) -> Optional[str]:
        """Check if any open slot hit stop loss or take profit.

        In multi-slot mode (AI + Fast) every open slot is evaluated
        independently so a SL/TP on one slot never "forgets" the other.

        Args:
            current_price: Current market price

        Returns:
            Reason for closing a position if hit, else None. When several
            slots trigger in the same tick, the last hit reason is returned
            (every triggered slot is still closed).
        """
        async with self._position_lock:
            if not self.positions:
                return None

            last_reason: Optional[str] = None
            # Snapshot slots first — close_position mutates self.positions.
            for source, position in list(self.positions.items()):
                # Update live performance metrics (MAE/MFE)
                position.update_metrics(current_price)

                if position.is_stop_hit(current_price):
                    conditions = self._build_conditions_from_position(position)
                    await self.close_position(
                        "stop_loss", current_price, conditions, source=source
                    )
                    last_reason = "stop_loss"
                    continue

                if position.is_target_hit(current_price):
                    conditions = self._build_conditions_from_position(position)
                    await self.close_position(
                        "take_profit", current_price, conditions, source=source
                    )
                    last_reason = "take_profit"
                    continue

            # Persist remaining slots once (close_position already persists
            # on closure; this captures MAE/MFE updates on survivors).
            if self.positions:
                await self._persist_positions()

            return last_reason

    async def close_position(
        self,
        reason: str,
        current_price: float,
        market_conditions: Optional[Dict[str, Any]] = None,
        source: str = "ai",
    ) -> None:
        """Close the current position and update trading brain.

        Args:
            reason: Reason for closing (stop_loss, take_profit, signal)
            current_price: Current market price
            market_conditions: Optional market conditions for brain learning
            source: Slot to close ('ai' or 'fast'). Defaults to the active
                    slot if only one is open.
        """
        # Resolve the target slot defensively. If callers pass the default
        # "ai" but only the "fast" slot is open, fall back to "fast" to
        # preserve legacy single-slot behaviour.
        position = self.positions.get(source)
        if position is None:
            if source == "ai" and "fast" in self.positions:
                source = "fast"
                position = self.positions["fast"]
            elif source == "fast" and "ai" in self.positions:
                source = "ai"
                position = self.positions["ai"]
        if position is None:
            return

        pnl = position.calculate_pnl(current_price)

        # Calculate closing fee
        closing_fee = position.calculate_closing_fee(
            current_price, self.config.TRANSACTION_FEE_PERCENT
        )

        decision = TradeDecision(
            timestamp=datetime.now(timezone.utc),
            symbol=position.symbol,
            action=f"CLOSE_{position.direction}",
            confidence=position.confidence,
            price=current_price,
            stop_loss=position.stop_loss,
            take_profit=position.take_profit,
            position_size=position.size_pct,
            quote_amount=position.quote_amount,
            quantity=position.size,
            fee=closing_fee,
            reasoning=f"Position closed: {reason}. P&L: {pnl:+.2f}%. Fee: ${closing_fee:.4f}",
        )

        self.logger.info(
            "[%s] Closing %s position (%s) @ $%s, P&L: %s%%, Fee: $%.4f",
            source.upper(),
            position.direction,
            reason,
            f"{current_price:,.2f}",
            f"{pnl:+.2f}",
            closing_fee,
        )

        # Execute close order on exchange (live or demo)
        if self.order_executor:
            # Check dashboard auto-trade toggle (SL/TP hits bypass — they are urgent safety orders)
            if (
                self.dashboard_state
                and not self.dashboard_state.auto_trade_enabled
                and reason == "analysis_signal"
            ):
                self.logger.warning(
                    "Auto-trade DISABLED via dashboard — skipping close order"
                )
                return

            # Require confirmation for live signal-based closes (not SL/TP hits which are urgent)
            if (
                self.order_executor.is_live
                and self.config.LIVE_CONFIRM_ORDERS
                and reason == "analysis_signal"
            ):
                close_action = f"CLOSE_{position.direction}"
                confirmed = await self._confirm_live_order(
                    close_action,
                    position.symbol,
                    position.size,
                    current_price,
                )
                if not confirmed:
                    return

            # Close = opposite side: LONG → sell, SHORT → buy
            close_side = "sell" if position.direction == "LONG" else "buy"
            order_result = await self.order_executor.close_order(
                symbol=position.symbol,
                side=close_side,
                quantity=position.size,
                price=current_price,
                order_type=self.config.LIVE_ORDER_TYPE
                if hasattr(self.config, "LIVE_ORDER_TYPE")
                else "limit",
                source=source,
            )
            if not order_result.success:
                # External/manual close: finalize cleanly instead of erroring out
                if getattr(order_result, "already_closed", False):
                    self.logger.warning(
                        "[%s] Position already closed externally — "
                        "finalizing locally (slot=%s)",
                        source.upper(),
                        source,
                    )
                else:
                    self.logger.error(
                        "Close order failed: %s — position still open",
                        order_result.error,
                    )
                    return
            # Update with actual fill data
            if order_result.success and order_result.fee > 0:
                closing_fee = order_result.fee
                decision = TradeDecision(
                    timestamp=datetime.now(timezone.utc),
                    symbol=position.symbol,
                    action=f"CLOSE_{position.direction}",
                    confidence=position.confidence,
                    price=order_result.avg_price or current_price,
                    stop_loss=position.stop_loss,
                    take_profit=position.take_profit,
                    position_size=position.size_pct,
                    quote_amount=position.quote_amount,
                    quantity=position.size,
                    fee=closing_fee,
                    reasoning=f"Position closed: {reason}. P&L: {pnl:+.2f}%. Fee: ${closing_fee:.4f}",
                )

        # Retrieve entry decision from trade history for brain learning
        entry_decision = None
        try:
            entry_decision = self.persistence.get_entry_decision_for_position(
                position.entry_time
            )
            if entry_decision:
                reasoning_preview = (
                    entry_decision.reasoning[:500]
                    if entry_decision.reasoning
                    else "(no reasoning)"
                )
                self.logger.debug(
                    "Retrieved entry decision with reasoning: %s...", reasoning_preview
                )
            else:
                self.logger.warning(
                    "Could not retrieve entry decision from trade history"
                )
        except Exception as e:
            self.logger.error("Error retrieving entry decision: %s", e)

        # Update trading brain with closed trade insights
        try:
            self.brain_service.update_from_closed_trade(
                position=position,
                close_price=current_price,
                close_reason=reason,
                entry_decision=entry_decision,
                market_conditions=market_conditions,
            )
        except Exception as e:
            self.logger.error("Error updating trading brain: %s", e)
        # Save close decision FIRST so statistics include this trade
        await self.persistence.async_save_trade_decision(decision)

        # Recalculate performance statistics (Sharpe, Sortino, drawdown, etc.)
        try:
            symbol = position.symbol if position else ""
            self.statistics_service.recalculate(
                self._get_initial_capital(), symbol=symbol
            )
        except Exception as e:
            self.logger.error("Error recalculating statistics: %s", e)
        # Remove the specific slot and persist the remaining dict
        self.positions.pop(source, None)
        await self._persist_positions()

    async def _finalize_close(
        self,
        reason: str,
        current_price: float,
        market_conditions: Optional[Dict[str, Any]] = None,
        source: str = "ai",
    ) -> None:
        """Finalize a position close — brain learning, persistence, stats.

        Called by ExecutionEngine when PositionMonitor already placed the
        close order.  Skips order execution (already done by monitor).
        """
        position = self.positions.get(source)
        if position is None:
            if source == "ai" and "fast" in self.positions:
                source = "fast"
                position = self.positions["fast"]
            elif source == "fast" and "ai" in self.positions:
                source = "ai"
                position = self.positions["ai"]
        if position is None:
            return

        pnl = position.calculate_pnl(current_price)
        closing_fee = position.calculate_closing_fee(
            current_price, self.config.TRANSACTION_FEE_PERCENT
        )

        decision = TradeDecision(
            timestamp=datetime.now(timezone.utc),
            symbol=position.symbol,
            action=f"CLOSE_{position.direction}",
            confidence=position.confidence,
            price=current_price,
            stop_loss=position.stop_loss,
            take_profit=position.take_profit,
            position_size=position.size_pct,
            quote_amount=position.quote_amount,
            quantity=position.size,
            fee=closing_fee,
            reasoning=f"Position closed by monitor: {reason}. P&L: {pnl:+.2f}%. Fee: ${closing_fee:.4f}",
        )

        self.logger.info(
            "[%s] Position closed by monitor (%s) @ $%s, P&L: %s%%",
            source.upper(),
            reason,
            f"{current_price:,.2f}",
            f"{pnl:+.2f}",
        )

        # Brain learning
        entry_decision = None
        try:
            entry_decision = self.persistence.get_entry_decision_for_position(
                position.entry_time
            )
        except Exception as e:
            self.logger.error("Error retrieving entry decision: %s", e)

        try:
            self.brain_service.update_from_closed_trade(
                position=position,
                close_price=current_price,
                close_reason=reason,
                entry_decision=entry_decision,
                market_conditions=market_conditions,
            )
        except Exception as e:
            self.logger.error("Error updating trading brain: %s", e)

        await self.persistence.async_save_trade_decision(decision)

        try:
            symbol = position.symbol if position else ""
            self.statistics_service.recalculate(
                self._get_initial_capital(), symbol=symbol
            )
        except Exception as e:
            self.logger.error("Error recalculating statistics: %s", e)

        self.positions.pop(source, None)
        await self._persist_positions()

    @profile_performance
    async def process_analysis(
        self, analysis_result: dict, symbol: str
    ) -> Optional[TradeDecision]:
        """Process AI analysis result and execute trading decision.

        Args:
            analysis_result: Result from AnalysisEngine.analyze_market()
            symbol: Trading symbol

        Returns:
            TradeDecision if action taken, else None
        """
        self.logger.info(
            "[process_analysis] ENTER symbol=%s open_slots=%s",
            symbol,
            list(self.positions.keys()),
        )
        async with self._position_lock:
            return await self._process_analysis_inner(analysis_result, symbol)

    @profile_performance
    async def _process_analysis_inner(
        self, analysis_result: dict, symbol: str
    ) -> Optional[TradeDecision]:
        """Inner implementation — called under position lock."""
        try:
            raw_response = analysis_result.get("raw_response", "")
            current_price = self._extract_price_from_result(analysis_result)

            if not raw_response:
                self.logger.warning(
                    "[BUY-BLOCKED] No response to process — LLM returned empty payload"
                )
                return None

            if current_price <= 0:
                self.logger.error(
                    "[BUY-BLOCKED] Invalid current_price=%s extracted from analysis",
                    current_price,
                )
                return None

            # Extract trading info from response
            (
                signal,
                confidence,
                stop_loss,
                take_profit,
                position_size,
                reasoning,
                extracted_rating,
            ) = self.extractor.extract_trading_info(raw_response)

            self.logger.info("Extracted Signal: %s, Confidence: %s", signal, confidence)

            # Validate signal
            if not self.extractor.validate_signal(signal):
                self.logger.warning(
                    "[BUY-BLOCKED] Invalid signal extracted from LLM response: %r",
                    signal,
                )
                return None

            # Run Bull/Bear debate if enabled and debate service is available
            debate_result = None
            if self.debate_service and self.config.DEBATE_ENABLED:
                try:
                    # Build concise analysis context for debate
                    debate_context = self._build_debate_context(analysis_result)
                    debate_result = await self.debate_service.debate(
                        signal=signal,
                        confidence=confidence,
                        analysis_context=debate_context,
                        reasoning=reasoning,
                    )
                    # Apply debate verdict — confidence only, signal is preserved
                    if debate_result and debate_result.final_confidence != confidence:
                        self.logger.info(
                            "Debate adjusted confidence: %s→%s (verdict: %s)",
                            confidence,
                            debate_result.final_confidence,
                            debate_result.verdict,
                        )
                        confidence = debate_result.final_confidence
                except Exception as e:
                    self.logger.error("Debate service error: %s", e)

            # Use AI-extracted rating if present, otherwise derive from signal+confidence
            rating = extracted_rating or Rating.ACTION_TO_RATING.get(
                (signal, confidence), ""
            )

            # Extract market conditions for brain learning
            market_conditions = self._extract_market_conditions(analysis_result)

            # Block SELL signals in bullish trends - avoid selling in uptrend
            if signal == "SELL":
                trend_dir = str(
                    market_conditions.get("trend_direction", "NEUTRAL")
                ).upper()
                try:
                    adx_value = float(market_conditions.get("adx") or 0.0)
                except (TypeError, ValueError):
                    adx_value = 0.0
                if trend_dir == "BULLISH":
                    self.logger.warning(
                        "[SELL-BLOCKED] SELL signal in BULLISH trend (%s) — avoiding sell in uptrend.",
                        trend_dir,
                    )
                    return None
                symbol_upper = str(symbol).upper()
                oil_symbol = symbol_upper in {"CRUDOIL", "XTIUSD", "XBRUSD"} or (
                    "OIL" in symbol_upper
                )
                if oil_symbol and (trend_dir != "BEARISH" or adx_value < 25.0):
                    self.logger.warning(
                        "[SELL-BLOCKED] Oil short requires BEARISH trend and ADX>=25 "
                        "(trend=%s, ADX=%.1f). Treating as HOLD.",
                        trend_dir,
                        adx_value,
                    )
                    return None

            # Extract confluence factors for brain learning (Feature 1)
            confluence_factors = self._extract_confluence_factors(analysis_result)

            _is_reversal_entry = False  # set True when close+open in same cycle

            # Handle existing position
            if self.current_position:
                # ── REVERSAL FAST-PATH ────────────────────────────────────
                # If the LLM emits BUY while SHORT (or SELL while LONG),
                # close the existing position AND open the new direction in
                # the same cycle. Without this, the bot would only emit a
                # CLOSE decision and miss the reverse entry until the next
                # candle — matching the prompt's "REVERSAL RULE".
                current_dir = self.current_position.direction

                # UPDATE/CLOSE signals: route directly without [BUY-BLOCKED] warning
                if signal in ("UPDATE", "CLOSE") or signal.startswith("CLOSE_"):
                    return await self._handle_existing_position(
                        signal,
                        confidence,
                        stop_loss,
                        take_profit,
                        current_price,
                        symbol,
                        reasoning,
                        market_conditions,
                    )

                signal_dir = (
                    "LONG" if signal == "BUY" else "SHORT" if signal == "SELL" else None
                )
                is_reversal = signal_dir is not None and signal_dir != current_dir
                if is_reversal:
                    self.logger.info(
                        "[REVERSAL] %s signal while %s open \u2014 closing then "
                        "opening %s in same cycle.",
                        signal,
                        current_dir,
                        signal_dir,
                    )
                    slot_source = getattr(self.current_position, "source", "ai") or "ai"
                    await self.close_position(
                        "reversal",
                        current_price,
                        market_conditions,
                        source=slot_source,
                    )
                    # Fall through to _open_new_position below (position dict
                    # now empty for this slot). Mark as reversal so confidence
                    # gates are bypassed — we already committed to this direction.
                    _is_reversal_entry = True
                else:
                    self.logger.warning(
                        "[BUY-BLOCKED] Existing %s position on %s (slot=%s) \u2014 "
                        "routing %s signal to update/close logic instead of opening new entry. "
                        "Enable double_trade_enabled=true in [safety] to allow a parallel slot.",
                        current_dir,
                        symbol,
                        getattr(self.current_position, "source", "?"),
                        signal,
                    )
                    return await self._handle_existing_position(
                        signal,
                        confidence,
                        stop_loss,
                        take_profit,
                        current_price,
                        symbol,
                        reasoning,
                        market_conditions,
                    )

            # Handle new position
            if signal in ("BUY", "SELL"):
                # Block LOW confidence trades — win rate 17%, not worth the risk
                if confidence == "LOW":
                    self.logger.warning(
                        "[BUY-BLOCKED] LOW confidence %s on %s — trade blocked "
                        "(debate or model returned low conviction). Treating as HOLD.",
                        signal,
                        symbol,
                    )
                    return None
                # Block MEDIUM if config min_confidence = HIGH or brain recommends
                # Skip this gate for reversal entries — the SHORT was already closed,
                # blocking here would leave the bot incorrectly flat.
                if confidence == "MEDIUM" and not _is_reversal_entry:
                    min_conf = (
                        getattr(self.config, "AI_MIN_CONFIDENCE", "MEDIUM")
                    ).upper()
                    if min_conf == "HIGH":
                        self.logger.warning(
                            "[BUY-BLOCKED] MEDIUM confidence %s on %s — "
                            "config min_confidence=HIGH. Treating as HOLD.",
                            signal,
                            symbol,
                        )
                        return None
                    try:
                        rec = self.brain_service.get_confidence_recommendation()
                        if rec and "MEDIUM" in rec and "outperforming" not in rec:
                            self.logger.warning(
                                "[BUY-BLOCKED] MEDIUM confidence %s on %s — "
                                "brain shows MEDIUM win rate <60%%. Treating as HOLD. "
                                "Recommendation: %s",
                                signal,
                                symbol,
                                rec,
                            )
                            return None
                    except Exception:
                        pass  # Fail open if brain check errors
                return await self._open_new_position(
                    signal,
                    confidence,
                    stop_loss,
                    take_profit,
                    position_size,
                    current_price,
                    symbol,
                    reasoning,
                    confluence_factors,
                    market_conditions,
                    rating=rating,
                    debate_result=debate_result,
                    source="ai",
                    is_reversal=_is_reversal_entry,
                )

            # LLM asked to UPDATE/CLOSE an existing position but no slot is
            # filled — most likely the model ignored the "no position" context.
            # Surface this clearly instead of silently falling through to HOLD.
            if signal in ("UPDATE", "CLOSE", "CLOSE_LONG", "CLOSE_SHORT"):
                self.logger.warning(
                    "[BUY-BLOCKED] LLM emitted %s with NO open position on %s "
                    "(confidence=%s) — ignoring. Treat as HOLD. Reasoning: %s",
                    signal,
                    symbol,
                    confidence,
                    (reasoning or "")[:200],
                )
                return None

            # HOLD or no action
            if reasoning:
                self.logger.info(
                    "No action: signal=%s, confidence=%s — reasoning: %s",
                    signal,
                    confidence,
                    reasoning[:200],
                )
            else:
                self.logger.info(
                    "No action: signal=%s, confidence=%s (no reasoning from AI)",
                    signal,
                    confidence,
                )
            return None

        except Exception as e:
            self.logger.error("Error processing analysis: %s", e)
            return None

    async def process_algo_decision(
        self,
        signal: str,
        confidence: str,
        current_price: float,
        symbol: str,
        reasoning: str,
        market_conditions: Optional[Dict[str, Any]] = None,
    ) -> Optional[TradeDecision]:
        """Execute a trade decision from algo strategy consensus (Fast Trading Mode).

        Bypasses LLM extraction and debate; uses the risk manager for SL/TP
        calculation (ATR-based defaults when no explicit levels provided).

        Args:
            signal: ``"BUY"``, ``"SELL"``, or ``"CLOSE"``
            confidence: ``"HIGH"``, ``"MEDIUM"``, or ``"LOW"``
            current_price: Current market price
            symbol: Trading symbol
            reasoning: Human-readable explanation from AlgoFastTrader
            market_conditions: Optional market condition dict for risk calculations

        Returns:
            TradeDecision if an order was placed/modified, else None
        """
        async with self._position_lock:
            return await self._process_algo_decision_inner(
                signal, confidence, current_price, symbol, reasoning, market_conditions
            )

    async def _process_algo_decision_inner(
        self,
        signal: str,
        confidence: str,
        current_price: float,
        symbol: str,
        reasoning: str,
        market_conditions: Optional[Dict[str, Any]] = None,
    ) -> Optional[TradeDecision]:
        """Inner implementation — called under position lock."""
        try:
            if current_price <= 0:
                self.logger.error("[Fast] Invalid price %.6f — skipping", current_price)
                return None

            if signal not in ("BUY", "SELL", "CLOSE"):
                self.logger.info("[Fast] Signal=%s — no action", signal)
                return None

            self.logger.info(
                "[Fast] Algo decision: %s | confidence=%s | price=$%.4f",
                signal,
                confidence,
                current_price,
            )

            # Fast loop operates on its own slot. With DOUBLE_TRADE off, the
            # shared _open_new_position gate will still refuse if AI already
            # has a position; that refusal is logged explicitly.
            fast_pos = self.positions.get("fast")
            if fast_pos:
                return await self._handle_existing_position(
                    signal=signal,
                    confidence=confidence,
                    stop_loss=None,
                    take_profit=None,
                    current_price=current_price,
                    symbol=symbol,
                    reasoning=reasoning,
                    market_conditions=market_conditions or {},
                    source="fast",
                )

            if signal in ("BUY", "SELL"):
                return await self._open_new_position(
                    signal=signal,
                    confidence=confidence,
                    stop_loss=None,
                    take_profit=None,
                    position_size=None,
                    current_price=current_price,
                    symbol=symbol,
                    reasoning=reasoning,
                    market_conditions=market_conditions or {},
                    source="fast",
                )

            return None

        except Exception as e:
            self.logger.error("[Fast] Error in process_algo_decision: %s", e)
            return None

    async def _handle_existing_position(
        self,
        signal: str,
        confidence: str,
        stop_loss: Optional[float],
        take_profit: Optional[float],
        current_price: float,
        symbol: str,
        reasoning: str,
        market_conditions: Optional[Dict[str, Any]] = None,
        source: str = "ai",
    ) -> Optional[TradeDecision]:
        """Handle trading decision when position exists in ``source`` slot.

        Args:
            signal: Trading signal
            confidence: Confidence level
            stop_loss: New stop loss (for update)
            take_profit: New take profit (for update)
            current_price: Current price
            symbol: Trading symbol
            reasoning: AI reasoning
            market_conditions: Market state for brain learning
            source: Slot name ('ai' or 'fast') to operate on.

        Returns:
            TradeDecision if action taken
        """
        position = self.positions.get(source) or self.current_position
        if position is None:
            return None
        if signal == "CLOSE" or signal.startswith("CLOSE_"):
            self.logger.info(
                "[%s] Closing position based on analysis signal...", source.upper()
            )
            await self.close_position(
                "analysis_signal", current_price, market_conditions, source=source
            )
            return TradeDecision(
                timestamp=datetime.now(timezone.utc),
                symbol=symbol,
                action="CLOSE",
                confidence=confidence,
                price=current_price,
                fee=0.0,
                reasoning=reasoning,
            )

        # Detect opposing signal: BUY while SHORT, or SELL while LONG → close first
        current_dir = position.direction  # "LONG" or "SHORT"
        signal_dir = (
            "LONG" if signal == "BUY" else "SHORT" if signal == "SELL" else None
        )

        if signal_dir and signal_dir != current_dir:
            self.logger.warning(
                "Opposing signal %s while %s open — closing current position first",
                signal,
                current_dir,
            )
            await self.close_position(
                "opposing_signal", current_price, market_conditions
            )
            # Position is now closed; return a CLOSE decision.
            # The next analysis cycle will open the new direction if still valid.
            return TradeDecision(
                timestamp=datetime.now(timezone.utc),
                symbol=symbol,
                action="CLOSE",
                confidence=confidence,
                price=current_price,
                fee=0.0,
                reasoning=f"Closed {current_dir} — opposing {signal} signal received. {reasoning}",
            )

        old_sl = self.current_position.stop_loss
        old_tp = self.current_position.take_profit

        updated = await self._update_position_parameters(stop_loss, take_profit)

        if updated:
            try:
                current_pnl = self.current_position.calculate_pnl(current_price)
                self.brain_service.track_position_update(
                    position=self.current_position,
                    old_sl=old_sl,
                    old_tp=old_tp,
                    new_sl=stop_loss if stop_loss else old_sl,
                    new_tp=take_profit if take_profit else old_tp,
                    current_price=current_price,
                    current_pnl_pct=current_pnl,
                    market_conditions=market_conditions,
                )
            except Exception as e:
                self.logger.warning("Failed to track position update: %s", e)

            decision = TradeDecision(
                timestamp=datetime.now(timezone.utc),
                symbol=symbol,
                action="UPDATE",
                confidence=confidence,
                price=current_price,
                stop_loss=stop_loss,
                take_profit=take_profit,
                fee=0.0,
                reasoning=f"Updated position parameters. {reasoning}",
            )
            await self.persistence.async_save_trade_decision(decision)
            self.logger.info(
                "Position updated: New SL=$%s, TP=$%s",
                f"{stop_loss:,.2f}",
                f"{take_profit:,.2f}",
            )
            return decision

        # Same direction, no parameter change — inform user why no action.
        self.logger.info(
            "No trade: %s already open on %s with matching %s signal; "
            "AI's SL/TP ($%s / $%s) match current values — holding.",
            current_dir,
            symbol,
            signal,
            f"{stop_loss:,.2f}" if stop_loss else "—",
            f"{take_profit:,.2f}" if take_profit else "—",
        )
        return None

    async def _open_new_position(
        self,
        signal: str,
        confidence: str,
        stop_loss: Optional[float],
        take_profit: Optional[float],
        position_size: Optional[float],
        current_price: float,
        symbol: str,
        reasoning: str,
        confluence_factors: tuple = (),
        market_conditions: Optional[Dict[str, Any]] = None,
        rating: str = "",
        debate_result=None,
        source: str = "ai",
        is_reversal: bool = False,
    ) -> TradeDecision:
        """Open a new trading position with dynamic parameter calculation."""
        direction = "LONG" if signal == "BUY" else "SHORT"
        market_conditions = dict(market_conditions or {})
        market_conditions.setdefault("symbol", symbol)

        # ── Multi-slot / DOUBLE_TRADE gate ────────────────────────────────
        # If another slot already has a position and DOUBLE_TRADE is off,
        # refuse the new open. Also refuse if this same slot already has
        # a position (should be caught by caller but defensive).
        double_trade_enabled = bool(getattr(self.config, "DOUBLE_TRADE_ENABLED", False))
        if source in self.positions:
            self.logger.warning(
                "[BUY-BLOCKED] [%s] Slot '%s' already has a position on %s — skipping new open",
                source.upper(),
                source,
                self.positions[source].symbol,
            )
            return None
        if not double_trade_enabled and self.positions:
            other_sources = ", ".join(self.positions.keys())
            self.logger.warning(
                "[BUY-BLOCKED] [%s] double_trade_enabled=false and another slot is open (%s). "
                "Refusing new open for source '%s'. "
                "Enable double_trade_enabled=true in [safety] to allow a parallel slot.",
                source.upper(),
                other_sources,
                source,
            )
            return None

        # ── AI Safety Gates (source='ai' only — fast has its own guards) ──
        # Bypass all gates for reversal entries: the old position was already
        # closed; blocking here would leave the bot incorrectly flat.
        if source == "ai" and not is_reversal:
            blocked_reason = self._check_ai_safety_gates(
                confidence=confidence,
                stop_loss=stop_loss,
                take_profit=take_profit,
                current_price=current_price,
                signal=signal,
                debate_result=debate_result,
            )
            if blocked_reason:
                self.logger.warning(
                    "[BUY-BLOCKED] [AI] %s on %s — %s",
                    signal,
                    symbol,
                    blocked_reason,
                )
                return None

        # Netting-safe gate: if broker is in NETTING mode, an opposing
        # position on the same symbol is impossible. Piggybacking a same-side
        # trade is allowed (netting just adds volume).
        if double_trade_enabled and self.positions:
            for other_src, other_pos in self.positions.items():
                if other_src == source:
                    continue
                if other_pos.symbol != symbol:
                    continue
                other_dir = other_pos.direction
                if other_dir != direction:
                    executor = self.order_executor
                    hedging_mode = True
                    if executor is not None:
                        hedging_mode = bool(getattr(executor, "hedging_mode", True))
                    if not hedging_mode:
                        self.logger.warning(
                            "[BUY-BLOCKED] [%s] Broker NETTING mode — cannot open "
                            "opposing %s position on %s while slot '%s' holds %s.",
                            source.upper(),
                            direction,
                            symbol,
                            other_src,
                            other_dir,
                        )
                        return None

        # Calculate quantity based on CURRENT capital (not initial)
        capital = await self._get_capital()

        # Query broker-side execution constraints (leverage, min lot, min
        # notional). Lets the risk manager produce a leverage-aware size
        # and detect when the trade is below broker minimums.
        broker_constraints = None
        if self.order_executor is not None and hasattr(
            self.order_executor, "get_broker_constraints"
        ):
            try:
                broker_constraints = await self.order_executor.get_broker_constraints(
                    symbol, current_price
                )
            except Exception as e:
                self.logger.warning(
                    "Broker constraints fetch failed — position sizing will be "
                    "unlevered (no leverage/min-lot awareness): %s",
                    e,
                )

        # Delegate Risk Calculation to RiskManager
        risk_assessment = self.risk_manager.calculate_entry_parameters(
            signal=signal,
            current_price=current_price,
            capital=capital,
            confidence=confidence,
            stop_loss=stop_loss,
            take_profit=take_profit,
            position_size=position_size,
            market_conditions=market_conditions,
            broker_constraints=broker_constraints,
        )

        # Non-executable sizing → surface a clear hypothesis and abort.
        if not risk_assessment.executable:
            sugg = risk_assessment.capital_suggestion or {}
            self.logger.warning(
                "[BUY-BLOCKED] [%s] Risk/sizing refused for %s: %s",
                source.upper(),
                symbol,
                risk_assessment.sizing_warning,
            )
            if sugg:
                self.logger.warning(
                    "Capital hypothesis for %s: add ~$%s to reach $%s equity; "
                    "at broker min (%s lots = $%s notional) expected TP=+$%s / "
                    "SL=-$%s (leverage %sx).",
                    symbol,
                    f"{sugg.get('capital_top_up', 0):,.2f}",
                    f"{sugg.get('capital_needed_total', 0):,.2f}",
                    sugg.get("required_lots"),
                    f"{sugg.get('required_notional', 0):,.2f}",
                    f"{sugg.get('expected_gain_at_tp', 0):,.2f}",
                    f"{sugg.get('expected_loss_at_sl', 0):,.2f}",
                    sugg.get("leverage"),
                )
            # Broadcast to dashboard as a dismissible banner.
            if self.dashboard_state is not None:
                try:
                    alert_payload = {
                        "symbol": symbol,
                        "side": signal,
                        "price": current_price,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "sizing_warning": risk_assessment.sizing_warning or "",
                        "current_capital": float(capital),
                        "account_currency": (
                            broker_constraints.account_currency
                            if broker_constraints
                            else "USD"
                        ),
                        **{
                            k: float(v) if isinstance(v, (int, float)) else v
                            for k, v in (sugg or {}).items()
                        },
                    }
                    await self.dashboard_state.update_capital_alert(alert_payload)
                except Exception as e:
                    self.logger.debug("Failed to push capital alert: %s", e)
            # Persist the refusal so the dashboard reflects the state
            self.positions.pop(source, None)
            await self._persist_positions()
            return None

        # Executable → clear any previous alert
        if self.dashboard_state is not None and getattr(
            self.dashboard_state, "capital_alert", None
        ):
            try:
                await self.dashboard_state.update_capital_alert(None)
            except Exception:
                pass

        final_sl = risk_assessment.stop_loss
        final_tp = risk_assessment.take_profit
        final_size_pct = risk_assessment.size_pct
        quantity = risk_assessment.quantity
        entry_fee = risk_assessment.entry_fee
        sl_distance_pct = risk_assessment.sl_distance_pct
        tp_distance_pct = risk_assessment.tp_distance_pct
        rr_ratio = risk_assessment.rr_ratio

        self.logger.info(
            "Position sizing: Capital=$%s, Size=%.2f%%, Allocation=$%s, Quantity=%.6f",
            f"{capital:,.2f}",
            final_size_pct * 100,
            f"{risk_assessment.quote_amount:,.2f}",
            quantity,
        )
        self.logger.info(
            "Risk metrics: SL=%.2f%%, TP=%.2f%%, R/R=%.2f",
            sl_distance_pct * 100,
            tp_distance_pct * 100,
            rr_ratio,
        )

        if source == "fast":
            min_rr = float(getattr(self.config, "FAST_MIN_RR_AFTER_FEES", 0.0) or 0.0)
            if min_rr > 0:
                rr_reason = self._check_min_rr_after_fees(
                    stop_loss=final_sl,
                    take_profit=final_tp,
                    current_price=current_price,
                    signal=signal,
                    min_rr=min_rr,
                )
                if rr_reason:
                    self.logger.warning(
                        "[BUY-BLOCKED] [FAST] %s on %s — %s",
                        signal,
                        symbol,
                        rr_reason.replace("[ai_safety]", "[fast_trading]"),
                    )
                    return None

        # Create position using Factory
        new_position = self.position_factory.create_position(
            symbol=symbol,
            direction=direction,
            confidence=confidence,
            risk_assessment=risk_assessment,
            confluence_factors=confluence_factors,
            market_conditions=market_conditions,
        )
        # Tag with source slot and register in the multi-slot dict.
        new_position.source = source

        # Per-source USD cap enforcement (Phase 5). Uses LIVE_MAX_ORDER_USD_AI
        # or LIVE_MAX_ORDER_USD_FAST depending on the slot. Falls back to the
        # global LIVE_MAX_ORDER_USD when the per-source cap is unset.
        order_value_usd = float(quantity) * float(current_price)
        if source == "fast":
            max_usd = float(getattr(self.config, "LIVE_MAX_ORDER_USD_FAST", 0) or 0)
        else:
            max_usd = float(getattr(self.config, "LIVE_MAX_ORDER_USD_AI", 0) or 0)
        if max_usd <= 0:
            max_usd = float(getattr(self.config, "LIVE_MAX_ORDER_USD", 0) or 0)
        if max_usd > 0 and order_value_usd > max_usd:
            self.logger.warning(
                "[BUY-BLOCKED] [%s] Order value $%.2f exceeds per-source USD cap $%.2f "
                "(config: LIVE_MAX_ORDER_USD_%s / LIVE_MAX_ORDER_USD)",
                source.upper(),
                order_value_usd,
                max_usd,
                source.upper(),
            )
            return None

        self.positions[source] = new_position
        await self._persist_positions()
        self.logger.info(
            "Opened %s position @ $%s (SL: $%s, TP: $%s, Qty: %.6f, Fee: $%.4f)",
            direction,
            f"{current_price:,.2f}",
            f"{final_sl:,.2f}",
            f"{final_tp:,.2f}",
            quantity,
            entry_fee,
        )

        # Execute order on exchange (live or demo)
        order_result = None
        if self.order_executor:
            # Check dashboard auto-trade toggle
            if self.dashboard_state and not self.dashboard_state.auto_trade_enabled:
                self.logger.warning(
                    "[BUY-BLOCKED] [%s] Auto-trade DISABLED via dashboard toggle — "
                    "order not sent to broker. Re-enable in the dashboard.",
                    source.upper(),
                )
                self.positions.pop(source, None)
                await self._persist_positions()
                return None

            # Require confirmation for live orders if configured
            if self.order_executor.is_live and self.config.LIVE_CONFIRM_ORDERS:
                confirmed = await self._confirm_live_order(
                    signal, symbol, quantity, current_price
                )
                if not confirmed:
                    self.logger.warning(
                        "[BUY-BLOCKED] [%s] Live order NOT CONFIRMED by operator "
                        "(LIVE_CONFIRM_ORDERS=true). Disable to auto-execute.",
                        source.upper(),
                    )
                    self.positions.pop(source, None)
                    await self._persist_positions()
                    return None

            order_side = "buy" if signal == "BUY" else "sell"
            order_result = await self.order_executor.open_order(
                symbol=symbol,
                side=order_side,
                quantity=quantity,
                price=current_price,
                order_type=self.config.LIVE_ORDER_TYPE
                if hasattr(self.config, "LIVE_ORDER_TYPE")
                else "limit",
                source=source,
            )
            if not order_result.success:
                self.logger.error(
                    "[BUY-BLOCKED] [%s] Broker order execution FAILED: %s — reverting position",
                    source.upper(),
                    order_result.error,
                )
                self.positions.pop(source, None)
                await self._persist_positions()
                return None
            # Update fee from actual exchange fill
            if order_result.fee > 0:
                entry_fee = order_result.fee
            if order_result.avg_price > 0:
                current_price = order_result.avg_price

            # Attach SL/TP to the broker position immediately after entry.
            # MT5 orders placed via open_order() don't carry SL/TP; without
            # this step the broker would show an unprotected position until
            # the next UPDATE signal.
            try:
                ok = await self.order_executor.modify_position(
                    symbol,
                    final_sl,
                    final_tp,
                    source=source,
                )
                if ok:
                    self.logger.info(
                        "[%s] SL/TP attached to broker position: SL=$%.5f TP=$%.5f",
                        source.upper(),
                        final_sl,
                        final_tp,
                    )
                else:
                    self.logger.warning(
                        "[%s] Broker refused SL/TP attach (SL=$%.5f TP=$%.5f) — "
                        "position opened WITHOUT protection.",
                        source.upper(),
                        final_sl,
                        final_tp,
                    )
            except Exception as e:
                self.logger.error(
                    "[%s] Failed to attach SL/TP to broker position: %s",
                    source.upper(),
                    e,
                )

        # Create and save decision (store size_pct for history context)
        decision = TradeDecision(
            timestamp=datetime.now(timezone.utc),
            symbol=symbol,
            action=signal,
            confidence=confidence,
            price=current_price,
            stop_loss=final_sl,
            take_profit=final_tp,
            position_size=final_size_pct,
            quote_amount=risk_assessment.quote_amount,
            quantity=quantity,
            fee=entry_fee,
            reasoning=reasoning,
            rating=rating,
            debate_verdict=debate_result.verdict if debate_result else None,
            debate_confidence_delta=debate_result.confidence_delta
            if debate_result
            else 0.0,
            source=source,
        )

        await self.persistence.async_save_trade_decision(decision)

        return decision

    async def _update_position_parameters(
        self,
        stop_loss: Optional[float],
        take_profit: Optional[float],
    ) -> bool:
        """Update position stop loss and take profit.

        Args:
            stop_loss: New stop loss
            take_profit: New take profit

        Returns:
            True if anything was updated
        """
        if not self.current_position:
            return False

        updated = False
        new_sl = self.current_position.stop_loss
        new_tp = self.current_position.take_profit

        if stop_loss and abs(stop_loss - self.current_position.stop_loss) > 1e-6:
            direction = self.current_position.direction
            old_sl = self.current_position.stop_loss
            # FULL AI AUTONOMY: Allow AI to move stop loss in any direction
            if direction == "LONG" and stop_loss < old_sl:
                self.logger.info(
                    "AI Widening Stop Loss for LONG: $%.2f -> $%.2f (Risk Increased)",
                    old_sl,
                    stop_loss,
                )
                new_sl = stop_loss
                updated = True
            elif direction == "SHORT" and stop_loss > old_sl:
                self.logger.info(
                    "AI Widening Stop Loss for SHORT: $%.2f -> $%.2f (Risk Increased)",
                    old_sl,
                    stop_loss,
                )
                new_sl = stop_loss
                updated = True
            else:
                new_sl = stop_loss
                self.logger.info("Updated Stop Loss: $%s", f"{stop_loss:,.2f}")
                updated = True

        if take_profit and abs(take_profit - self.current_position.take_profit) > 1e-6:
            new_tp = take_profit
            self.logger.info("Updated Take Profit: $%s", f"{take_profit:,.2f}")
            updated = True

        if updated:
            # Create new position with updated values using factory
            self.current_position = self.position_factory.create_updated_position(
                original_position=self.current_position,
                new_stop_loss=new_sl,
                new_take_profit=new_tp,
            )
            await self.persistence.async_save_position(self.current_position)
            # Propagate SL/TP change to the broker (MT5 / Live) immediately.
            if self.order_executor:
                _symbol = self.current_position.symbol
                _source = getattr(self.current_position, "source", "ai") or "ai"
                try:
                    await self.order_executor.modify_position(
                        _symbol, new_sl, new_tp, source=_source
                    )
                except Exception as e:
                    self.logger.error("Failed to send SL/TP update to broker: %s", e)

        return updated

    def _extract_price_from_result(self, result: dict) -> float:
        """Extract current price from analysis result.

        Args:
            result: Analysis result dictionary

        Returns:
            Current price
        """
        # Try different possible locations for price
        if "current_price" in result:
            return float(result["current_price"])

        if "context" in result and result["context"] is not None:
            return float(result["context"].current_price)

        # Default fallback
        if self.logger:
            self.logger.warning(
                "Could not extract price from result keys: %s", list(result.keys())
            )
        return 0.0

    # ── Manual Trading (dashboard buttons) ────────────────────────────────

    async def manual_open_position(
        self,
        signal: str,
        current_price: float,
        stop_loss: float,
        take_profit: float,
        symbol: str,
        volume: Optional[float] = None,
    ) -> Optional[TradeDecision]:
        """Open a position manually from the dashboard.

        The AI will monitor SL/TP/trailing as if it opened the position itself.

        Args:
            signal: "BUY" or "SELL"
            current_price: Current market price
            stop_loss: Stop loss price
            take_profit: Take profit price
            symbol: Trading symbol
            volume: Explicit lot/contract size (None = auto-size via risk manager)

        Returns:
            TradeDecision if successful, None on failure
        """
        async with self._position_lock:
            if self.current_position:
                # Check if opposing direction — close first, then open new
                current_dir = self.current_position.direction
                requested_dir = "LONG" if signal == "BUY" else "SHORT"
                if requested_dir != current_dir:
                    self.logger.warning(
                        "[MANUAL] Opposing trade %s while %s open — closing current position first",
                        signal,
                        current_dir,
                    )
                    await self.close_position("manual_opposing", current_price)
                    # current_position is now None, continue to open new one
                else:
                    self.logger.warning(
                        "Manual trade rejected — same-direction %s position already open",
                        current_dir,
                    )
                    return None

            if signal not in ("BUY", "SELL"):
                self.logger.warning(
                    "Manual trade rejected — invalid signal: %s", signal
                )
                return None

            direction = "LONG" if signal == "BUY" else "SHORT"
            capital = await self._get_capital()

            # Use risk manager for sizing with HIGH confidence (manual = deliberate)
            risk_assessment = self.risk_manager.calculate_entry_parameters(
                signal=signal,
                current_price=current_price,
                capital=capital,
                confidence="HIGH",
                stop_loss=stop_loss,
                take_profit=take_profit,
                position_size=0.5,  # 50% default for manual trades
                market_conditions={},
            )

            # Override quantity if user specified explicit volume
            if volume is not None and volume > 0:
                risk_assessment.quantity = volume

            # Create position via factory
            new_pos = self.position_factory.create_position(
                symbol=symbol,
                direction=direction,
                confidence="HIGH",
                risk_assessment=risk_assessment,
            )
            new_pos.source = "ai"
            self.positions["ai"] = new_pos
            await self._persist_positions()

            self.logger.info(
                "[MANUAL] Opened %s @ $%.5f (SL: $%.5f, TP: $%.5f, Qty: %.4f)",
                direction,
                current_price,
                risk_assessment.stop_loss,
                risk_assessment.take_profit,
                risk_assessment.quantity,
            )

            # Execute order on exchange
            if self.order_executor:
                order_side = "buy" if signal == "BUY" else "sell"
                order_type = getattr(self.config, "LIVE_ORDER_TYPE", "market")
                order_result = await self.order_executor.open_order(
                    symbol=symbol,
                    side=order_side,
                    quantity=risk_assessment.quantity,
                    price=current_price,
                    order_type=order_type,
                    source="ai",
                )
                if not order_result.success:
                    self.logger.error(
                        "[MANUAL] Order failed: %s — reverting", order_result.error
                    )
                    self.positions.pop("ai", None)
                    await self._persist_positions()
                    return None
                if order_result.avg_price > 0:
                    current_price = order_result.avg_price

                # Attach SL/TP to the broker position (MT5 / Live).
                try:
                    ok = await self.order_executor.modify_position(
                        symbol,
                        risk_assessment.stop_loss,
                        risk_assessment.take_profit,
                        source="ai",
                    )
                    if ok:
                        self.logger.info(
                            "[MANUAL] SL/TP attached to broker: SL=$%.5f TP=$%.5f",
                            risk_assessment.stop_loss,
                            risk_assessment.take_profit,
                        )
                    else:
                        self.logger.warning(
                            "[MANUAL] Broker refused SL/TP attach — position opened WITHOUT protection."
                        )
                except Exception as e:
                    self.logger.error(
                        "[MANUAL] Failed to attach SL/TP to broker: %s", e
                    )

            decision = TradeDecision(
                timestamp=datetime.now(timezone.utc),
                symbol=symbol,
                action=signal,
                confidence="HIGH",
                price=current_price,
                stop_loss=risk_assessment.stop_loss,
                take_profit=risk_assessment.take_profit,
                position_size=risk_assessment.size_pct,
                quote_amount=risk_assessment.quote_amount,
                quantity=risk_assessment.quantity,
                fee=risk_assessment.entry_fee,
                reasoning="Manual trade from dashboard",
            )
            await self.persistence.async_save_trade_decision(decision)
            return decision

    async def manual_close_position(
        self, current_price: float
    ) -> Optional[TradeDecision]:
        """Close the current position manually from the dashboard.

        Returns:
            TradeDecision if closed, None if no position
        """
        async with self._position_lock:
            if not self.current_position:
                self.logger.warning("[MANUAL] No position to close")
                return None

            # Capture fields before close_position() clears self.current_position
            symbol = self.current_position.symbol
            direction = self.current_position.direction

            self.logger.info("[MANUAL] Closing %s position", direction)
            await self.close_position("manual_dashboard", current_price)

            return TradeDecision(
                timestamp=datetime.now(timezone.utc),
                symbol=symbol,
                action="CLOSE",
                confidence="HIGH",
                price=current_price,
                fee=0.0,
                reasoning="Manual close from dashboard",
            )

    @staticmethod
    def _build_conditions_from_position(position: Position) -> Dict[str, Any]:
        """Reconstruct market conditions from Position's stored entry fields.

        Used when closing via SL/TP hit where no fresh analysis is available.
        """
        rsi = position.rsi_at_entry
        if rsi > 70:
            rsi_level = "OVERBOUGHT"
        elif rsi > 60:
            rsi_level = "STRONG"
        elif rsi < 30:
            rsi_level = "OVERSOLD"
        elif rsi < 40:
            rsi_level = "WEAK"
        else:
            rsi_level = "NEUTRAL"

        return {
            "trend_direction": position.trend_direction_at_entry,
            "adx": position.adx_at_entry,
            "rsi": rsi,
            "rsi_level": rsi_level,
            "volatility": position.volatility_level,
            "macd_signal": position.macd_signal_at_entry,
            "bb_position": position.bb_position_at_entry,
            "volume_state": position.volume_state_at_entry,
            "market_sentiment": position.market_sentiment_at_entry,
        }

    def _extract_market_conditions(self, result: dict) -> Dict[str, Any]:
        """Extract market conditions from analysis result for brain learning.

        Args:
            result: Analysis result dictionary

        Returns:
            Dictionary with trend_direction, adx, volatility, etc.
        """
        conditions = {}

        try:
            # Extract from analysis dict (result has 'analysis' at top level, not under 'parsed_json')
            analysis = result.get("analysis", {})

            # Get trend info
            trend = analysis.get("trend", {})
            if trend:
                conditions["trend_direction"] = trend.get("direction", "NEUTRAL")
                conditions["trend_strength"] = trend.get(
                    "strength_4h", trend.get("strength", 50)
                )
                conditions["timeframe_alignment"] = trend.get("timeframe_alignment")

            # Get technical data (result has 'technical_data' at top level, not under 'context')
            tech_data = result.get("technical_data", {})
            if tech_data:
                conditions["adx"] = tech_data.get("adx", 0)
                conditions["rsi"] = tech_data.get("rsi", 50)
                # Derive RSI Level
                rsi_val = conditions["rsi"]
                if rsi_val > 70:
                    conditions["rsi_level"] = "OVERBOUGHT"
                elif rsi_val > 60:
                    conditions["rsi_level"] = "STRONG"
                elif rsi_val < 30:
                    conditions["rsi_level"] = "OVERSOLD"
                elif rsi_val < 40:
                    conditions["rsi_level"] = "WEAK"
                else:
                    conditions["rsi_level"] = "NEUTRAL"

                conditions["choppiness"] = tech_data.get("choppiness", None)

                # MACD
                macd = tech_data.get("macd", {})
                if isinstance(macd, dict):
                    conditions["macd_signal"] = macd.get("signal", "NEUTRAL")

                # Bollinger Bands
                bb = tech_data.get("bollinger_bands", {})
                if isinstance(bb, dict):
                    pct_b = bb.get("percent_b", 0.5)
                    if pct_b > 0.95:
                        conditions["bb_position"] = "UPPER"
                    elif pct_b < 0.05:
                        conditions["bb_position"] = "LOWER"
                    else:
                        conditions["bb_position"] = "MIDDLE"

                # Volume
                vol_data = tech_data.get("volume", {})
                if isinstance(vol_data, dict):
                    conditions["volume_state"] = vol_data.get("state", "NORMAL")

                # Extract ATR for dynamic SL/TP calculation
                atr_value = tech_data.get("atr", 0)
                if atr_value:
                    if isinstance(atr_value, dict):
                        # Handle dict type (from 2D arrays)
                        atr_value = next(iter(atr_value.values()), None) if atr_value else 0.0
                    if hasattr(atr_value, '__iter__') and not isinstance(atr_value, (int, float)):
                        # It's an array/list, get the last valid value
                        try:
                            import numpy as np
                            val = atr_value[-1] if hasattr(atr_value, '__getitem__') else atr_value
                            atr_value = float(np.nan_to_num(val)) if not np.isnan(val) else 0.0
                        except (IndexError, TypeError, ValueError):
                            atr_value = 0.0
                    conditions["atr"] = float(atr_value) if atr_value else 0.0
                else:
                    conditions["atr"] = 0.0

                atr_pct = tech_data.get("atr_percentage", 0)
                if atr_pct:
                    if isinstance(atr_pct, dict):
                        atr_pct = next(iter(atr_pct.values()), None) if atr_pct else 0.0
                    if hasattr(atr_pct, '__iter__') and not isinstance(atr_pct, (int, float)):
                        try:
                            import numpy as np
                            val = atr_pct[-1] if hasattr(atr_pct, '__getitem__') else atr_pct
                            atr_pct = float(np.nan_to_num(val)) if not np.isnan(val) else 0.0
                        except (IndexError, TypeError, ValueError):
                            atr_pct = 0.0
                    conditions["atr_percentage"] = float(atr_pct) if atr_pct else 0.0
                else:
                    conditions["atr_percentage"] = 0.0
                # Determine volatility from ATR or other indicators
                if atr_pct > 3:
                    conditions["volatility"] = "HIGH"
                elif atr_pct < 1.5:
                    conditions["volatility"] = "LOW"
                else:
                    conditions["volatility"] = "MEDIUM"

            # Market Sentiment
            conditions["market_sentiment"] = (
                result.get("context", {}).get("market_sentiment", "NEUTRAL")
                if "context" in result
                else "NEUTRAL"
            )
            conditions["fear_greed_index"] = (
                result.get("context", {}).get("fear_greed_index", 50)
                if "context" in result
                else 50
            )

            # Fallback: try to extract from raw response keywords
            raw_response = result.get("raw_response", "").lower()
            if not conditions.get("trend_direction"):
                if "bullish" in raw_response or "uptrend" in raw_response:
                    conditions["trend_direction"] = "BULLISH"
                elif "bearish" in raw_response or "downtrend" in raw_response:
                    conditions["trend_direction"] = "BEARISH"
                else:
                    conditions["trend_direction"] = "NEUTRAL"
        except Exception as e:
            self.logger.warning("Could not extract market conditions: %s", e)
        return conditions

    def _extract_confluence_factors(self, result: dict) -> tuple:
        """Extract confluence factors from analysis result for brain learning.

        Args:
            result: Analysis result dictionary

        Returns:
            Tuple of (factor_name, score) pairs
        """
        factors = []

        try:
            # Extract from analysis dict (result has 'analysis' at top level, not under 'parsed_json')
            analysis = result.get("analysis", {})
            confluence_factors = analysis.get("confluence_factors", {})
            if isinstance(confluence_factors, dict):
                for factor_name, score in confluence_factors.items():
                    try:
                        # Ensure score is numeric and in valid range
                        score_value = float(score)
                        if 0 <= score_value <= 100:
                            factors.append((factor_name, score_value))
                    except (ValueError, TypeError):
                        pass
        except Exception as e:
            self.logger.warning("Could not extract confluence factors: %s", e)
        return tuple(factors)

    async def get_position_context(self, current_price: Optional[float] = None) -> str:
        """Get formatted context about current position for prompts.

        Args:
            current_price: Current market price for P&L calculation

        Returns:
            Formatted position context string with capital status
        """
        capital = await self._get_capital()
        currency = self.config.QUOTE_CURRENCY
        if not self.current_position:
            return (
                f"## Capital Status\n"
                f"- Total Capital: ${capital:,.2f} {currency}\n"
                f"- Available: ${capital:,.2f} (100%)\n\n"
                f"CURRENT POSITION: None"
            )
        pos = self.current_position
        # Ensure both datetimes are timezone-aware for subtraction
        now = datetime.now(timezone.utc)
        entry_time = pos.entry_time
        if entry_time.tzinfo is None:
            entry_time = entry_time.replace(tzinfo=timezone.utc)
        duration = now - entry_time
        hours = duration.total_seconds() / 3600
        allocated = pos.quote_amount
        available = capital - allocated
        allocation_pct = (allocated / capital) * 100 if capital > 0 else 0
        context_lines = [
            "## Capital Status",
            f"- Total Capital: ${capital:,.2f} {currency}",
            f"- Allocated: ${allocated:,.2f} ({allocation_pct:.1f}%)",
            f"- Available: ${available:,.2f} ({100 - allocation_pct:.1f}%)",
            "",
            "## Current Position",
            f"- Direction: {pos.direction}",
            f"- Symbol: {pos.symbol}",
            f"- Entry Price: ${pos.entry_price:,.2f}",
        ]
        if current_price and current_price > 0:
            context_lines.append(f"- Current Price: ${current_price:,.2f}")
        context_lines.extend(
            [
                f"- Stop Loss: ${pos.stop_loss:,.2f}",
                f"- Take Profit: ${pos.take_profit:,.2f}",
                f"- Position Size: {pos.size_pct * 100:.2f}%",
                f"- Quantity: {pos.size:.6f}",
                f"- Entry Fee: ${pos.entry_fee:.4f}",
                f"- Duration: {hours:.1f} hours",
                f"- Confidence: {pos.confidence}",
            ]
        )
        if current_price and current_price > 0:
            pnl_pct = pos.calculate_pnl(current_price)
            pnl_quote = (
                (current_price - pos.entry_price) * pos.size
                if pos.direction == "LONG"
                else (pos.entry_price - current_price) * pos.size
            )
            context_lines.append(
                f"- Unrealized P&L: {pnl_pct:+.2f}% (${pnl_quote:+,.2f} {currency})"
            )
        return "\n".join(context_lines)

    def _build_debate_context(self, result: dict) -> str:
        """Build a concise analysis summary for the debate service.

        Extracts key market data points from the analysis result to provide
        the Bull and Bear analysts with relevant context without sending
        the entire raw response.

        Args:
            result: Analysis result dictionary

        Returns:
            Formatted context string for debate
        """
        lines = []

        # Trend info
        analysis = result.get("analysis", {})
        trend = analysis.get("trend", {})
        if trend:
            lines.append(
                f"Trend: {trend.get('direction', 'N/A')} | "
                f"4h Strength: {trend.get('strength_4h', 'N/A')} | "
                f"Alignment: {trend.get('timeframe_alignment', 'N/A')}"
            )

        # Technical data
        tech = result.get("technical_data", {})
        if tech:
            lines.append(
                f"RSI: {tech.get('rsi', 'N/A')} | "
                f"ADX: {tech.get('adx', 'N/A')} | "
                f"ATR%: {tech.get('atr_percentage', 'N/A')}"
            )

            macd = tech.get("macd", {})
            if isinstance(macd, dict):
                lines.append(f"MACD Signal: {macd.get('signal', 'N/A')}")

            bb = tech.get("bollinger_bands", {})
            if isinstance(bb, dict):
                lines.append(f"BB Position: {bb.get('percent_b', 'N/A')}")

        # Confluence factors
        confluence = analysis.get("confluence_factors", {})
        if isinstance(confluence, dict):
            factor_strs = [f"{k}: {v}" for k, v in confluence.items()]
            lines.append(f"Confluence: {', '.join(factor_strs)}")

        # Sentiment
        ctx = result.get("context")
        if ctx and hasattr(ctx, "market_sentiment"):
            lines.append(f"Sentiment: {ctx.market_sentiment}")

        return "\n".join(lines) if lines else "No detailed analysis data available."

    async def _confirm_live_order(
        self, action: str, symbol: str, quantity: float, price: float
    ) -> bool:
        """Ask for user confirmation before placing a live order.

        Only used when LIVE_CONFIRM_ORDERS is True and executor is live.
        Uses asyncio stdin to avoid blocking the event loop.

        Args:
            action: "BUY" or "SELL"
            symbol: Trading pair
            quantity: Order quantity
            price: Order price

        Returns:
            True if confirmed, False if rejected
        """
        import sys

        value = quantity * price
        prompt = (
            f"\n{'=' * 50}\n"
            f"  LIVE ORDER CONFIRMATION\n"
            f"  {action} {quantity:.6f} {symbol} @ ${price:,.2f}\n"
            f"  Total value: ${value:,.2f}\n"
            f"{'=' * 50}\n"
            f"  Type 'yes' to confirm, anything else to cancel: "
        )

        sys.stdout.write(prompt)
        sys.stdout.flush()

        loop = asyncio.get_event_loop()
        try:
            answer = await asyncio.wait_for(
                loop.run_in_executor(None, sys.stdin.readline),
                timeout=60.0,
            )
            confirmed = answer.strip().lower() == "yes"
            if not confirmed:
                self.logger.info("Live order CANCELLED by user")
            return confirmed
        except asyncio.TimeoutError:
            self.logger.warning("Live order confirmation timed out (60s) — cancelled")
            return False

    # ── AI Safety Gates ─────────────────────────────────────────────────────

    _CONFIDENCE_RANK = {"LOW": 1, "MEDIUM": 2, "HIGH": 3}
    _CLOSE_ACTIONS = frozenset({"CLOSE", "CLOSE_LONG", "CLOSE_SHORT"})

    def _check_ai_safety_gates(
        self,
        confidence: str,
        stop_loss: Optional[float],
        take_profit: Optional[float],
        current_price: float,
        signal: str,
        debate_result=None,
    ) -> Optional[str]:
        """Pre-entry safety checks for AI trades.

        Returns a human-readable block reason if any gate triggers,
        or None when all gates pass. Invoked in ``_open_new_position``
        only when ``source='ai'``.

        Gates:
          1. Minimum confidence (blocks LOW by default)
          2. Consecutive-loss cooldown (pauses after N losses in a row)
          3. Minimum RR after round-trip fees (blocks trades too tight to
             overcome fees)
        """
        if self.config is None:
            return None

        # Gate 1 — Min confidence --------------------------------------------
        min_conf = getattr(self.config, "AI_MIN_CONFIDENCE", "LOW")
        required_rank = self._CONFIDENCE_RANK.get(min_conf, 1)
        actual_rank = self._CONFIDENCE_RANK.get(str(confidence).upper().strip(), 1)
        if actual_rank < required_rank:
            return (
                f"confidence={confidence} below required min={min_conf} "
                f"(config [ai_safety].min_confidence)"
            )

        # Gate 1.5 — Debate verdict ------------------------------------------
        if debate_result and hasattr(debate_result, "verdict"):
            blocked_verdicts = getattr(
                self.config, "AI_BLOCKED_DEBATE_VERDICTS", ["NEUTRAL"]
            )
            if debate_result.verdict in blocked_verdicts:
                return (
                    f"debate verdict={debate_result.verdict} is blocked "
                    f"(config [ai_safety].blocked_debate_verdicts)"
                )

        # Gate 2 — Consecutive-loss cooldown ---------------------------------
        loss_threshold = int(getattr(self.config, "AI_CONSECUTIVE_LOSS_THRESHOLD", 0))
        if loss_threshold > 0:
            block_reason = self._check_consecutive_loss_cooldown(loss_threshold)
            if block_reason:
                return block_reason

        # Gate 2.5 - Post-trade cooldown -------------------------------------
        post_trade_cooldown_min = int(
            getattr(self.config, "AI_POST_TRADE_COOLDOWN_MINUTES", 0)
        )
        if post_trade_cooldown_min > 0:
            block_reason = self._check_post_trade_cooldown(post_trade_cooldown_min)
            if block_reason:
                return block_reason

        # Gate 3 - Min RR after fees -----------------------------------------
        min_rr = float(getattr(self.config, "AI_MIN_RR_AFTER_FEES", 0.0))
        if min_rr > 0 and stop_loss and take_profit and current_price > 0:
            rr_reason = self._check_min_rr_after_fees(
                stop_loss=stop_loss,
                take_profit=take_profit,
                current_price=current_price,
                signal=signal,
                min_rr=min_rr,
            )
            if rr_reason:
                return rr_reason

        return None

    def _check_consecutive_loss_cooldown(self, threshold: int) -> Optional[str]:
        """Block if last N closed AI trades in history are all losses."""
        try:
            history = self.persistence.load_trade_history() or []
        except Exception as e:
            self.logger.debug("Safety gate: failed to load history: %s", e)
            return None

        # Walk history in reverse, count loss streak from most recent close
        streak = 0
        last_loss_ts: Optional[datetime] = None
        for row in reversed(history):
            action = row.get("action", "")
            if action not in self._CLOSE_ACTIONS:
                continue
            pnl_pct = self._extract_pnl_from_row(row)
            if pnl_pct is None:
                break
            if pnl_pct < 0:
                streak += 1
                if last_loss_ts is None:
                    ts_str = row.get("timestamp")
                    if ts_str:
                        try:
                            ts = datetime.fromisoformat(ts_str)
                            last_loss_ts = (
                                ts.replace(tzinfo=timezone.utc)
                                if ts.tzinfo is None
                                else ts
                            )
                        except (ValueError, TypeError):
                            pass
            else:
                break

        if streak < threshold or last_loss_ts is None:
            return None

        cooldown_min = int(
            getattr(self.config, "AI_CONSECUTIVE_LOSS_COOLDOWN_MINUTES", 60)
        )
        cooldown_until = last_loss_ts + timedelta(minutes=cooldown_min)
        now = datetime.now(timezone.utc)
        if now >= cooldown_until:
            return None

        remaining_min = int((cooldown_until - now).total_seconds() / 60)
        return (
            f"consecutive-loss cooldown active: {streak} losses in a row, "
            f"{remaining_min}min remaining "
            f"(config [ai_safety].consecutive_loss_threshold={threshold})"
        )

    def _check_post_trade_cooldown(self, cooldown_min: int) -> Optional[str]:
        """Block if the latest AI close is still inside its cooldown window."""
        try:
            history = self.persistence.load_trade_history() or []
        except Exception as e:
            self.logger.debug("Safety gate: failed to load history: %s", e)
            return None

        last_close_ts: Optional[datetime] = None
        for row in reversed(history):
            action = row.get("action", "")
            if action not in self._CLOSE_ACTIONS:
                continue
            source = row.get("source") or "ai"
            if str(source).lower() == "fast":
                continue
            ts_str = row.get("timestamp")
            if not ts_str:
                continue
            try:
                ts = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
                last_close_ts = (
                    ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts
                )
                break
            except (ValueError, TypeError):
                continue

        if last_close_ts is None:
            return None

        cooldown_until = last_close_ts + timedelta(minutes=cooldown_min)
        now = datetime.now(timezone.utc)
        if now >= cooldown_until:
            return None

        remaining_min = int((cooldown_until - now).total_seconds() / 60)
        return (
            f"post-trade cooldown active after latest AI close: "
            f"{remaining_min}min remaining "
            f"(config [ai_safety].post_trade_cooldown_minutes={cooldown_min})"
        )

    def _check_min_rr_after_fees(
        self,
        stop_loss: float,
        take_profit: float,
        current_price: float,
        signal: str,
        min_rr: float,
    ) -> Optional[str]:
        """Block if net reward (after round-trip fees) / risk < threshold."""
        fee_pct = float(getattr(self.config, "TRANSACTION_FEE_PERCENT", 0.00075))
        round_trip_fee = 2.0 * fee_pct

        if signal == "BUY":
            reward_pct = (take_profit - current_price) / current_price
            risk_pct = (current_price - stop_loss) / current_price
        else:  # SELL / SHORT
            reward_pct = (current_price - take_profit) / current_price
            risk_pct = (stop_loss - current_price) / current_price

        if risk_pct <= 0 or reward_pct <= 0:
            # Invalid levels — let RiskManager handle/correct downstream
            return None

        net_reward = reward_pct - round_trip_fee
        if net_reward <= 0:
            return (
                f"TP too tight vs fees: net_reward={net_reward * 100:+.3f}% "
                f"after {round_trip_fee * 100:.3f}% round-trip fees "
                f"(config [ai_safety].min_rr_after_fees={min_rr})"
            )

        rr_net = net_reward / risk_pct
        if rr_net < min_rr:
            return (
                f"RR after fees too low: {rr_net:.2f} < {min_rr} "
                f"(TP:+{reward_pct * 100:.2f}%, SL:-{risk_pct * 100:.2f}%, "
                f"fees:{round_trip_fee * 100:.3f}%)"
            )
        return None

    @staticmethod
    def _extract_pnl_from_row(row: Dict[str, Any]) -> Optional[float]:
        """Extract P&L % from a trade history row (direct field or reasoning)."""
        for key in ("pnl_pct", "pnl_percent", "pnl_percentage"):
            if key in row and row[key] is not None:
                try:
                    return float(row[key])
                except (TypeError, ValueError):
                    pass
        reasoning = row.get("reasoning", "")
        if isinstance(reasoning, str) and "P&L:" in reasoning:
            try:
                frag = reasoning.split("P&L:")[1].strip()
                num = frag.split("%")[0].strip().replace("+", "")
                return float(num)
            except (IndexError, ValueError):
                pass
        return None
