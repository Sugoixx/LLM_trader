"""Main entry point for the Crypto Trading Bot application.

This module defines the `CryptoTradingBot` class, which orchestrates the interaction
between various components like the market analyzer, trading strategy, and external APIs.
"""

import asyncio
import time
from datetime import datetime, timezone
from typing import Optional, Dict, Any

import numpy as np

try:
    import psutil

    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False

from src.logger.logger import Logger
from src.utils.timeframe_validator import TimeframeValidator
from src.managers.persistence_manager import PersistenceManager
from src.contracts.model_contract import ModelManagerProtocol
from src.trading import (
    TradingBrainService,
    TradingStatisticsService,
    TradingMemoryService,
)
from src.trading.algo_strategies.fast_trader import AlgoFastTrader
from src.trading.algo_strategies.safety_guard import (
    FastTradingSafetyGuard,
    FastTradingConfig,
)


# Configuration Constants
POSITION_UPDATE_INTERVAL = 3600  # 1 hour
SLEEP_CHUNK_SIZE = 1.0  # Check for interruptions every second
CANDLE_BUFFER_SECONDS = 2  # Seconds to wait after candle start
ERROR_WAIT_SHORT = 60  # Seconds to wait after minor error
ERROR_WAIT_LONG = 300  # Seconds to wait after major error


