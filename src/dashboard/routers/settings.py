"""Dashboard router for runtime trading settings management."""

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from typing import Optional

from ..symbol_catalog import SYMBOL_CATALOG, is_known_symbol, find_symbol


# --- Preset definitions ---
PRESETS = {
    "aggressive": {
        "label": "Aggressive",
        "description": "Higher risk, bigger positions, tighter trailing",
        "settings": {
            "demo_trading.demo_quote_capital": None,  # unchanged
            "execution_engine.trailing_atr_multiplier": 1.5,
            "execution_engine.trailing_enabled": True,
            "execution_engine.trailing_breakeven_on_tp1": False,
            "execution_engine.partial_enabled": False,
            "debate.enabled": False,
            "live_trading.max_order_usd": 500,
        },
    },
    "moderate": {
        "label": "Moderate",
        "description": "Balanced risk/reward with debate validation",
        "settings": {
            "execution_engine.trailing_atr_multiplier": 2.0,
            "execution_engine.trailing_enabled": True,
            "execution_engine.trailing_breakeven_on_tp1": True,
            "execution_engine.partial_enabled": False,
            "debate.enabled": True,
            "debate.use_quick_model": True,
            "live_trading.max_order_usd": 500,
        },
    },
    "conservative": {
        "label": "Conservative",
        "description": "Lower risk, partial TP, full debate, tighter caps",
        "settings": {
            "execution_engine.trailing_atr_multiplier": 2.5,
            "execution_engine.trailing_enabled": True,
            "execution_engine.trailing_breakeven_on_tp1": True,
            "execution_engine.partial_enabled": True,
            "execution_engine.partial_targets": "0.5:0.5, 1.0:1.0",
            "debate.enabled": True,
            "debate.use_quick_model": False,
            "live_trading.max_order_usd": 250,
        },
    },
}

# Whitelist of settings that can be changed at runtime (section.key)
ALLOWED_SETTINGS = {
    "general.timeframe",
    "general.analysis_cooldown_minutes",
    "general.candle_limit",
    "general.ai_chart_candle_limit",
    "execution_engine.trailing_enabled",
    "execution_engine.trailing_atr_multiplier",
    "execution_engine.trailing_breakeven_on_tp1",
    "execution_engine.partial_enabled",
    "execution_engine.partial_targets",
    "debate.enabled",
    "debate.use_quick_model",
    "debate.skip_for_hold",
    "live_trading.max_order_usd",
    "live_trading.max_order_usd_ai",
    "live_trading.max_order_usd_fast",
    "live_trading.double_trade_enabled",
    "live_trading.confirm_orders",
    "fast_trading.min_interval_seconds",
    "fast_trading.min_confidence",
    "fast_trading.max_signal_age_seconds",
    "fast_trading.min_rr_after_fees",
    "fast_trading.poll_interval_seconds",
    "fast_trading.block_minutes_before_close",
    "model_config.temperature",
    "model_config.max_tokens",
}

VALID_TIMEFRAMES = [
    "1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d", "1w",
]


class SettingUpdate(BaseModel):
    """Single setting key-value pair."""
    key: str = Field(..., description="Setting key in section.name format")
    value: object = Field(..., description="New value")


class SettingsUpdateRequest(BaseModel):
    """Batch settings update."""
    settings: list[SettingUpdate]


class PresetRequest(BaseModel):
    """Apply a named preset."""
    preset: str


class FastTradingToggle(BaseModel):
    """Optional toggle body for /fast-trading.

    - Send ``{"enabled": true}`` or ``{"enabled": false}`` to set explicitly.
    - Send no body (or ``{}``) to flip the current state.
    """
    enabled: Optional[bool] = None


class SymbolSwitchRequest(BaseModel):
    """Request body for /api/settings/symbol (change active trading pair)."""
    symbol: str = Field(..., description="Canonical symbol (e.g. 'EURUSD' or 'BTC/USDT')")
    close_position: bool = Field(
        False,
        description="If True and a position is open, close it at market before switching.",
    )


