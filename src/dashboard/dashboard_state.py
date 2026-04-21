"""Shared dashboard state for real-time updates.

This module holds state that is updated by the trading bot and read by the dashboard.
It enables WebSocket broadcasts and API endpoints to share live data.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any
import asyncio
import json
import logging
import time
from src.dashboard.routers.ws_router import broadcast


_RUNTIME_SETTINGS_PATH = Path("data/trading/runtime_settings.json")
_logger = logging.getLogger(__name__)


def _load_runtime_settings() -> Dict[str, Any]:
    """Load persisted runtime toggles from disk (best-effort)."""
    try:
        if _RUNTIME_SETTINGS_PATH.exists():
            with _RUNTIME_SETTINGS_PATH.open("r", encoding="utf-8") as fh:
                return json.load(fh) or {}
    except (OSError, json.JSONDecodeError) as e:
        _logger.warning("[dashboard_state] runtime_settings load failed: %s", e)
    return {}


def _save_runtime_settings(data: Dict[str, Any]) -> None:
    """Persist runtime toggles to disk (best-effort, atomic-ish)."""
    try:
        _RUNTIME_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _RUNTIME_SETTINGS_PATH.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        tmp.replace(_RUNTIME_SETTINGS_PATH)
    except OSError as e:
        _logger.warning("[dashboard_state] runtime_settings save failed: %s", e)


_PERSISTED = _load_runtime_settings()


@dataclass
class DashboardState:
    """Shared state between bot and dashboard."""
    # pylint: disable=too-many-instance-attributes
    next_check_utc: Optional[datetime] = None
    bot_status: str = "running"
    last_analysis_time: Optional[datetime] = None
    current_position: Optional[Dict[str, Any]] = None
    current_price: Optional[float] = None
    api_costs: Dict[str, float] = field(default_factory=lambda: {"openrouter": 0.0, "google": 0.0})
    last_request_cost: Optional[float] = None
    auto_trade_enabled: bool = True
    fast_trading_enabled: bool = field(
        default_factory=lambda: bool(_PERSISTED.get("fast_trading_enabled", False))
    )  # Fast Trading Mode: trade on algo signals, AI for correction
    live_capital: Optional[float] = None  # MT5 balance when live, updated by trading strategy
    market_hours_status: Optional[Dict[str, Any]] = None
    algo_signals: Optional[Dict[str, Any]] = None  # Latest algo strategy signals
    fast_guard_state: Optional[Dict[str, Any]] = None  # Fast Trading Mode safety state
    # Capital-sizing alert: populated by TradingStrategy when a signal is
    # refused because the computed position is below broker minimums.
    # Shape:
    #   {
    #     symbol, side, price, timestamp,
    #     sizing_warning (str),
    #     capital_needed_total, capital_top_up, required_lots,
    #     required_notional, required_margin, expected_gain_at_tp,
    #     expected_loss_at_sl, leverage, min_lot_notional,
    #     account_currency, current_capital,
    #   }
    capital_alert: Optional[Dict[str, Any]] = None
    _cache: dict[str, Any] = field(default_factory=dict)
    cache_timestamps: dict[str, float] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    async def update_price(self, price: float) -> None:
        """Update current price (no broadcast to avoid spam)."""
        async with self._lock:
            self.current_price = price

    async def update_next_check(self, next_time: datetime) -> None:
        """Update next check time and broadcast to clients."""
        async with self._lock:
            self.next_check_utc = next_time
        await self._broadcast({"type": "countdown", "next_check_utc": next_time.isoformat()})

    async def update_position(self, position_data: Optional[Dict[str, Any]]) -> None:
        """Update current position and broadcast to clients."""
        async with self._lock:
            self.current_position = position_data
        await self._broadcast({"type": "position", "data": position_data})

    async def update_analysis_complete(self) -> None:
        """Signal that analysis has completed."""
        async with self._lock:
            self.last_analysis_time = datetime.now(timezone.utc)
        await self._broadcast({"type": "analysis_complete"})

    async def update_api_costs(self, provider: str, cost: float) -> None:
        """Update API costs for a provider and broadcast to clients."""
        async with self._lock:
            if provider in self.api_costs:
                self.api_costs[provider] += cost
            self.last_request_cost = cost
        await self._broadcast({"type": "cost_update", "provider": provider, "cost": cost, "total": self.api_costs})

    async def reset_api_costs(self) -> None:
        """Reset all API costs to zero."""
        async with self._lock:
            self.api_costs = {"openrouter": 0.0, "google": 0.0}
            self.last_request_cost = None
        await self._broadcast({"type": "cost_reset", "total": self.api_costs})

    async def _broadcast(self, data: Dict[str, Any]) -> None:
        """Broadcast data to all connected WebSocket clients."""
        await broadcast(data)

    def get_countdown_data(self) -> Dict[str, Any]:
        """Get current countdown state for REST API."""
        if not self.next_check_utc:
            return {"next_check_utc": None, "seconds_remaining": None}
        now = datetime.now(timezone.utc)
        remaining = (self.next_check_utc.replace(tzinfo=timezone.utc) 
                     if self.next_check_utc.tzinfo is None 
                     else self.next_check_utc - now).total_seconds()
        return {
            "next_check_utc": self.next_check_utc.isoformat(),
            "seconds_remaining": max(0, int(remaining))
        }

    def get_cost_data(self) -> Dict[str, Any]:
        """Get current API cost data for REST API."""
        total = sum(self.api_costs.values())
        return {
            "costs_by_provider": self.api_costs.copy(),
            "total_session_cost": total,
            "last_request_cost": self.last_request_cost,
            "formatted_total": f"${total:.6f}" if total > 0 else "Free"
        }

    def get_cached(self, key: str, ttl_seconds: float = 30.0) -> Optional[Any]:
        """Retrieve a cached value if it is within TTL."""
        cached_time = self.cache_timestamps.get(key, 0)
        if time.time() - cached_time > ttl_seconds:
            return None
        return self._cache.get(key)

    def set_cached(self, key: str, value: Any) -> None:
        """Store a value in cache with current timestamp, enforcing max size."""
        if len(self._cache) >= 100 and key not in self._cache:
            if self.cache_timestamps:
                oldest_key = min(self.cache_timestamps, key=self.cache_timestamps.get)
                self.invalidate_cache(oldest_key)
        self._cache[key] = value
        self.cache_timestamps[key] = time.time()

    def invalidate_cache(self, key: str) -> None:
        """Remove cached value."""
        self._cache.pop(key, None)
        self.cache_timestamps.pop(key, None)

    async def update_execution_snapshot(self, snapshot: Dict[str, Any]) -> None:
        """Update Layer 2 execution engine snapshot (called by ExecutionEngine)."""
        async with self._lock:
            self._execution_snapshot = snapshot

    async def update_monitor_snapshot(self, snapshot: Dict[str, Any]) -> None:
        """Update position monitor snapshot (trailing SL, partials, etc)."""
        async with self._lock:
            self._monitor_snapshot = snapshot
        await self._broadcast({"type": "monitor_update", "data": snapshot})

    async def update_market_hours_status(self, status: Dict[str, Any]) -> None:
        """Update market-hours status for dashboard badges and API status endpoint."""
        async with self._lock:
            self.market_hours_status = status
        await self._broadcast({"type": "market_hours", "data": status})

    async def update_algo_signals(self, signals: Dict[str, Any]) -> None:
        """Update latest algo strategy signals and broadcast to clients."""
        async with self._lock:
            self.algo_signals = signals
        self.invalidate_cache("algo_signals")
        await self._broadcast({"type": "algo_signals", "data": signals})

    async def toggle_auto_trade(self, enabled: Optional[bool] = None) -> bool:
        """Toggle or set auto_trade_enabled. Returns new state."""
        async with self._lock:
            if enabled is not None:
                self.auto_trade_enabled = enabled
            else:
                self.auto_trade_enabled = not self.auto_trade_enabled
            state = self.auto_trade_enabled
        await self._broadcast({"type": "auto_trade", "enabled": state})
        return state

    async def toggle_fast_trading(self, enabled: Optional[bool] = None) -> bool:
        """Toggle or set fast_trading_enabled. Returns new state.

        The new value is persisted to ``data/trading/runtime_settings.json``
        so Fast Trading Mode survives a bot restart.
        """
        async with self._lock:
            if enabled is not None:
                self.fast_trading_enabled = enabled
            else:
                self.fast_trading_enabled = not self.fast_trading_enabled
            state = self.fast_trading_enabled
        # Persist (best-effort, outside the lock)
        try:
            persisted = _load_runtime_settings()
            persisted["fast_trading_enabled"] = state
            _save_runtime_settings(persisted)
        except Exception as e:  # pragma: no cover
            _logger.warning("[dashboard_state] persist fast_trading failed: %s", e)
        await self._broadcast({"type": "fast_trading", "enabled": state})
        return state

    async def update_fast_guard(self, snapshot: Dict[str, Any]) -> None:
        """Update Fast Trading Mode safety-guard state and broadcast."""
        async with self._lock:
            self.fast_guard_state = snapshot
        self.invalidate_cache("fast_guard")
        await self._broadcast({"type": "fast_guard", "data": snapshot})

    async def update_capital_alert(self, alert: Optional[Dict[str, Any]]) -> None:
        """Publish or clear the capital-top-up alert banner.

        Pass `None` to clear. Broadcast `type: "capital_alert"` so the
        dashboard can show a dismissible banner with the hypothesis
        (top-up needed, expected TP gain / SL loss).
        """
        async with self._lock:
            self.capital_alert = alert
        await self._broadcast({"type": "capital_alert", "data": alert})


dashboard_state = DashboardState()
