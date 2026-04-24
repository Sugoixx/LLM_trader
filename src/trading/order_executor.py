"""Order Executor — bridges AI decisions with real exchange orders.

Architecture:
    OrderExecutorProtocol  (contract)
        ├─ DemoExecutor     (default, no-op, logs only — current behavior)
        └─ LiveExecutor     (places real orders via CCXT)

The executor is injected into TradingStrategy. When LIVE_TRADING_ENABLED=true
the CompositionRoot wires LiveExecutor; otherwise DemoExecutor is used.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Dict, Any, Protocol, runtime_checkable, TYPE_CHECKING

import ccxt.async_support as ccxt

from src.logger.logger import Logger
from src.utils.decorators import retry_async

if TYPE_CHECKING:
    from src.config.protocol import ConfigProtocol


# ---------------------------------------------------------------------------
# Data model for order results
# ---------------------------------------------------------------------------

@dataclass(slots=True, kw_only=True)
class OrderResult:
    """Result of an exchange order execution."""
    success: bool
    order_id: Optional[str] = None
    symbol: str = ""
    side: str = ""           # "buy" or "sell"
    order_type: str = ""     # "limit" or "market"
    quantity: float = 0.0
    price: float = 0.0       # Execution / requested price
    filled: float = 0.0      # Actually filled quantity
    avg_price: float = 0.0   # Average fill price
    fee: float = 0.0         # Fee paid (quote currency)
    fee_currency: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)
    error: str = ""
    already_closed: bool = False  # True when close failed because broker has no open position
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class OrderExecutorProtocol(Protocol):
    """Contract for order execution — demo or live."""

    async def open_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        price: float,
        order_type: str = "limit",
        source: str = "ai",
    ) -> OrderResult:
        """Place an opening order (BUY or SELL)."""
        ...

    async def close_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        price: float,
        order_type: str = "limit",
        source: str = "ai",
    ) -> OrderResult:
        """Place a closing order (opposite side)."""
        ...

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        """Cancel an open order."""
        ...

    async def get_balance(self, currency: str = "USDC") -> float:
        """Return available balance in quote currency."""
        ...

    async def get_open_orders(self, symbol: str) -> list:
        """Return list of open orders for a symbol."""
        ...

    async def get_broker_constraints(self, symbol: str, price: float):
        """Return a BrokerConstraints snapshot (leverage, min lot, etc.)."""
        ...

    async def modify_position(self, symbol: str, sl: float, tp: float, source: str = "ai") -> bool:
        """Modify SL/TP of the open position on the broker side. Returns True on success."""
        ...

    @property
    def is_live(self) -> bool:
        """True if this executor places real orders."""
        ...


# ---------------------------------------------------------------------------
# Demo Executor (current behavior — no real orders)
# ---------------------------------------------------------------------------

class DemoExecutor:
    """Simulated order execution for paper trading (default mode)."""

    def __init__(self, logger: Logger, config: "ConfigProtocol"):
        self.logger = logger
        self.config = config

    async def open_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        price: float,
        order_type: str = "limit",
        source: str = "ai",
    ) -> OrderResult:
        self.logger.info(
            "[DEMO] [%s] %s %s %.6f %s @ $%.2f (%s)",
            source.upper(), side.upper(), symbol, quantity,
            symbol.split("/")[0], price, order_type,
        )
        return OrderResult(
            success=True,
            order_id=f"demo-{datetime.now(timezone.utc).timestamp():.0f}",
            symbol=symbol,
            side=side,
            order_type=order_type,
            quantity=quantity,
            price=price,
            filled=quantity,
            avg_price=price,
            fee=price * quantity * self.config.TRANSACTION_FEE_PERCENT,
            fee_currency=symbol.split("/")[1] if "/" in symbol else "USDC",
        )

    async def close_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        price: float,
        order_type: str = "limit",
        source: str = "ai",
    ) -> OrderResult:
        return await self.open_order(symbol, side, quantity, price, order_type, source=source)

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        self.logger.info("[DEMO] Cancel order %s on %s", order_id, symbol)
        return True

    async def get_balance(self, currency: str = "USDC") -> float:
        return self.config.DEMO_QUOTE_CAPITAL

    async def get_open_orders(self, symbol: str) -> list:
        return []

    async def get_broker_constraints(self, symbol: str, price: float):
        from src.trading.data_models import BrokerConstraints
        return BrokerConstraints(symbol=symbol, leverage=1.0, contract_size=1.0)

    async def modify_position(self, symbol: str, sl: float, tp: float, source: str = "ai") -> bool:
        self.logger.info("[DEMO] [%s] Modify position %s → SL=%.5f  TP=%.5f", source.upper(), symbol, sl, tp)
        return True

    @property
    def is_live(self) -> bool:
        return False


# ---------------------------------------------------------------------------
# Live Executor (real Binance orders via CCXT)
# ---------------------------------------------------------------------------

class LiveExecutor:
    """Real order execution via CCXT (Binance spot by default).

    Safety features:
    - Requires explicit LIVE_TRADING_ENABLED=true in config
    - Double-checks balance before every order
    - Maximum single order cap (LIVE_MAX_ORDER_USD)
    - All orders are LIMIT by default (never market unless configured)
    - Detailed logging of every order placed and filled
    - Retry with exponential backoff on transient errors
    """

    def __init__(
        self,
        logger: Logger,
        config: "ConfigProtocol",
        exchange: ccxt.Exchange,
    ):
        self.logger = logger
        self.config = config
        self.exchange = exchange
        self._order_lock = asyncio.Lock()
        # Track last filled quantity per symbol so modify_position can size the OCO.
        self._last_fill: Dict[str, float] = {}
        # Track OCO list IDs so we can cancel them before a close order.
        self._oco_list_ids: Dict[str, str] = {}

    # ---- Public API -------------------------------------------------------

    async def open_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        price: float,
        order_type: str = "limit",
        source: str = "ai",
    ) -> OrderResult:
        result = await self._place_order(symbol, side, quantity, price, order_type)
        if result.success and result.filled > 0:
            self._last_fill[symbol] = result.filled
        return result

    async def close_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        price: float,
        order_type: str = "limit",
        source: str = "ai",
    ) -> OrderResult:
        # Cancel any protection orders (OCO) before placing the close order.
        # If the OCO is not cancelled first, the two SELL orders would compete.
        await self._cancel_protection_orders(symbol)
        result = await self._place_order(symbol, side, quantity, price, order_type)
        if result.success or getattr(result, "already_closed", False):
            self._last_fill.pop(symbol, None)
        return result

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        try:
            await self.exchange.cancel_order(order_id, symbol)
            self.logger.info("[LIVE] Cancelled order %s on %s", order_id, symbol)
            return True
        except Exception as e:
            self.logger.error("[LIVE] Failed to cancel order %s: %s", order_id, e)
            return False

    async def get_balance(self, currency: str = "USDC") -> float:
        try:
            # Use private_get_account (GET /api/v3/account) directly — avoids ccxt
            # calling sapi endpoints which don't exist on Binance demo-api.
            response = await self.exchange.private_get_account()
            balances = response.get("balances", [])
            for b in balances:
                if b.get("asset") == currency:
                    return float(b.get("free", 0))
            return 0.0
        except Exception:
            # Fallback to standard fetch_balance
            try:
                balance = await self.exchange.fetch_balance()
                free = float(balance.get("free", {}).get(currency, 0))
                return free
            except Exception as e:
                self.logger.error("[LIVE] Failed to fetch balance: %s", e)
                return 0.0

    async def get_open_orders(self, symbol: str) -> list:
        try:
            return await self.exchange.fetch_open_orders(symbol)
        except Exception as e:
            self.logger.error("[LIVE] Failed to fetch open orders: %s", e)
            return []

    async def get_broker_constraints(self, symbol: str, price: float):
        """Return CCXT market limits for `symbol`.

        Reads `exchange.markets[symbol]['limits']` for min_amount and
        min_notional (Binance MIN_NOTIONAL filter). Leverage defaults to 1
        for spot; override via config if running futures.
        """
        from src.trading.data_models import BrokerConstraints
        constraints = BrokerConstraints(symbol=symbol, leverage=1.0, contract_size=1.0)
        try:
            markets = getattr(self.exchange, "markets", None) or {}
            if not markets:
                try:
                    markets = await self.exchange.load_markets()
                except Exception:
                    markets = {}
            market = markets.get(symbol) or {}
            limits = market.get("limits") or {}
            amount_limits = limits.get("amount") or {}
            cost_limits = limits.get("cost") or {}
            constraints.min_volume = float(amount_limits.get("min") or 0.0)
            constraints.max_volume = float(amount_limits.get("max") or 0.0)
            constraints.min_notional = float(cost_limits.get("min") or 0.0)
            # Derive volume_step from CCXT precision. Binance spot exposes
            # precision.amount as either a step size (e.g. 0.00001) or a
            # decimal digit count (e.g. 5). Support both.
            precision = (market.get("precision") or {}).get("amount")
            if precision is None:
                constraints.volume_step = constraints.min_volume
            else:
                precision_f = float(precision)
                if precision_f > 0 and precision_f < 1:
                    constraints.volume_step = precision_f
                elif precision_f >= 1:
                    constraints.volume_step = 10 ** (-precision_f)
                else:
                    constraints.volume_step = constraints.min_volume
            quote = market.get("quote") or (symbol.split("/")[1] if "/" in symbol else "USDC")
            constraints.account_currency = str(quote)
            # Detect margin / futures mode via exchange options
            default_type = None
            try:
                default_type = (self.exchange.options or {}).get("defaultType")
            except Exception:
                default_type = None
            if default_type in ("future", "swap", "margin"):
                constraints.leverage = float(
                    getattr(self.config, "LIVE_MAX_LEVERAGE", 1) or 1
                )
        except Exception as e:
            self.logger.debug("[LIVE] get_broker_constraints fallback: %s", e)
        return constraints

    async def modify_position(self, symbol: str, sl: float, tp: float, source: str = "ai") -> bool:
        """Place an OCO order (SL + TP) to protect the open LONG position on Binance spot.

        On Binance spot demo/mainnet, places a SELL OCO:
          - Limit leg  → take-profit (executes when price reaches TP)
          - Stop-limit leg → stop-loss (triggers + executes when price drops to SL)

        Any previous protection orders for the symbol are cancelled first.
        Returns True on success, False if OCO is unsupported or an error occurs.
        """
        exchange_id = getattr(self.exchange, "id", "")
        if exchange_id != "binance":
            self.logger.warning(
                "[LIVE] modify_position: OCO not implemented for exchange '%s' — "
                "SL/TP will be managed by the bot internally.",
                exchange_id,
            )
            return False

        quantity = self._last_fill.get(symbol, 0.0)
        if quantity <= 0:
            self.logger.warning(
                "[LIVE] modify_position: no tracked fill for %s — cannot size OCO",
                symbol,
            )
            return False

        # Cancel existing protection orders before placing new ones
        await self._cancel_protection_orders(symbol)

        try:
            # Price precision for the symbol (use exchange helper when available)
            def _fmt(p: float) -> float:
                try:
                    return float(self.exchange.price_to_precision(symbol, p))
                except Exception:
                    return round(p, 2)

            tp_price = _fmt(tp)
            sl_trigger = _fmt(sl)
            # Stop-limit execution price: 0.2% below trigger to absorb slippage
            sl_limit = _fmt(sl * 0.998)

            oco = await self.exchange.create_order(
                symbol,
                "oco",
                "sell",
                quantity,
                tp_price,
                params={
                    "stopPrice": sl_trigger,
                    "stopLimitPrice": sl_limit,
                    "stopLimitTimeInForce": "GTC",
                },
            )

            list_id = str((oco.get("info") or {}).get("orderListId", ""))
            if list_id:
                self._oco_list_ids[symbol] = list_id

            self.logger.info(
                "[LIVE] [%s] OCO protection placed — SL=%.2f TP=%.2f qty=%.6f%s",
                source.upper(), sl, tp, quantity,
                f" listId={list_id}" if list_id else "",
            )
            return True

        except (ccxt.NotSupported, ccxt.InvalidOrder, ccxt.ExchangeError) as e:
            self.logger.warning(
                "[LIVE] OCO placement failed — position has no broker-side SL/TP: %s", e
            )
            return False
        except Exception as e:
            self.logger.error("[LIVE] Unexpected error placing OCO: %s", e)
            return False

    @property
    def is_live(self) -> bool:
        return True

    # ---- Internal ---------------------------------------------------------

    async def _cancel_protection_orders(self, symbol: str) -> None:
        """Cancel all open orders for `symbol` (clears OCO protection before a close)."""
        try:
            open_orders = await self.exchange.fetch_open_orders(symbol)
        except Exception as e:
            self.logger.warning("[LIVE] fetch_open_orders(%s) failed: %s", symbol, e)
            self._oco_list_ids.pop(symbol, None)
            return
        if not open_orders:
            self._oco_list_ids.pop(symbol, None)
            return
        for order in open_orders:
            try:
                await self.exchange.cancel_order(order["id"], symbol)
                self.logger.info(
                    "[LIVE] Cancelled open order %s (%s) for %s",
                    order["id"], order.get("type", "?"), symbol,
                )
            except Exception as e:
                self.logger.warning(
                    "[LIVE] Failed to cancel order %s: %s", order["id"], e
                )
        self._oco_list_ids.pop(symbol, None)

    @retry_async(max_retries=2, initial_delay=1.0)
    async def _place_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        price: float,
        order_type: str,
    ) -> OrderResult:
        """Core order placement with safety checks."""
        async with self._order_lock:
            # Safety check 1: max order size
            order_value = quantity * price
            max_order = self.config.LIVE_MAX_ORDER_USD
            if order_value > max_order:
                msg = (
                    f"Order value ${order_value:,.2f} exceeds max "
                    f"${max_order:,.2f}. Rejected."
                )
                self.logger.error("[LIVE] %s", msg)
                return OrderResult(success=False, error=msg, symbol=symbol, side=side)

            # Safety check 2: sufficient balance
            quote_currency = symbol.split("/")[1] if "/" in symbol else "USDC"
            if side.lower() == "buy":
                balance = await self.get_balance(quote_currency)
                required = order_value * (1 + self.config.TRANSACTION_FEE_PERCENT)
                if balance < required:
                    msg = (
                        f"Insufficient {quote_currency} balance: "
                        f"${balance:,.2f} < ${required:,.2f}"
                    )
                    self.logger.error("[LIVE] %s", msg)
                    return OrderResult(success=False, error=msg, symbol=symbol, side=side)

            # Place the order
            self.logger.info(
                "[LIVE] Placing %s %s order: %.6f %s @ $%.2f (value: $%.2f)",
                order_type.upper(), side.upper(), quantity,
                symbol, price, order_value,
            )

            try:
                raw_order = await self.exchange.create_order(
                    symbol=symbol,
                    type=order_type,
                    side=side.lower(),
                    amount=quantity,
                    price=price if order_type == "limit" else None,
                )
            except ccxt.InsufficientFunds as e:
                msg = f"Insufficient funds: {e}"
                self.logger.error("[LIVE] %s", msg)
                # For SELL (close long) / BUY (close short): InsufficientFunds means
                # we don't hold the base asset — the position was never opened on this
                # account (e.g. opened in demo mode before live was configured).
                # Mark as already_closed so the strategy cleans up locally.
                already_closed = side.lower() == "sell" or (
                    side.lower() == "buy" and order_type not in ("open", "limit")
                )
                return OrderResult(
                    success=False,
                    error=msg,
                    symbol=symbol,
                    side=side,
                    already_closed=already_closed,
                )
            except ccxt.InvalidOrder as e:
                msg = f"Invalid order: {e}"
                self.logger.error("[LIVE] %s", msg)
                return OrderResult(success=False, error=msg, symbol=symbol, side=side)

            # Parse response
            order_id = str(raw_order.get("id", ""))
            filled = float(raw_order.get("filled", 0))
            avg_fill = float(raw_order.get("average", price) or price)
            fee_info = raw_order.get("fee", {}) or {}
            fee_cost = float(fee_info.get("cost", 0) or 0)
            fee_curr = str(fee_info.get("currency", quote_currency) or quote_currency)
            status = raw_order.get("status", "unknown")

            self.logger.info(
                "[LIVE] Order %s placed — status: %s, filled: %.6f @ $%.2f, fee: %.4f %s",
                order_id, status, filled, avg_fill, fee_cost, fee_curr,
            )

            return OrderResult(
                success=True,
                order_id=order_id,
                symbol=symbol,
                side=side,
                order_type=order_type,
                quantity=quantity,
                price=price,
                filled=filled,
                avg_price=avg_fill,
                fee=fee_cost,
                fee_currency=fee_curr,
                raw=raw_order,
            )

    async def startup(self) -> None:
        """Pre-load market data then switch to demo-api.binance.com.

        ccxt sends X-MBX-APIKEY even on public endpoints. Mainnet Binance
        rejects demo keys on any request. Fix: temporarily clear credentials,
        load markets (public call), then restore credentials and switch URLs
        to demo-api.binance.com.
        """
        exchange_id = getattr(self.exchange, "id", "")
        if exchange_id != "binance":
            return

        saved_key = self.exchange.apiKey
        saved_secret = self.exchange.secret
        try:
            # Step 1: clear credentials so mainnet public call succeeds
            self.exchange.apiKey = ""
            self.exchange.secret = ""
            self.exchange.options["fetchCurrencies"] = False
            self.exchange.options.setdefault("fetchMarkets", ["spot"])
            await self.exchange.load_markets()
            self.logger.info(
                "[LIVE] Markets pre-loaded (%d symbols)", len(self.exchange.markets)
            )
        except Exception as e:
            self.logger.warning("[LIVE] startup market pre-load failed: %s", e)
        finally:
            # Step 2: restore credentials
            self.exchange.apiKey = saved_key
            self.exchange.secret = saved_secret
            # Step 3: switch private URLs to demo-api.binance.com
            demo_urls = self.exchange.urls.get("demo", {})
            if demo_urls and isinstance(self.exchange.urls.get("api"), dict):
                self.exchange.urls["api"].update(demo_urls)
            for sapi_key in ("sapi", "sapiV2", "sapiV3", "sapiV4"):
                self.exchange.urls["api"].pop(sapi_key, None)
            self.logger.info(
                "[LIVE] Switched exchange URLs to demo-api.binance.com"
            )

    async def close(self) -> None:
        """Close the authenticated exchange connection."""
        try:
            await self.exchange.close()
            self.logger.info("[LIVE] Exchange connection closed")
        except Exception as e:
            self.logger.error("[LIVE] Error closing exchange: %s", e)