class CryptoTradingBot:
    """Automated crypto trading bot - TRADING MODE ONLY."""

    def __init__(
        self,
        logger: Logger,
        config,
        shutdown_manager: Optional[Any],
        exchange_manager,
        market_analyzer,
        trading_strategy,
        discord_notifier,
        keyboard_handler,
        rag_engine,
        coingecko_api,
        news_client,
        market_api,
        categories_api,
        alternative_me_api,
        cryptocompare_session,
        persistence: PersistenceManager,
        model_manager: ModelManagerProtocol,
        brain_service: TradingBrainService,
        statistics_service: TradingStatisticsService,
        memory_service: TradingMemoryService,
        dashboard_state=None,
        discord_task: Optional[asyncio.Task] = None,
    ):
        # pylint: disable=too-many-arguments, too-many-locals
        # Reason: Dependency Injection pattern requires all components to be injected.
        """Initialize bot with all dependencies injected.

        All components are injected via constructor following the Dependency
        Injection pattern, with start.py acting as the composition root.
        """
        self.logger = logger
        self.config = config
        self.shutdown_manager = shutdown_manager

        # Injected core components
        self.exchange_manager = exchange_manager
        self.market_analyzer = market_analyzer
        self.trading_strategy = trading_strategy
        self.discord_notifier = discord_notifier
        self.keyboard_handler = keyboard_handler
        self.rag_engine = rag_engine

        # Injected API clients
        self.coingecko_api = coingecko_api
        self.news_client = news_client
        self.market_api = market_api
        self.categories_api = categories_api
        self.alternative_me_api = alternative_me_api
        self.cryptocompare_session = cryptocompare_session

        # Injected trading services
        self.persistence = persistence
        self.model_manager = model_manager
        self.brain_service = brain_service
        self.statistics_service = statistics_service
        self.memory_service = memory_service
        self.dashboard_state = dashboard_state

        # Runtime state
        self.tasks = []
        self.running = False
        self._active_tasks = set()
        self._force_analysis = asyncio.Event()
        self._discord_task = discord_task
        self._position_status_task: Optional[asyncio.Task] = None
        self._last_preopen_analysis_target: Optional[str] = None

        # Layer 2 signal bus (injected by CompositionRoot if execution engine is enabled)
        self.signal_bus = None

        # Fast Trading Mode — algo consensus trader (stateless helper)
        self._fast_trader = AlgoFastTrader()
        self.last_llm_signal: Optional[str] = None

        # Fast Trading Mode — safety guards (min-interval / daily loss / streak cooldown)
        _ft_cfg = FastTradingConfig(
            min_interval_seconds=getattr(config, "FAST_MIN_INTERVAL_SECONDS", 900),
            daily_loss_pct_limit=getattr(config, "FAST_DAILY_LOSS_PCT_LIMIT", -3.0),
            consecutive_loss_threshold=getattr(
                config, "FAST_CONSECUTIVE_LOSS_THRESHOLD", 3
            ),
            consecutive_loss_cooldown_seconds=getattr(
                config, "FAST_CONSECUTIVE_LOSS_COOLDOWN_SECONDS", 7200
            ),
            min_confidence=getattr(config, "FAST_MIN_CONFIDENCE", "MEDIUM"),
            min_rr_after_fees=getattr(config, "FAST_MIN_RR_AFTER_FEES", 0.0),
            max_signal_age_seconds=getattr(
                config, "FAST_MAX_SIGNAL_AGE_SECONDS", 900
            ),
        )
        self._fast_guard = FastTradingSafetyGuard(
            logger=self.logger,
            persistence=self.persistence,
            statistics_service=self.statistics_service,
            config=_ft_cfg,
        )
        # Lock serialising trade execution between the main LLM loop and the
        # independent fast-trading poll loop (prevents concurrent modification
        # of ``trading_strategy.current_position``).
        self._fast_trade_lock = asyncio.Lock()

        # Pre-close gap-authorization cache. Tuple: ((symbol, next_close_iso, signal), allowed, reason)
        # Invalidated automatically because the key rotates each session.
        self._gap_verdict_cache: Optional[tuple] = None

        # In-process symbol switch support. When set, the main run() loop
        # exits cleanly and the composition root re-launches bot.run() with
        # the new symbol. See ``request_symbol_switch``.
        self._switch_requested: Optional[str] = None

        # Trading state
        self.current_exchange = None
        self.current_symbol: Optional[str] = None
        self.current_timeframe: Optional[str] = None

    async def initialize(self):
        """Initialize all components."""
        if self.shutdown_manager:
            self.shutdown_manager.register_shutdown_callback(self.shutdown)

            # Register components for shutdown
            if self.keyboard_handler:
                self.shutdown_manager.register_shutdown_callback(
                    self.keyboard_handler.stop_listening
                )

            if self.model_manager:
                self.shutdown_manager.register_shutdown_callback(
                    self.model_manager.close
                )

            if self.market_analyzer:
                self.shutdown_manager.register_shutdown_callback(
                    self.market_analyzer.close
                )

            if self.rag_engine:
                self.shutdown_manager.register_shutdown_callback(self.rag_engine.close)

            if self.exchange_manager:
                self.shutdown_manager.register_shutdown_callback(
                    self.exchange_manager.shutdown
                )

            if self.cryptocompare_session:
                self.shutdown_manager.register_shutdown_callback(
                    self.cryptocompare_session.close
                )

            # API clients (if they have close method)
            for client in [
                self.alternative_me_api,
                self.coingecko_api,
                self.news_client,
                self.market_api,
                self.categories_api,
            ]:
                if client:
                    try:
                        self.shutdown_manager.register_shutdown_callback(client.close)
                    except AttributeError:
                        pass

        # Register keyboard commands
        self.keyboard_handler.register_command(
            "a", self._force_analysis_now, "Force immediate analysis"
        )
        self.keyboard_handler.register_command(
            "h", self._show_help, "Show available keyboard commands"
        )
        self.keyboard_handler.register_command(
            "q", self._request_shutdown, "Quit the application"
        )

        # Start keyboard handler task
        keyboard_task = asyncio.create_task(
            self.keyboard_handler.start_listening(), name="Keyboard-Handler"
        )
        self._active_tasks.add(keyboard_task)
        keyboard_task.add_done_callback(self._active_tasks.discard)
        self.tasks.append(keyboard_task)

        self.logger.info("Crypto Trading Bot ready")

    async def shutdown(self):
        """Callback for graceful shutdown."""
        self.logger.info("Signaling trading loops to stop...")
        self.running = False

        # Cancel active tasks managed by bot
        pending_tasks = list(self._active_tasks)
        if pending_tasks:
            self.logger.info("Cancelling %s bot-specific tasks...", len(pending_tasks))
            for task in pending_tasks:
                if not task.done():
                    task.cancel()
            try:
                await asyncio.wait(pending_tasks, timeout=3.0)
            except asyncio.TimeoutError:
                self.logger.warning("Bot tasks shutdown timed out")

        # Discord task cleanup
        if self._discord_task and not self._discord_task.done():
            self._discord_task.cancel()
            try:
                await asyncio.wait_for(self._discord_task, timeout=1.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

        self.logger.info("Bot shutdown signaling complete.")

    async def request_symbol_switch(
        self, new_symbol: str, close_position: bool = False
    ) -> Dict[str, Any]:
        """Request an in-process switch to a new trading symbol.

        The main ``run()`` loop will exit cleanly and the composition root
        will re-launch trading on the new symbol (re-initialising the analyzer
        and the Layer 2 execution engine).

        Args:
            new_symbol: Canonical symbol (e.g. ``"EURUSD"`` or ``"BTC/USDT"``).
            close_position: If True and a position is open, attempt to close
                it at market before switching. If False and a position is
                open, the switch is refused.

        Returns:
            ``{"ok": bool, "new_symbol": str?, "reason": str?, "position": dict?}``
        """
        current = self.current_symbol or self.config.CRYPTO_PAIR
        if new_symbol.strip().upper() == str(current).upper():
            return {"ok": False, "reason": "same_symbol"}

        # Refuse / handle an open position
        pos = self.trading_strategy.current_position
        if pos is not None:
            if not close_position:
                return {
                    "ok": False,
                    "reason": "position_open",
                    "position": {
                        "symbol": getattr(pos, "symbol", None),
                        "direction": getattr(pos, "direction", None),
                        "entry_price": getattr(pos, "entry_price", None),
                        "size": getattr(pos, "size", None),
                    },
                }
            # Close at market using the latest available price
            try:
                current_price = None
                if self.current_exchange and self.current_symbol:
                    try:
                        ticker = await self.current_exchange.fetch_ticker(
                            self.current_symbol
                        )
                        current_price = float(
                            ticker.get("last")
                            or ticker.get("bid")
                            or ticker.get("ask")
                            or 0
                        )
                    except Exception as ticker_err:
                        self.logger.warning(
                            "[SWITCH] fetch_ticker failed: %s", ticker_err
                        )
                if not current_price:
                    # Fallback on the position's entry price so the close still records
                    current_price = float(getattr(pos, "entry_price", 0.0) or 0.0)
                if current_price <= 0:
                    return {"ok": False, "reason": "close_failed: no_price"}
                self.logger.warning(
                    "[SWITCH] Closing open position before switching to %s (price=%s)",
                    new_symbol,
                    current_price,
                )
                await self.trading_strategy.manual_close_position(current_price)
            except Exception as close_err:
                self.logger.exception("[SWITCH] Failed to close position during switch")
                return {"ok": False, "reason": f"close_failed: {close_err}"}

        # Persist to config.ini so the choice survives process restarts
        try:
            if hasattr(self.config, "persist_config_value"):
                self.config.persist_config_value("general", "crypto_pair", new_symbol)
        except Exception as persist_err:
            self.logger.warning(
                "[SWITCH] Could not persist symbol to config: %s", persist_err
            )

        # Signal the main loop to exit; composition root picks up the switch
        self._switch_requested = new_symbol
        self.running = False
        self._force_analysis.set()  # wake up any waiting sleep

        self.logger.warning(
            "[SWITCH] Symbol switch requested: %s -> %s (close_position=%s)",
            current,
            new_symbol,
            close_position,
        )
        return {"ok": True, "new_symbol": new_symbol}

    async def run(self, symbol: str, timeframe: str = None):
        """Run the trading bot in continuous mode.

        Args:
            symbol: Trading pair (e.g., "BTC/USDT")
            timeframe: Optional timeframe override
        """
        self.current_symbol = symbol
        self.current_timeframe = timeframe or self.config.TIMEFRAME

        # Find exchange that supports the symbol
        exchange, exchange_id = await self.exchange_manager.find_symbol_exchange(symbol)
        if not exchange:
            self.logger.error("Symbol %s not found on any configured exchange", symbol)
            return

        self.current_exchange = exchange
        self.logger.info("Starting trading for %s on %s", symbol, exchange_id)
        self.logger.info("Timeframe: %s", self.current_timeframe)

        # Initialize analyzer for this symbol
        self.market_analyzer.initialize_for_symbol(
            symbol=symbol, exchange=exchange, timeframe=self.current_timeframe
        )

        # Sync broker positions at startup (before main loop)
        broker_position_count = await self.trading_strategy.sync_broker_positions(
            symbol, exchange
        )

        # Enable running state before starting any async loops
        self.running = True
        check_count = 0

        # Start the independent fast-trading poll loop as a background task.
        # It stays idle (fast_trading_enabled=False) until the user activates Fast Mode.
        fast_loop_task = asyncio.create_task(self._fast_trading_loop())

        # Log current position if any
        if self.trading_strategy.current_position:
            position = self.trading_strategy.current_position
            self.logger.info(
                "Existing position: %s @ $%s",
                position.direction,
                f"{position.entry_price:,.2f}",
            )
            # Start hourly position updates for existing position
            if self.discord_notifier:
                await self._start_position_status_updates()
        else:
            self.logger.info("No existing position")

        # Fetch initial price for dashboard (one-time startup call)
        await self._fetch_current_ticker()

        # Check if resuming from previous session (regardless of position status)
        last_analysis_time = self.persistence.get_last_analysis_time()
        if last_analysis_time:
            self.logger.info(
                "Resuming from last analysis at %s UTC",
                last_analysis_time.strftime("%Y-%m-%d %H:%M:%S"),
            )
            await self._wait_until_next_timeframe_after(last_analysis_time)
        self.logger.info("Ready for next analysis after wait")

        # Initial run is considered regular (unless we want to skipping update on restart, but safer to update)
        is_regular_run = True

        try:
            while self.running:
                try:
                    check_count += 1

                    # Log memory usage periodically
                    if check_count % 10 == 0 and HAS_PSUTIL:
                        memory_mb = psutil.Process().memory_info().rss / 1024 / 1024
                        self.logger.info("Memory usage: %.2f MB", memory_mb)
                        if memory_mb > 1000:  # 1GB alert threshold
                            self.logger.warning(
                                "High memory usage detected: %.2f MB", memory_mb
                            )

                    await self._execute_trading_check(
                        check_count,
                        force_news_update=is_regular_run,
                        is_candle_close=is_regular_run,
                    )

                    # Check if still running before waiting
                    if not self.running:
                        break

                    # Wait for next timeframe
                    # Returns True if forced (interrupted), False if waited full duration (regular)
                    was_forced_wait = await self._wait_for_next_timeframe()
                    is_regular_run = not was_forced_wait

                except asyncio.CancelledError:
                    self.logger.info("Trading cancelled")
                    self.running = False
                    break
                except Exception as e:
                    self.logger.error("Error in trading loop: %s", e)
                    await self._interruptible_sleep(ERROR_WAIT_SHORT)
        finally:
            # Ensure the fast-trading background loop is cleaned up on exit
            fast_loop_task.cancel()
            try:
                await fast_loop_task
            except asyncio.CancelledError:
                pass

    async def _execute_trading_check(
        self,
        check_count: int,
        force_news_update: bool = True,
        is_candle_close: bool = True,
    ):
        """Execute a single trading check iteration.

        When analysis_cooldown_minutes > 0, the LLM is only called once the
        cooldown has elapsed.  In between, the bot still fetches price and
        updates position metrics so Layer 2 always has fresh data.

        When ``fast_trading_enabled`` is True (Fast Trading Mode):
        - Algo strategy consensus drives position opens/closes on every candle.
        - LLM still runs on its normal cooldown but acts as a correction layer:
          only CLOSE / opposing signals from the LLM are honoured; BUY/SELL
          open-position signals are suppressed (algo handles those).
        - If the cooldown gates out the LLM, the last cached algo signals are
          used so the bot keeps trading without waiting for AI.
        """
        self._log_check_header(check_count)

        current_ticker, current_price = await self._fetch_ticker_data()
        await self._check_position_status(
            current_price, is_candle_close=is_candle_close
        )

        if await self._should_skip_llm_for_market_hours(
            is_candle_close=is_candle_close
        ):
            return

        fast_mode = bool(
            self.dashboard_state and self.dashboard_state.fast_trading_enabled
        )

        # --- Cooldown gate: skip LLM if not enough time has passed ---
        cooldown = self.config.ANALYSIS_COOLDOWN_MINUTES
        llm_skipped_by_cooldown = False
        if cooldown > 0 and is_candle_close:
            last = self.persistence.get_last_analysis_time()
            if last:
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                elapsed = (datetime.now(timezone.utc) - last).total_seconds()
                if elapsed < cooldown * 60:
                    remaining = cooldown * 60 - elapsed
                    self.logger.info(
                        "Analysis cooldown: %.0fs remaining (cooldown=%dm). Skipping LLM.",
                        remaining,
                        cooldown,
                    )
                    llm_skipped_by_cooldown = True

        # Fast Trading Mode — execute algo trade using last cached signals when LLM is gated
        if fast_mode and llm_skipped_by_cooldown:
            await self._execute_fast_trade_from_cache(current_price)
            return

        if llm_skipped_by_cooldown:
            return

        await self._execute_market_knowledge_update(force_news_update)

        self.logger.info("Running market analysis...")
        context_data = await self._build_analysis_context(current_price, current_ticker)
        result = await self.market_analyzer.analyze_market(**context_data)

        if "error" in result:
            self.logger.error("Analysis failed: %s", result["error"])
            return

        # Extract LLM signal for Fast Trading consensus
        raw_response = result.get("raw_response", "")
        if raw_response:
            llm_signal, _, _, _, _, _, _ = (
                self.trading_strategy.extractor.extract_trading_info(raw_response)
            )
            self.last_llm_signal = llm_signal

        # Broadcast latest algo strategy signals to dashboard via WebSocket
        if self.dashboard_state and getattr(
            self.market_analyzer, "last_algo_signals", None
        ):
            try:
                await self.dashboard_state.update_algo_signals(
                    self.market_analyzer.last_algo_signals
                )
            except Exception as _ae:
                self.logger.debug("Failed to broadcast algo signals: %s", _ae)

        self.persistence.save_last_analysis_time()

        self.logger.info(
            "[dispatch] fast_mode=%s double_trade=%s open_slots=%s",
            fast_mode,
            bool(getattr(self.config, "DOUBLE_TRADE_ENABLED", False)),
            list(self.trading_strategy.positions.keys()),
        )

        if fast_mode:
            # Fast Trading Mode: algo signals drive the Fast slot
            decision = await self._execute_fast_trade_from_signals(current_price)
            # LLM correction on Fast slot: CLOSE / opposing signals only
            await self._apply_llm_correction(result, self.current_symbol, current_price)
            # When double_trade is enabled, also let the LLM drive the AI slot
            # (independent from the Fast slot — different magic # on MT5).
            ai_decision = None
            if bool(getattr(self.config, "DOUBLE_TRADE_ENABLED", False)):
                ai_decision = await self.trading_strategy.process_analysis(
                    result, self.current_symbol
                )
        else:
            # Normal mode: LLM drives all trading decisions
            decision = await self.trading_strategy.process_analysis(
                result, self.current_symbol
            )
            ai_decision = None

        # Process every non-null decision — never drop a slot silently.
        for _dec in (decision, ai_decision):
            if not _dec:
                continue
            await self.discord_notifier.send_trading_decision(
                _dec, self.config.MAIN_CHANNEL_ID
            )
            await self._handle_new_position(_dec, current_price)
            await self._publish_signal_for_decision(_dec, current_price)

        if not decision and not ai_decision:
            self.logger.info(
                "No trading action taken (fast_mode=%s, double_trade=%s) \u2014 "
                "check preceding logs for details",
                fast_mode,
                bool(getattr(self.config, "DOUBLE_TRADE_ENABLED", False)),
            )

        await self._send_discord_notification(result)
        self._save_analysis_data(result)

    async def _execute_fast_trade_from_signals(self, current_price: float):
        """Execute a trade from fresh algo signals (called after analyze_market()).

        Uses ``market_analyzer.last_algo_signals`` which is populated by the
        signal layer running *inside* the current analysis cycle.
        """
        algo_data = getattr(self.market_analyzer, "last_algo_signals", None)
        if not algo_data:
            self.logger.debug(
                "[Fast] No algo signals from this cycle — skipping fast trade"
            )
            return None
        async with self._fast_trade_lock:
            return await self._execute_fast_trade_core(
                algo_data, current_price, source="fresh"
            )

    async def _execute_fast_trade_from_cache(self, current_price: float):
        """Execute a trade using the last cached algo signals (LLM gated by cooldown).

        This keeps the bot trading on every candle in Fast Trading Mode even
        when the AI analysis cooldown prevents an LLM call.
        """
        algo_data = getattr(self.market_analyzer, "last_algo_signals", None)
        if not algo_data:
            self.logger.debug("[Fast] No cached algo signals available — skipping")
            return
        self.logger.info("[Fast] LLM on cooldown — trading from cached algo signals")
        async with self._fast_trade_lock:
            await self._execute_fast_trade_core(
                algo_data, current_price, source="cached"
            )

    async def _execute_fast_trade_core(
        self, algo_data: dict, current_price: float, source: str
    ):
        """Shared fast trade execution given an algo signals dict."""
        signals = algo_data.get("signals", [])
        market_condition = algo_data.get("market_condition")
        signal, confidence, reasoning = self._fast_trader.decide(
            signals, market_condition, self.last_llm_signal
        )

        regime = None
        volatility = None
        adx = None
        if isinstance(market_condition, dict):
            regime = market_condition.get("market_condition")
            volatility = market_condition.get("volatility_regime")
            adx = market_condition.get("adx")

        if isinstance(market_condition, dict):
            self.logger.info(
                "[Fast/%s] Market regime=%s volatility=%s adx=%s",
                source,
                regime,
                volatility,
                adx,
            )
        for sig in signals:
            try:
                sig_conf = float(sig.get("confidence", 0.0) or 0.0)
            except (TypeError, ValueError):
                sig_conf = 0.0
            self.logger.info(
                "[Fast/%s] %s => %s (%d%%) | %s",
                source,
                sig.get("strategy_name", "?"),
                sig.get("signal", "?"),
                round(sig_conf * 100),
                sig.get("explanation", ""),
            )

        def _record_fast_snapshot(outcome: str, detail: str | None = None) -> None:
            self._fast_guard.record_decision(
                {
                    "source": source,
                    "signal": signal,
                    "confidence": confidence,
                    "reasoning": reasoning,
                    "outcome": outcome,
                    "detail": detail,
                    "market_regime": regime,
                    "volatility_regime": volatility,
                    "adx": adx,
                    "signals": [
                        {
                            "strategy_name": s.get("strategy_name"),
                            "signal": s.get("signal"),
                            "confidence": s.get("confidence"),
                            "explanation": s.get("explanation"),
                        }
                        for s in signals
                    ],
                }
            )

        self.logger.info(
            "[Fast/%s] Consensus: %s | %s — %s", source, signal, confidence, reasoning
        )
        # Record consensus for dashboard diagnostics (even if HOLD/blocked)
        try:
            self._fast_guard.record_consensus(f"{signal} | {confidence} — {reasoning}")
        except Exception:  # pragma: no cover
            pass

        if signal == "HOLD":
            _record_fast_snapshot("HOLD")
            self.logger.info(
                "[Fast/%s] HOLD — no trade this cycle (reason: %s)", source, reasoning
            )
            await self._broadcast_fast_guard_state()
            return None

        has_fast_open = self.trading_strategy.has_position("fast")
        is_new_fast_open = (signal in ("BUY", "SELL")) and not has_fast_open

        if is_new_fast_open:
            block_reason = self._check_fast_entry_quality(algo_data, confidence)
            if block_reason:
                _record_fast_snapshot("BLOCKED", block_reason)
                self.logger.warning("[Fast/%s] BLOCKED: %s", source, block_reason)
                await self._broadcast_fast_guard_state()
                return None

        # ── Safety guards: min-interval, daily loss limit, loss-streak cooldown ──
        self._refresh_fast_guard_config()
        guard = self._fast_guard.check(has_open_position=has_fast_open)
        await self._broadcast_fast_guard_state()
        if not guard.allowed:
            _record_fast_snapshot("BLOCKED", guard.reason)
            self.logger.warning(
                "[Fast/%s] BLOCKED by safety guard: %s", source, guard.reason
            )
            await self._broadcast_fast_guard_state()
            return None

        # ── Market-hours proximity guard: block NEW opens before market close ──
        # Exits (CLOSE signals / opposing signals that close an existing position)
        # are always allowed — we never trap the bot inside a closing market.
        if is_new_fast_open:
            blocked, reason = await self._check_market_close_proximity(
                signal, reasoning
            )
            if blocked:
                _record_fast_snapshot("BLOCKED", reason)
                self.logger.warning("[Fast/%s] BLOCKED: %s", source, reason)
                await self._broadcast_fast_guard_state()
                return None

        market_conditions_for_risk = {}
        if isinstance(market_condition, dict):
            market_conditions_for_risk = dict(market_condition)
        market_conditions_for_risk.setdefault("symbol", self.current_symbol or "")

        decision = await self.trading_strategy.process_algo_decision(
            signal=signal,
            confidence=confidence,
            current_price=current_price,
            symbol=self.current_symbol or "",
            reasoning=reasoning,
            market_conditions=market_conditions_for_risk,
        )
        _record_fast_snapshot(
            getattr(decision, "action", None) or "NO_DECISION",
            getattr(decision, "reasoning", None) if decision else None,
        )
        await self._broadcast_fast_guard_state()
        return decision

    def _check_fast_entry_quality(
        self, algo_data: Dict[str, Any], confidence: str
    ) -> Optional[str]:
        """Return a blocking reason for weak/stale fast entries, else None."""
        min_conf = str(getattr(self.config, "FAST_MIN_CONFIDENCE", "LOW")).upper()
        rank = {"LOW": 1, "MEDIUM": 2, "HIGH": 3}
        required = rank.get(min_conf, 1)
        actual = rank.get(str(confidence).upper(), 1)
        if actual < required:
            return (
                f"fast confidence {confidence} below required {min_conf} "
                "(config [fast_trading].min_confidence)"
            )

        max_age = int(getattr(self.config, "FAST_MAX_SIGNAL_AGE_SECONDS", 0) or 0)
        if max_age <= 0:
            return None

        age = self._fast_signal_age_seconds(algo_data)
        if age is None:
            return (
                "fast signal timestamp missing/invalid "
                "(config [fast_trading].max_signal_age_seconds)"
            )
        if age > max_age:
            return (
                f"fast signal is stale: {age}s old > {max_age}s "
                "(config [fast_trading].max_signal_age_seconds)"
            )
        return None

    @staticmethod
    def _fast_signal_age_seconds(algo_data: Dict[str, Any]) -> Optional[int]:
        ts_raw = algo_data.get("timestamp")
        if not ts_raw:
            return None
        try:
            ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - ts).total_seconds()
        return max(0, int(age))

    def _refresh_fast_guard_config(self) -> None:
        """Keep fast guard thresholds in sync with runtime settings."""
        cfg = self._fast_guard.config
        cfg.min_interval_seconds = int(
            getattr(self.config, "FAST_MIN_INTERVAL_SECONDS", cfg.min_interval_seconds)
        )
        cfg.daily_loss_pct_limit = float(
            getattr(self.config, "FAST_DAILY_LOSS_PCT_LIMIT", cfg.daily_loss_pct_limit)
        )
        cfg.consecutive_loss_threshold = int(
            getattr(
                self.config,
                "FAST_CONSECUTIVE_LOSS_THRESHOLD",
                cfg.consecutive_loss_threshold,
            )
        )
        cfg.consecutive_loss_cooldown_seconds = int(
            getattr(
                self.config,
                "FAST_CONSECUTIVE_LOSS_COOLDOWN_SECONDS",
                cfg.consecutive_loss_cooldown_seconds,
            )
        )
        cfg.min_confidence = str(
            getattr(self.config, "FAST_MIN_CONFIDENCE", cfg.min_confidence)
        ).upper()
        cfg.min_rr_after_fees = float(
            getattr(self.config, "FAST_MIN_RR_AFTER_FEES", cfg.min_rr_after_fees)
        )
        cfg.max_signal_age_seconds = int(
            getattr(
                self.config,
                "FAST_MAX_SIGNAL_AGE_SECONDS",
                cfg.max_signal_age_seconds,
            )
        )

    async def _broadcast_fast_guard_state(self) -> None:
        """Push current safety-guard state to the dashboard (best-effort)."""
        if not self.dashboard_state:
            return
        try:
            self._refresh_fast_guard_config()
            snap = self._fast_guard.snapshot()
            await self.dashboard_state.update_fast_guard(snap)
        except Exception as e:  # pylint: disable=broad-exception-caught
            self.logger.debug("Failed to broadcast fast guard state: %s", e)

    async def _check_market_close_proximity(
        self, signal: str, algo_reasoning: str
    ) -> tuple[bool, str]:
        """Decide whether a NEW fast-mode open should be blocked near market close.

        When the close is < ``FAST_BLOCK_MINUTES_BEFORE_CLOSE`` minutes away, a
        one-shot LLM consultation is performed to decide whether the setup is
        worth holding through the close (gap play).

        Returns:
            (blocked, reason) — blocked=True means do NOT open a new position.

        Rules:
        - Not blocked if MT5 is disabled (crypto markets are 24/7).
        - Not blocked if the user explicitly allows gap trading
          (``FAST_ALLOW_GAP_TRADING=True`` — bypass without LLM check).
        - Not blocked if no ``next_close_utc`` is available (data missing).
        - If within the pre-close window: consult LLM once per session-close;
          verdict is cached by ``next_close_utc`` so subsequent polls reuse it.
        - Fail-closed: any LLM error or timeout results in BLOCK (safety).
        """
        threshold_min = int(getattr(self.config, "FAST_BLOCK_MINUTES_BEFORE_CLOSE", 15))
        if threshold_min <= 0:
            return False, ""
        if bool(getattr(self.config, "FAST_ALLOW_GAP_TRADING", False)):
            return False, ""
        if not getattr(self.config, "MT5_ENABLED", False):
            return False, ""
        if not self.current_exchange or not hasattr(
            self.current_exchange, "get_market_status"
        ):
            return False, ""

        try:
            stale_seconds = getattr(self.config, "MT5_MARKET_STALE_TICK_SECONDS", 1800)
            market_status = await self.current_exchange.get_market_status(
                self.current_symbol, stale_seconds=stale_seconds
            )
        except Exception as e:  # pylint: disable=broad-exception-caught
            self.logger.debug("[Fast] Market-status check failed, not blocking: %s", e)
            return False, ""

        next_close_raw = market_status.get("next_close_utc")
        if not next_close_raw:
            return False, ""

        try:
            next_close = datetime.fromisoformat(
                str(next_close_raw).replace("Z", "+00:00")
            )
            if next_close.tzinfo is None:
                next_close = next_close.replace(tzinfo=timezone.utc)
        except ValueError:
            return False, ""

        minutes_to_close = (
            next_close - datetime.now(timezone.utc)
        ).total_seconds() / 60.0
        if not (0 < minutes_to_close < threshold_min):
            return False, ""

        # ── In pre-close window: consult LLM (cached per close timestamp) ──
        cache_key = (self.current_symbol, next_close.isoformat(), signal)
        if (
            self._gap_verdict_cache is not None
            and self._gap_verdict_cache[0] == cache_key
        ):
            verdict, verdict_reason = (
                self._gap_verdict_cache[1],
                self._gap_verdict_cache[2],
            )
            self.logger.info(
                "[Fast/Gap] Cached LLM verdict: %s — %s",
                "ALLOW" if verdict else "BLOCK",
                verdict_reason,
            )
            return (not verdict), verdict_reason

        self.logger.info(
            "[Fast/Gap] Close in %.1fmin — requesting LLM authorization for %s (%s)",
            minutes_to_close,
            signal,
            algo_reasoning,
        )
        allowed, verdict_reason = await self._request_gap_authorization(
            signal=signal,
            algo_reasoning=algo_reasoning,
            minutes_to_close=minutes_to_close,
            next_close_utc=next_close,
        )
        self._gap_verdict_cache = (cache_key, allowed, verdict_reason)

        if allowed:
            return False, ""  # not blocked — LLM authorized gap play
        return True, f"LLM rejected gap play: {verdict_reason}"

    async def _request_gap_authorization(
        self,
        signal: str,
        algo_reasoning: str,
        minutes_to_close: float,
        next_close_utc: datetime,
    ) -> tuple[bool, str]:
        """Ask the LLM whether to hold ``signal`` through the upcoming close as a gap play.

        LLM must answer with ``GAP_OK`` or ``GAP_NO`` somewhere in its response.
        Fail-closed: any exception or ambiguous response returns (False, reason).
        """
        gap_instruction = (
            "\n\n=== PRE-CLOSE GAP DECISION REQUIRED ===\n"
            f"The fast-trading algo wants to open a new {signal} position, but "
            f"the market closes in {minutes_to_close:.1f} minute(s) "
            f"(at {next_close_utc.isoformat()}).\n"
            f"Algo reasoning: {algo_reasoning}\n\n"
            "Your task: decide whether this entry is good enough to HOLD THROUGH "
            "THE CLOSE as a gap play, or whether the overnight/weekend gap risk "
            "outweighs the edge.\n\n"
            "Consider: position directionality vs. overnight news risk, recent "
            "momentum/volume, position of the entry in the session range, and "
            "historical gap behaviour of this instrument.\n\n"
            "Respond with EXACTLY ONE of these tokens on its own line:\n"
            "  GAP_OK     — authorize holding through close (good gap setup)\n"
            "  GAP_NO     — reject (gap risk too high / setup not strong enough)\n"
            "Then briefly explain in ≤2 sentences."
        )

        try:
            result = await asyncio.wait_for(
                self.market_analyzer.analyze_market(additional_context=gap_instruction),
                timeout=45.0,
            )
        except asyncio.TimeoutError:
            return False, "LLM gap-analysis timed out (45s)"
        except Exception as e:  # pylint: disable=broad-exception-caught
            self.logger.warning("[Fast/Gap] LLM call failed: %s", e)
            return False, f"LLM error: {e}"

        if "error" in result:
            return False, f"LLM error: {result.get('error')}"

        raw = str(result.get("raw_response", "")).upper()
        if "GAP_OK" in raw and "GAP_NO" not in raw:
            # Extract a short reason snippet for logs/dashboard (first 180 chars after verdict)
            snippet = raw.split("GAP_OK", 1)[1][:180].strip().replace("\n", " ")
            return True, f"gap approved ({snippet})" if snippet else "gap approved"
        if "GAP_NO" in raw:
            snippet = raw.split("GAP_NO", 1)[1][:180].strip().replace("\n", " ")
            return False, f"gap rejected ({snippet})" if snippet else "gap rejected"
        return False, "LLM response did not contain GAP_OK/GAP_NO verdict"

    async def _fast_trading_loop(self) -> None:
        """Independent algo poll loop (always active).

        Runs every ``FAST_POLL_INTERVAL_SECONDS`` (default 5 min) regardless of
        the main LLM timeframe.  On each tick it:
          1. Fetches fresh short-interval OHLCV candles from the exchange (no LLM).
          2. Runs the algo signal layer (pure Python indicators).
          3. Updates the dashboard with fresh signals unconditionally.
          4. Calls ``_execute_fast_trade_core()`` **only** when fast trading is enabled.

        When ``fast_trading_enabled = False``: analysis and dashboard updates still
        run for monitoring purposes — no trade orders are placed.
        When ``fast_trading_enabled = True``: the bot can enter/exit positions every
        5 minutes while the LLM runs at its own cadence as a correction layer.
        """
        fast_ohlcv_timeframe = str(getattr(self.config, "FAST_TIMEFRAME", "5m"))
        # Keep a margin above the strict minimum required by current fast
        # strategies (BollingerReversion needs 220 bars: SMA200 + BB20).
        fast_ohlcv_limit = max(260, int(getattr(self.config, "FAST_OHLCV_LIMIT", 260)))

        interval = self.config.FAST_POLL_INTERVAL_SECONDS
        self.logger.info(
            "[FastLoop] Background algo poll loop started (interval=%ds)", interval
        )
        _idle_log_every_n = max(1, int(600 / max(interval, 1)))  # log ~ every 10 min
        _tick = 0

        try:
            while self.running:
                # Sleep first — main loop handles the immediate startup check
                await asyncio.sleep(interval)

                if not self.running:
                    break

                _tick += 1
                fast_mode = bool(
                    self.dashboard_state and self.dashboard_state.fast_trading_enabled
                )

                signal_layer = getattr(self.market_analyzer, "signal_layer", None)
                if not (signal_layer and self.current_exchange and self.current_symbol):
                    self.logger.info(
                        "[FastLoop] SKIP — not ready: signal_layer=%s exchange=%s symbol=%s",
                        bool(signal_layer),
                        bool(self.current_exchange),
                        bool(self.current_symbol),
                    )
                    continue

                try:
                    # ── 1. Fetch fresh short-interval candles (no LLM cost) ──
                    raw_ohlcv = await self.current_exchange.fetch_ohlcv(
                        self.current_symbol,
                        timeframe=fast_ohlcv_timeframe,
                        limit=fast_ohlcv_limit,
                    )
                    if not raw_ohlcv or len(raw_ohlcv) < 30:
                        self.logger.info(
                            "[FastLoop] SKIP — insufficient OHLCV (%s candles)",
                            len(raw_ohlcv) if raw_ohlcv else 0,
                        )
                        continue

                    ohlcv = np.array(raw_ohlcv, dtype=float)
                    current_price = float(ohlcv[-1, 4])  # last close as proxy

                    # ── 2. Run algo signal layer (pure indicator computation) ──
                    raw_result = signal_layer.run(ohlcv, self.current_symbol)
                    if raw_result is None:
                        self.logger.info(
                            "[FastLoop] SKIP — signal layer returned None (no "
                            "regime/consensus computed)"
                        )
                        continue

                    algo_data = {
                        **raw_result,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                    # Update shared cache so main loop / dashboard see fresh data
                    self.market_analyzer.last_algo_signals = algo_data

                    if self.dashboard_state:
                        try:
                            await self.dashboard_state.update_algo_signals(algo_data)
                        except Exception as _be:  # pylint: disable=broad-exception-caught
                            self.logger.debug(
                                "[FastLoop] Broadcast algo signals failed: %s", _be
                            )

                    if not fast_mode:
                        if _tick % _idle_log_every_n == 0:
                            self.logger.info(
                                "[FastLoop] Analysis mode — Fast Trading OFF, but signals updating"
                            )

                    # ── 3. Execute trade decision via shared core (guards apply) ──
                    if fast_mode:
                        async with self._fast_trade_lock:
                            await self._execute_fast_trade_core(
                                algo_data, current_price, source="poll"
                            )

                except asyncio.CancelledError:
                    raise
                except Exception as e:  # pylint: disable=broad-exception-caught
                    self.logger.error("[FastLoop] Error during poll: %s", e)

        except asyncio.CancelledError:
            pass
        finally:
            self.logger.info("[FastLoop] Background algo poll loop stopped")

    async def _apply_llm_correction(
        self, result: dict, symbol: str, current_price: float
    ):
        """Apply LLM analysis as a correction layer in Fast Trading Mode.

        Only CLOSE signals or high-confidence opposing signals from the LLM
        are honoured.  BUY/SELL open signals are suppressed — algo handles those.
        """
        try:
            raw_response = result.get("raw_response", "")
            if not raw_response or not self.trading_strategy.current_position:
                return  # Nothing to correct

            signal, confidence, *_ = (
                self.trading_strategy.extractor.extract_trading_info(raw_response)
            )
            if not signal:
                return

            current_pos = self.trading_strategy.current_position
            pos_dir = (
                current_pos.direction if current_pos else None
            )  # "LONG" or "SHORT"

            # Determine if the LLM wants to close the current position
            wants_close = (
                signal in ("CLOSE", "CLOSE_LONG", "CLOSE_SHORT")
                or (signal == "SELL" and pos_dir == "LONG" and confidence == "HIGH")
                or (signal == "BUY" and pos_dir == "SHORT" and confidence == "HIGH")
            )

            if wants_close:
                # In fast mode the algo owns the 'fast' slot. If only 'ai' is
                # open (single-slot mode), fall back to closing it instead.
                target_source = (
                    "fast" if "fast" in self.trading_strategy.positions else "ai"
                )
                self.logger.info(
                    "[Fast/LLM-Correction] AI says %s (%s) — closing %s "
                    "position (slot=%s)",
                    signal,
                    confidence,
                    pos_dir,
                    target_source,
                )
                market_conditions = self.trading_strategy._extract_market_conditions(
                    result
                )  # pylint: disable=protected-access
                async with self._fast_trade_lock:
                    await self.trading_strategy.close_position(
                        "llm_correction",
                        current_price,
                        market_conditions,
                        source=target_source,
                    )
            else:
                self.logger.debug(
                    "[Fast/LLM-Correction] AI says %s (%s) — no correction needed",
                    signal,
                    confidence,
                )
        except Exception as e:
            self.logger.warning(
                "[Fast/LLM-Correction] Error applying correction: %s", e
            )

    async def _should_skip_llm_for_market_hours(self, *, is_candle_close: bool) -> bool:
        """Gate LLM calls when MT5 market is closed, with one pre-open analysis call."""
        if not is_candle_close:
            return False
        if not getattr(self.config, "MT5_ENABLED", False):
            await self._publish_market_hours_status(
                {
                    "enabled": False,
                    "is_open": True,
                    "llm_paused": False,
                    "state": "disabled",
                    "reason": "mt5_disabled",
                }
            )
            return False
        if not getattr(self.config, "SKIP_LLM_WHEN_MARKET_CLOSED", True):
            await self._publish_market_hours_status(
                {
                    "enabled": False,
                    "is_open": True,
                    "llm_paused": False,
                    "state": "disabled",
                    "reason": "skip_gate_disabled",
                }
            )
            return False
        if not self.current_exchange or not hasattr(
            self.current_exchange, "get_market_status"
        ):
            return False

        stale_seconds = getattr(self.config, "MT5_MARKET_STALE_TICK_SECONDS", 1800)
        try:
            market_status = await self.current_exchange.get_market_status(
                self.current_symbol,
                stale_seconds=stale_seconds,
            )
        except Exception as e:
            self.logger.warning(
                "Market-hours check failed, keeping LLM analysis enabled: %s", e
            )
            await self._publish_market_hours_status(
                {
                    "enabled": True,
                    "is_open": True,
                    "llm_paused": False,
                    "state": "unknown",
                    "reason": f"status_error:{e}",
                }
            )
            return False

        if market_status.get("is_open", True):
            self._last_preopen_analysis_target = None
            await self._publish_market_hours_status(
                {
                    "enabled": True,
                    "is_open": True,
                    "llm_paused": False,
                    "state": "open",
                    "reason": str(market_status.get("reason", "open")),
                    "next_open_utc": market_status.get("next_open_utc"),
                    "preopen_analysis_done": False,
                }
            )
            return False

        reason = str(market_status.get("reason", "market_closed"))
        next_open_raw = market_status.get("next_open_utc")
        preopen_minutes = max(
            0, int(getattr(self.config, "PREOPEN_ANALYSIS_MINUTES", 20))
        )

        if next_open_raw:
            try:
                next_open_dt = datetime.fromisoformat(
                    str(next_open_raw).replace("Z", "+00:00")
                )
                if next_open_dt.tzinfo is None:
                    next_open_dt = next_open_dt.replace(tzinfo=timezone.utc)

                seconds_to_open = (
                    next_open_dt - datetime.now(timezone.utc)
                ).total_seconds()
                if 0 <= seconds_to_open <= preopen_minutes * 60:
                    next_open_key = next_open_dt.isoformat()
                    if self._last_preopen_analysis_target != next_open_key:
                        self._last_preopen_analysis_target = next_open_key
                        await self._publish_market_hours_status(
                            {
                                "enabled": True,
                                "is_open": False,
                                "llm_paused": False,
                                "state": "preopen_analysis",
                                "reason": reason,
                                "next_open_utc": next_open_key,
                                "seconds_to_open": seconds_to_open,
                                "preopen_analysis_done": True,
                            }
                        )
                        self.logger.info(
                            "Market closed (%s), but pre-open window active (%.0fs). Running one pre-open LLM analysis.",
                            reason,
                            seconds_to_open,
                        )
                        return False

                    await self._publish_market_hours_status(
                        {
                            "enabled": True,
                            "is_open": False,
                            "llm_paused": True,
                            "state": "closed_preopen_done",
                            "reason": reason,
                            "next_open_utc": next_open_key,
                            "seconds_to_open": seconds_to_open,
                            "preopen_analysis_done": True,
                        }
                    )
                    self.logger.info(
                        "Market closed (%s). Pre-open analysis already done for %s. Skipping LLM.",
                        reason,
                        next_open_key,
                    )
                    return True

                await self._publish_market_hours_status(
                    {
                        "enabled": True,
                        "is_open": False,
                        "llm_paused": True,
                        "state": "closed",
                        "reason": reason,
                        "next_open_utc": next_open_dt.isoformat(),
                        "seconds_to_open": seconds_to_open,
                        "preopen_analysis_done": False,
                    }
                )
                self.logger.info(
                    "Market closed (%s). Next open at %s (in %.0fs). Skipping LLM.",
                    reason,
                    next_open_dt.isoformat(),
                    seconds_to_open,
                )
                return True
            except Exception as e:
                self.logger.warning(
                    "Invalid next_open_utc value '%s': %s", next_open_raw, e
                )

        await self._publish_market_hours_status(
            {
                "enabled": True,
                "is_open": False,
                "llm_paused": True,
                "state": "closed",
                "reason": reason,
                "next_open_utc": None,
                "preopen_analysis_done": False,
            }
        )
        self.logger.info(
            "Market closed (%s). No valid next-open estimate. Skipping LLM.", reason
        )
        return True

    async def _publish_market_hours_status(self, payload: Dict[str, Any]) -> None:
        """Publish market-hours status for dashboard status badge."""
        if not self.dashboard_state:
            return
        data = dict(payload)
        data["updated_at"] = datetime.now(timezone.utc).isoformat()
        try:
            await self.dashboard_state.update_market_hours_status(data)
        except Exception as e:
            self.logger.debug("Failed to publish market-hours status: %s", e)

    def _log_check_header(self, check_count: int):
        """Log trading check header"""
        current_time = datetime.now()
        self.logger.info("=" * 60)
        self.logger.info(
            "Trading Check #%s at %s",
            check_count,
            current_time.strftime("%Y-%m-%d %H:%M:%S"),
        )
        self.logger.info("=" * 60)

    async def _fetch_ticker_data(self):
        """Fetch current ticker and price"""
        try:
            current_ticker = await self._fetch_current_ticker()
            if current_ticker:
                current_price = float(
                    current_ticker.get("last", current_ticker.get("close", 0))
                )
                return current_ticker, current_price
        except Exception as e:
            self.logger.warning("Could not fetch current ticker: %s", e)
        return None, None

    async def _check_position_status(
        self, current_price: Optional[float], *, is_candle_close: bool = True
    ):
        """Check if existing position hit stop/target.

        When Layer 2 execution engine is active, SL/TP is monitored
        in real-time via WebSocket — skip the candle-close check here.
        Forced analysis (keyboard 'a') skips automated stop checks
        but the AI can still consciously signal CLOSE.
        """
        if not (self.trading_strategy.current_position and current_price is not None):
            return

        # Layer 2 handles real-time SL/TP — only update MAE/MFE here
        if self.signal_bus is not None:
            self.trading_strategy.current_position.update_metrics(current_price)
            return

        if not is_candle_close:
            self.logger.info(
                "Intra-candle check: skipping SL/TP evaluation (soft stop mode)"
            )
            return

        try:
            close_reason = await self.trading_strategy.check_position(current_price)
            if close_reason:
                self.logger.info("Position closed: %s", close_reason)
                await self._stop_position_status_updates()
                if self.discord_notifier:
                    history = self.persistence.load_trade_history()
                    await self.discord_notifier.send_performance_stats(
                        trade_history=history,
                        symbol=self.current_symbol,
                        channel_id=self.config.MAIN_CHANNEL_ID,
                    )
        except Exception as e:
            self.logger.error("Error checking position: %s", e)

    async def _execute_market_knowledge_update(self, force_news_update: bool):
        """Update market knowledge based on analysis type"""
        if force_news_update:
            self.logger.info("Updating market knowledge (Regular Analysis)...")
            await self.rag_engine.update_if_needed(force_update=True)
        else:
            self.logger.info(
                "Skipping forced market knowledge update (Forced Analysis)"
            )
            await self.rag_engine.update_if_needed(force_update=False)

    async def _build_analysis_context(
        self, current_price: Optional[float], current_ticker
    ) -> Dict[str, Any]:
        """Build context data for market analysis"""
        position_context = await self.trading_strategy.get_position_context(
            current_price
        )
        memory_context = self.memory_service.get_context_summary(
            symbol=self.current_symbol or ""
        )
        statistics_context = self.statistics_service.get_context()

        if statistics_context:
            position_context = f"{position_context}\n\n{statistics_context}"

        previous_data = await self.persistence.async_load_previous_response()
        previous_response = previous_data.get("response") if previous_data else None
        previous_indicators = (
            previous_data.get("technical_indicators") if previous_data else None
        )

        last_analysis_time_str = self._get_formatted_last_analysis_time()
        dynamic_thresholds = self.brain_service.get_dynamic_thresholds()

        # Inject market-hours awareness when close is approaching so the LLM
        # can factor gap risk into its BUY/SELL/HOLD decision.
        additional_context = await self._build_market_close_context()

        return {
            "previous_response": previous_response,
            "previous_indicators": previous_indicators,
            "position_context": position_context,
            "performance_context": memory_context,
            "brain_service": self.brain_service,
            "last_analysis_time": last_analysis_time_str,
            "current_ticker": current_ticker,
            "dynamic_thresholds": dynamic_thresholds,
            "additional_context": additional_context,
        }

    async def _build_market_close_context(self) -> Optional[str]:
        """Return a pre-close advisory string for the LLM prompt, or None.

        When the market closes in less than ``FAST_BLOCK_MINUTES_BEFORE_CLOSE``
        minutes (reused as the "pre-close awareness window" for the LLM too),
        we inject a reminder so the AI explicitly weighs overnight/weekend gap
        risk before opening a new position. This is advisory only — the LLM
        remains the sole decider in normal mode.
        """
        if not getattr(self.config, "MT5_ENABLED", False):
            return None
        if not self.current_exchange or not hasattr(
            self.current_exchange, "get_market_status"
        ):
            return None

        threshold_min = int(getattr(self.config, "FAST_BLOCK_MINUTES_BEFORE_CLOSE", 15))
        if threshold_min <= 0:
            return None

        try:
            stale_seconds = getattr(self.config, "MT5_MARKET_STALE_TICK_SECONDS", 1800)
            market_status = await self.current_exchange.get_market_status(
                self.current_symbol, stale_seconds=stale_seconds
            )
        except Exception:  # pylint: disable=broad-exception-caught
            return None

        next_close_raw = market_status.get("next_close_utc")
        if not next_close_raw:
            return None

        try:
            next_close = datetime.fromisoformat(
                str(next_close_raw).replace("Z", "+00:00")
            )
            if next_close.tzinfo is None:
                next_close = next_close.replace(tzinfo=timezone.utc)
        except ValueError:
            return None

        minutes_to_close = (
            next_close - datetime.now(timezone.utc)
        ).total_seconds() / 60.0
        if not (0 < minutes_to_close < threshold_min):
            return None

        has_open = self.trading_strategy.current_position is not None
        directive = (
            "\n\n=== MARKET CLOSE IMMINENT ===\n"
            f"The market for {self.current_symbol} closes in {minutes_to_close:.1f} minute(s) "
            f"(at {next_close.isoformat()}).\n"
        )
        if has_open:
            directive += (
                "An open position will be carried through the close. "
                "Factor overnight/weekend gap risk into your CLOSE-vs-HOLD decision: "
                "if gap risk outweighs the remaining edge, prefer CLOSE.\n"
            )
        else:
            directive += (
                "Any new BUY/SELL you recommend will be held through the close. "
                "Only open a new position if the setup is strong enough to justify "
                "overnight/weekend gap exposure (directional conviction, benign news "
                "calendar, favourable historical gap behaviour). Otherwise prefer HOLD.\n"
            )
        return directive

    def _get_formatted_last_analysis_time(self) -> Optional[str]:
        """Get last analysis time formatted as UTC string"""
        last_analysis_time_obj = self.persistence.get_last_analysis_time()
        if not last_analysis_time_obj:
            return None

        if last_analysis_time_obj.tzinfo is None:
            last_analysis_time_obj = last_analysis_time_obj.astimezone(timezone.utc)
        else:
            last_analysis_time_obj = last_analysis_time_obj.astimezone(timezone.utc)

        return last_analysis_time_obj.strftime("%Y-%m-%d %H:%M:%S")

    async def _handle_new_position(self, decision, current_price: Optional[float]):
        """Handle new position creation and status updates"""
        # Look up the exact slot owned by this decision (AI or Fast) instead
        # of relying on current_position, which only exposes one slot.
        slot_source = getattr(decision, "source", "ai") or "ai"
        position = self.trading_strategy.positions.get(slot_source)
        if decision.action not in ("BUY", "SELL") or not position:
            return

        if not self.discord_notifier:
            return

        try:
            if current_price is None:
                ticker = await self._fetch_current_ticker()
                current_price = (
                    float(ticker.get("last", ticker.get("close", 0))) if ticker else 0.0
                )

            await self.discord_notifier.send_position_status(
                position=position,
                current_price=current_price,
                channel_id=self.config.MAIN_CHANNEL_ID,
            )
        except Exception as e:
            self.logger.warning("Error sending initial position status: %s", e)

        await self._start_position_status_updates()

    async def _send_discord_notification(self, result: Dict[str, Any]):
        """Send Discord notification with analysis results"""
        if self.discord_notifier:
            await self.discord_notifier.send_analysis_notification(
                result=result,
                symbol=self.current_symbol,
                timeframe=self.current_timeframe,
                channel_id=self.config.MAIN_CHANNEL_ID,
            )

    def _save_analysis_data(self, result: Dict[str, Any]):
        """Save analysis response and technical data"""
        raw_response = result.get("raw_response", "")
        if raw_response:
            technical_data = result.get("technical_data")
            generated_prompt = result.get("generated_prompt")
            self.persistence.save_previous_response(
                raw_response, technical_data, generated_prompt
            )

    async def _publish_signal_for_decision(
        self, decision, current_price: float
    ) -> None:
        """Publish a Layer 1 signal to the execution engine via SignalBus."""
        if not self.signal_bus:
            return
        try:
            from src.execution.signal_bus import Signal, SignalType

            slot_source = getattr(decision, "source", "ai") or "ai"

            if decision.action in ("BUY", "SELL"):
                signal = Signal(
                    signal_type=SignalType.OPEN,
                    symbol=decision.symbol,
                    direction="LONG" if decision.action == "BUY" else "SHORT",
                    confidence=decision.confidence,
                    price_at_signal=current_price or decision.price,
                    stop_loss=decision.stop_loss or 0.0,
                    take_profit=decision.take_profit or 0.0,
                    position_size=decision.position_size,
                    reasoning=decision.reasoning,
                    rating=decision.rating or "",
                    source=slot_source,
                )
            elif decision.action.startswith("CLOSE"):
                signal = Signal(
                    signal_type=SignalType.CLOSE,
                    symbol=decision.symbol,
                    price_at_signal=current_price or decision.price,
                    source=slot_source,
                )
            elif decision.action == "UPDATE":
                signal = Signal(
                    signal_type=SignalType.UPDATE,
                    symbol=decision.symbol,
                    stop_loss=decision.stop_loss or 0.0,
                    take_profit=decision.take_profit or 0.0,
                    price_at_signal=current_price or decision.price,
                    source=slot_source,
                )
            else:
                return

            await self.signal_bus.publish(signal)
        except Exception as e:
            self.logger.warning("Failed to publish signal to Layer 2: %s", e)

    async def _fetch_current_ticker(self) -> Optional[Dict[str, Any]]:
        """Fetch current ticker from exchange."""
        try:
            ticker = await self.current_exchange.fetch_ticker(self.current_symbol)
            if ticker and self.dashboard_state:
                price = float(ticker.get("last", ticker.get("close", 0)))
                if price > 0:
                    await self.dashboard_state.update_price(price)
            return ticker
        except Exception as e:
            self.logger.error("Error fetching current ticker: %s", e)
            return None

    async def _wait_for_next_timeframe(self):
        """Wait until the next timeframe candle starts."""
        try:
            current_time_ms = int(time.time() * 1000)

            # Calculate next candle start using validator (handles alignment)
            next_candle_ms = TimeframeValidator.calculate_next_candle_time(
                current_time_ms, self.current_timeframe
            )
            delay_ms = next_candle_ms - current_time_ms + (CANDLE_BUFFER_SECONDS * 1000)
            delay_seconds = max(0, delay_ms / 1000)

            next_check_time = datetime.fromtimestamp(
                next_candle_ms / 1000, timezone.utc
            )
            self.logger.info(
                "Next check at %s UTC (in %.0fs)",
                next_check_time.strftime("%Y-%m-%d %H:%M:%S"),
                delay_seconds,
            )
            if self.dashboard_state:
                await self.dashboard_state.update_next_check(next_check_time)
            return await self._interruptible_sleep(delay_seconds)

        except Exception as e:
            self.logger.error("Error calculating next timeframe: %s", e)
            await self._interruptible_sleep(ERROR_WAIT_LONG)
            return False

    async def _wait_until_next_timeframe_after(self, last_time: datetime):
        """Wait until the next timeframe candle after a specific timestamp.

        Args:
            last_time: Timestamp of last analysis
        """
        try:
            # Ensure last_time is timezone-aware (assume UTC if naive)
            if last_time.tzinfo is None:
                last_time = last_time.replace(tzinfo=timezone.utc)

            current_time_ms = int(time.time() * 1000)
            last_time_ms = int(last_time.timestamp() * 1000)

            # Calculate next candle after last analysis using validator (handles alignment)
            next_candle_ms = TimeframeValidator.calculate_next_candle_time(
                last_time_ms, self.current_timeframe
            )

            # Check if we're past the next candle boundary
            if current_time_ms >= next_candle_ms:
                self.logger.info(
                    "Resuming from last check at %s. Next candle already passed - proceeding immediately",
                    last_time.strftime("%Y-%m-%d %H:%M:%S"),
                )
                return

            # Wait for next candle
            # Use buffer to ensure we are safely into the next candle
            delay_ms = next_candle_ms - current_time_ms + (CANDLE_BUFFER_SECONDS * 1000)
            delay_seconds = max(0, delay_ms / 1000)
            next_check_time = datetime.fromtimestamp(
                next_candle_ms / 1000, timezone.utc
            )

            # Check if we're still within the same candle as the last analysis (for logging context)
            is_same = TimeframeValidator.is_same_candle(
                current_time_ms, last_time_ms, self.current_timeframe
            )

            context_msg = "Still in same candle" if is_same else "Resuming wait"
            self.logger.info(
                "Resuming from last check at %s. %s - next check at %s UTC (in %.0fs)",
                last_time.strftime("%Y-%m-%d %H:%M:%S"),
                context_msg,
                next_check_time.strftime("%Y-%m-%d %H:%M:%S"),
                delay_seconds,
            )

            if self.dashboard_state:
                await self.dashboard_state.update_next_check(next_check_time)
            await self._interruptible_sleep(delay_seconds)

        except Exception as e:
            self.logger.error("Error calculating wait time: %s", e)
            await self._interruptible_sleep(ERROR_WAIT_SHORT)

    async def _interruptible_sleep(
        self, seconds: float, respect_force_analysis: bool = True
    ):
        """Sleep in small chunks to allow responsive shutdown and force analysis.

        Uses SLEEP_CHUNK_SIZE to check for interruptions periodically.

        Args:
            seconds: Duration to sleep
            respect_force_analysis: If True, wake early on force analysis event (main loop only)

        Returns:
            bool: True if sleep was interrupted by force_analysis, False otherwise
        """
        start_time = time.monotonic()  # Use monotonic clock to track real elapsed time

        # Only clear force analysis flag for main loop sleeps
        if respect_force_analysis:
            self._force_analysis.clear()

        while self.running:
            elapsed = time.monotonic() - start_time
            if elapsed >= seconds:
                break

            # Check for force analysis (only if this sleep respects it)
            if respect_force_analysis and self._force_analysis.is_set():
                self._force_analysis.clear()
                self.logger.info("Force analysis triggered - interrupting wait")
                return True

            remaining = seconds - elapsed
            sleep_time = min(SLEEP_CHUNK_SIZE, remaining)
            await asyncio.sleep(sleep_time)

        return False

    async def _force_analysis_now(self):
        """Force immediate analysis by interrupting the wait."""
        self.logger.info("Forcing immediate analysis...")
        self._force_analysis.set()

    async def _start_position_status_updates(self):
        """Start periodic position status updates to Discord (every hour)."""
        if self._position_status_task and not self._position_status_task.done():
            return  # Already running

        self._position_status_task = asyncio.create_task(
            self._position_status_loop(), name="Position-Status-Updates"
        )
        self._active_tasks.add(self._position_status_task)
        self._position_status_task.add_done_callback(self._active_tasks.discard)
        self.logger.debug("Started hourly position status updates")

    async def _stop_position_status_updates(self):
        """Stop periodic position status updates."""
        if self._position_status_task and not self._position_status_task.done():
            self._position_status_task.cancel()
            try:
                await self._position_status_task
            except asyncio.CancelledError:
                pass
            self._position_status_task = None
            self.logger.debug("Stopped position status updates")

    async def _position_status_loop(self):
        """Send position status updates every hour while position is open."""
        try:
            while self.running:
                # Wait for interval (in chunks for responsiveness)
                await self._interruptible_sleep(
                    POSITION_UPDATE_INTERVAL, respect_force_analysis=False
                )

                if not self.running:
                    break

                # Stop when every slot is empty. In multi-slot mode we emit one
                # status message per open slot (AI + Fast).
                open_positions = dict(self.trading_strategy.positions)
                if not open_positions:
                    self.logger.debug("All positions closed, stopping status updates")
                    break

                # Send position status update
                if self.discord_notifier:
                    try:
                        ticker = await self._fetch_current_ticker()
                        current_price = (
                            float(ticker.get("last", ticker.get("close", 0)))
                            if ticker
                            else 0.0
                        )

                        for slot_source, pos in open_positions.items():
                            try:
                                await self.discord_notifier.send_position_status(
                                    position=pos,
                                    current_price=current_price,
                                    channel_id=self.config.MAIN_CHANNEL_ID,
                                )
                            except Exception as e:
                                self.logger.warning(
                                    "Error sending [%s] position status: %s",
                                    slot_source.upper(),
                                    e,
                                )
                        self.logger.debug(
                            "Sent hourly position status update to Discord "
                            "(%d slot(s))",
                            len(open_positions),
                        )
                    except Exception as e:
                        self.logger.warning(
                            "Error sending position status update: %s", e
                        )
        except asyncio.CancelledError:
            self.logger.debug("Position status loop cancelled")
            raise

    async def _show_help(self):
        """Show help information about available commands."""
        self.keyboard_handler.display_help()

    async def _request_shutdown(self):
        """Request application shutdown via keyboard."""
        self.logger.info("Shutdown requested via keyboard command")
        if self.shutdown_manager:
            await self.shutdown_manager.shutdown_gracefully()
        else:
            self.running = False
            for task in self.tasks:
                if not task.done():
                    task.cancel()