class SettingsRouter:
    """Exposes runtime-configurable trading parameters via REST endpoints."""

    def __init__(self, config, logger, dashboard_state, bot=None):
        self.router = APIRouter(prefix="/api/settings", tags=["settings"])
        self.config = config
        self.logger = logger
        self.dashboard_state = dashboard_state
        # Reference to the running bot (injected post-init via set_bot).
        # Needed for in-process symbol switching.
        self.bot = bot
        self._register_routes()

    def _get_current_settings(self) -> dict:
        """Gather all tunable settings into a flat dict."""
        return {
            # Timeframe & analysis
            "general.timeframe": self.config.TIMEFRAME,
            "general.analysis_cooldown_minutes": self.config.ANALYSIS_COOLDOWN_MINUTES,
            "general.candle_limit": self.config.CANDLE_LIMIT,
            "general.ai_chart_candle_limit": self.config.AI_CHART_CANDLE_LIMIT,
            # Execution engine
            "execution_engine.trailing_enabled": self.config.EXECUTION_TRAILING_ENABLED,
            "execution_engine.trailing_atr_multiplier": self.config.EXECUTION_TRAILING_ATR_MULT,
            "execution_engine.trailing_breakeven_on_tp1": self.config.EXECUTION_TRAILING_BREAKEVEN,
            "execution_engine.partial_enabled": self.config.EXECUTION_PARTIAL_ENABLED,
            "execution_engine.partial_targets": self.config.get_config(
                'execution_engine', 'partial_targets', '0.5:0.5, 1.0:1.0'
            ),
            # Debate
            "debate.enabled": self.config.DEBATE_ENABLED,
            "debate.use_quick_model": self.config.DEBATE_USE_QUICK_MODEL,
            "debate.skip_for_hold": self.config.DEBATE_SKIP_FOR_HOLD,
            # Live trading safety
            "live_trading.max_order_usd": self.config.LIVE_MAX_ORDER_USD,
            "live_trading.max_order_usd_ai": self.config.LIVE_MAX_ORDER_USD_AI,
            "live_trading.max_order_usd_fast": self.config.LIVE_MAX_ORDER_USD_FAST,
            "live_trading.double_trade_enabled": bool(
                getattr(self.config, "DOUBLE_TRADE_ENABLED", False)
            ),
            "live_trading.confirm_orders": self.config.LIVE_CONFIRM_ORDERS,
            # Fast trading
            "fast_trading.min_interval_seconds": self.config.FAST_MIN_INTERVAL_SECONDS,
            "fast_trading.min_confidence": self.config.FAST_MIN_CONFIDENCE,
            "fast_trading.max_signal_age_seconds": self.config.FAST_MAX_SIGNAL_AGE_SECONDS,
            "fast_trading.min_rr_after_fees": self.config.FAST_MIN_RR_AFTER_FEES,
            "fast_trading.poll_interval_seconds": self.config.FAST_POLL_INTERVAL_SECONDS,
            "fast_trading.block_minutes_before_close": self.config.FAST_BLOCK_MINUTES_BEFORE_CLOSE,
            # Model
            "model_config.temperature": self.config.get_config('model_config', 'temperature', 0.7),
            "model_config.max_tokens": self.config.get_config('model_config', 'max_tokens', 32768),
        }

    def _apply_setting(self, key: str, value) -> Optional[str]:
        """Apply a single setting. Returns error string or None on success."""
        if key not in ALLOWED_SETTINGS:
            return f"Setting '{key}' is not modifiable at runtime"

        section, name = key.split(".", 1)

        # Validate timeframe
        if key == "general.timeframe":
            if str(value) not in VALID_TIMEFRAMES:
                return f"Invalid timeframe '{value}'. Must be one of: {', '.join(VALID_TIMEFRAMES)}"
            value = str(value)

        # Validate numeric ranges
        if key == "execution_engine.trailing_atr_multiplier":
            try:
                value = float(value)
                if not 0.5 <= value <= 10.0:
                    return "ATR multiplier must be between 0.5 and 10.0"
            except (ValueError, TypeError):
                return "ATR multiplier must be a number"

        if key == "live_trading.max_order_usd":
            try:
                value = float(value)
                if not 10 <= value <= 10000:
                    return "Max order must be between $10 and $10,000"
            except (ValueError, TypeError):
                return "Max order must be a number"

        if key in ("live_trading.max_order_usd_ai", "live_trading.max_order_usd_fast"):
            try:
                value = float(value)
                if not 10 <= value <= 10000:
                    return "Max order (per source) must be between $10 and $10,000"
            except (ValueError, TypeError):
                return "Max order (per source) must be a number"

        if key == "general.analysis_cooldown_minutes":
            try:
                value = int(value)
                if not 0 <= value <= 120:
                    return "Cooldown must be between 0 and 120 minutes"
            except (ValueError, TypeError):
                return "Cooldown must be an integer"

        if key in (
            "fast_trading.min_interval_seconds",
            "fast_trading.max_signal_age_seconds",
            "fast_trading.poll_interval_seconds",
        ):
            try:
                value = int(value)
                if not 0 <= value <= 86400:
                    return "Fast trading seconds value must be between 0 and 86400"
            except (ValueError, TypeError):
                return "Fast trading seconds value must be an integer"

        if key == "fast_trading.block_minutes_before_close":
            try:
                value = int(value)
                if not 0 <= value <= 1440:
                    return "Fast close-block window must be between 0 and 1440 minutes"
            except (ValueError, TypeError):
                return "Fast close-block window must be an integer"

        if key == "fast_trading.min_rr_after_fees":
            try:
                value = float(value)
                if not 0.0 <= value <= 10.0:
                    return "Fast min RR after fees must be between 0.0 and 10.0"
            except (ValueError, TypeError):
                return "Fast min RR after fees must be a number"

        if key == "fast_trading.min_confidence":
            value = str(value).upper()
            if value not in ("LOW", "MEDIUM", "HIGH"):
                return "Fast min confidence must be LOW, MEDIUM, or HIGH"

        if key == "model_config.temperature":
            try:
                value = float(value)
                if not 0.0 <= value <= 2.0:
                    return "Temperature must be between 0.0 and 2.0"
            except (ValueError, TypeError):
                return "Temperature must be a number"

        if key == "model_config.max_tokens":
            try:
                value = int(value)
                if not 256 <= value <= 65536:
                    return "Max tokens must be between 256 and 65536"
            except (ValueError, TypeError):
                return "Max tokens must be an integer"

        # Type coercion for booleans
        if isinstance(value, str) and value.lower() in ("true", "false"):
            value = value.lower() == "true"

        # Apply to config._config_data
        if section not in self.config._config_data:
            self.config._config_data[section] = {}
        self.config._config_data[section][name] = value

        self.logger.info("Setting changed: %s = %s", key, value)
        return None

    def _register_routes(self):
        @self.router.get("")
        async def get_settings():
            """Return all tunable settings with current values."""
            return JSONResponse(content={
                "settings": self._get_current_settings(),
                "presets": {k: {"label": v["label"], "description": v["description"]}
                           for k, v in PRESETS.items()},
                "valid_timeframes": VALID_TIMEFRAMES,
            })

        @self.router.post("")
        async def update_settings(request: SettingsUpdateRequest):
            """Update one or more settings at runtime."""
            errors = []
            applied = []
            for item in request.settings:
                err = self._apply_setting(item.key, item.value)
                if err:
                    errors.append({"key": item.key, "error": err})
                else:
                    applied.append(item.key)

            # Invalidate dashboard cache so new values take effect
            self.dashboard_state.set_cached("statistics", None)

            return JSONResponse(content={
                "applied": applied,
                "errors": errors,
                "settings": self._get_current_settings(),
            })

        @self.router.post("/preset")
        async def apply_preset(request: PresetRequest):
            """Apply a named trading style preset."""
            preset = PRESETS.get(request.preset)
            if not preset:
                return JSONResponse(
                    status_code=400,
                    content={"error": f"Unknown preset '{request.preset}'. Available: {list(PRESETS.keys())}"}
                )

            errors = []
            applied = []
            for key, value in preset["settings"].items():
                if value is None:
                    continue
                err = self._apply_setting(key, value)
                if err:
                    errors.append({"key": key, "error": err})
                else:
                    applied.append(key)

            self.dashboard_state.set_cached("statistics", None)

            return JSONResponse(content={
                "preset": request.preset,
                "label": preset["label"],
                "applied": applied,
                "errors": errors,
                "settings": self._get_current_settings(),
            })

        @self.router.get("/fast-trading")
        async def get_fast_trading():
            """Return current Fast Trading Mode state."""
            return JSONResponse(content={"enabled": self.dashboard_state.fast_trading_enabled})

        @self.router.post("/fast-trading")
        async def toggle_fast_trading(body: FastTradingToggle = Body(default_factory=FastTradingToggle)):
            """Toggle or set Fast Trading Mode.

            Optional JSON body: ``{"enabled": true/false}``
            Omit body (or send ``{}``) to flip the current state.
            """
            new_state = await self.dashboard_state.toggle_fast_trading(body.enabled)
            self.logger.info(
                "Fast Trading Mode set to: %s (requested=%s)", new_state, body.enabled,
            )
            return JSONResponse(content={"enabled": new_state})

        # ------------------------------------------------------------------
        # Fast-mode safety guard admin
        # ------------------------------------------------------------------
        @self.router.get("/fast-guard/state")
        async def get_fast_guard_state():
            """Return current guard snapshot (cooldown, streak, last consensus)."""
            guard = getattr(self.bot, "_fast_guard", None) if self.bot else None
            if guard is None:
                return JSONResponse(
                    status_code=503,
                    content={"error": "Fast guard not available (bot not ready)"},
                )
            # Refresh state from history before returning
            try:
                guard.check(has_open_position=False)
            except Exception as e:  # pragma: no cover
                self.logger.warning("[fast-guard] refresh failed: %s", e)
            return JSONResponse(content=guard.snapshot())

        @self.router.post("/fast-guard/reset")
        async def reset_fast_guard_cooldown():
            """Manually clear consecutive-loss cooldown (admin override)."""
            guard = getattr(self.bot, "_fast_guard", None) if self.bot else None
            if guard is None:
                return JSONResponse(
                    status_code=503,
                    content={"error": "Fast guard not available (bot not ready)"},
                )
            snapshot = guard.reset_cooldown()
            # Broadcast so other dashboard clients see the update immediately
            try:
                await self.dashboard_state.update_fast_guard(snapshot)
            except Exception as e:  # pragma: no cover
                self.logger.warning("[fast-guard] broadcast failed: %s", e)
            self.logger.info("[fast-guard] Cooldown reset via dashboard")
            return JSONResponse(content={"reset": True, "snapshot": snapshot})

        @self.router.post("/fast-guard/clear-history")
        async def clear_fast_guard_history():
            """Clear the recent-decisions inspector history (UI convenience only)."""
            guard = getattr(self.bot, "_fast_guard", None) if self.bot else None
            if guard is None:
                return JSONResponse(
                    status_code=503,
                    content={"error": "Fast guard not available (bot not ready)"},
                )
            guard.clear_recent_decisions()
            snapshot = guard.snapshot()
            try:
                await self.dashboard_state.update_fast_guard(snapshot)
            except Exception as e:  # pragma: no cover
                self.logger.warning("[fast-guard] broadcast failed: %s", e)
            self.logger.info("[fast-guard] Decision history cleared via dashboard")
            return JSONResponse(content={"cleared": True, "snapshot": snapshot})

        # ------------------------------------------------------------------
        # Symbol (trading pair) management
        # ------------------------------------------------------------------
        @self.router.get("/symbols")
        async def get_symbols_catalog():
            """Return the static catalog of tradable symbols + current active."""
            current = self.config.CRYPTO_PAIR
            return JSONResponse(content={
                "catalog": SYMBOL_CATALOG,
                "current_symbol": current,
                "current_entry": find_symbol(current) or None,
            })

        @self.router.get("/symbol/status")
        async def symbol_status():
            """Return current active symbol and any open position."""
            current = self.config.CRYPTO_PAIR
            pos = None
            if self.bot is not None and getattr(self.bot, "trading_strategy", None) is not None:
                p = self.bot.trading_strategy.current_position
                if p is not None:
                    pos = {
                        "symbol": getattr(p, "symbol", None),
                        "direction": getattr(p, "direction", None),
                        "entry_price": getattr(p, "entry_price", None),
                        "size": getattr(p, "size", None),
                    }
            return JSONResponse(content={
                "current_symbol": current,
                "current_entry": find_symbol(current) or None,
                "position": pos,
                "switch_pending": bool(getattr(self.bot, "_switch_requested", None)) if self.bot else False,
            })

        @self.router.post("/symbol")
        async def switch_symbol(request: SymbolSwitchRequest):
            """Request a switch of the active trading symbol.

            - Validates against the symbol catalog.
            - If a position is open and ``close_position`` is False, returns 409.
            - If ``close_position`` is True, closes the position at market first.
            - Persists the new symbol to ``config.ini`` and signals the bot's
              main loop to exit. The composition root then re-launches trading
              on the new symbol (analyzer + Layer 2 engine are re-initialised).
            """
            if self.bot is None:
                return JSONResponse(
                    status_code=503,
                    content={"error": "Bot not yet ready (dashboard started before bot init)"},
                )

            new_symbol = (request.symbol or "").strip()
            if not new_symbol:
                return JSONResponse(status_code=400, content={"error": "Empty symbol"})
            if not is_known_symbol(new_symbol):
                return JSONResponse(
                    status_code=400,
                    content={"error": f"Unknown symbol '{new_symbol}'. Not in catalog."},
                )

            result = await self.bot.request_symbol_switch(
                new_symbol, close_position=request.close_position
            )
            if not result.get("ok"):
                reason = result.get("reason", "")
                status = 409 if reason == "position_open" else (
                    400 if reason == "same_symbol" else 500
                )
                return JSONResponse(status_code=status, content=result)

            self.logger.warning(
                "[Settings] Symbol switch accepted: -> %s (close_position=%s)",
                new_symbol, request.close_position,
            )
            return JSONResponse(content=result)
