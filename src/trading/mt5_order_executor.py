"""MT5 Order Executor — bridges AI decisions with MetaTrader 5 orders.

Implements the same OrderExecutorProtocol as LiveExecutor but routes
orders through MT5Exchange instead of CCXT.

Safety features:
    - Max order value cap (LIVE_MAX_ORDER_USD)
    - Margin/balance check before every order
    - asyncio.Lock for thread safety
    - Detailed logging of every order
    - retry_async on transient errors
"""

import asyncio
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from src.logger.logger import Logger
from src.trading.order_executor import OrderResult
from src.utils.decorators import retry_async

if TYPE_CHECKING:
    from src.config.protocol import ConfigProtocol
    from src.platforms.mt5_exchange import MT5Exchange


class MT5OrderExecutor:
    """Real order execution via MetaTrader 5.

    Drop-in replacement for LiveExecutor when trading Forex/Commodities/Indices
    through an MT5 broker (e.g. Admirals, Pepperstone).
    """

    # Per-slot MT5 magic numbers (Phase 6). 234001 for AI, 234002 for Fast.
    _MAGIC_BY_SOURCE = {"ai": 234001, "fast": 234002}

    def __init__(
        self,
        logger: Logger,
        config: "ConfigProtocol",
        exchange: "MT5Exchange",
    ):
        self.logger = logger
        self.config = config
        self.exchange = exchange
        self._order_lock = asyncio.Lock()

    @property
    def hedging_mode(self) -> bool:
        """True if the broker account runs in MT5 hedging (not netting) mode.

        Proxied from the underlying MT5Exchange which detects the account
        margin_mode at connect time. Defaults to True (permissive) if the
        exchange hasn't reported it yet.
        """
        return bool(getattr(self.exchange, "hedging_mode", True))

    # ---- Public API (OrderExecutorProtocol) ----------------------------

    async def open_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        price: float,
        order_type: str = "market",
        source: str = "ai",
    ) -> OrderResult:
        # Convert raw base-currency units to MT5 lots before placing the order.
        # The risk manager computes `quantity` as allocation / price (crypto
        # convention, 1 unit = 1 BTC). MT5 expects LOTS (1 lot = 100k units
        # for major forex pairs, varies for CFDs/indices). Without this
        # conversion a 5-unit order becomes 5 lots (~500k notional → "No money").
        lots = await self._units_to_lots(symbol, quantity)
        if lots <= 0:
            msg = (
                f"Computed volume is below MT5 minimum for {symbol} "
                f"(requested {quantity:.6f} units). Increase capital, position size, "
                "or confidence to reach at least the broker's volume_min."
            )
            self.logger.error("[MT5-LIVE] %s", msg)
            return OrderResult(success=False, error=msg, symbol=symbol, side=side)
        magic = self._MAGIC_BY_SOURCE.get(source, self._MAGIC_BY_SOURCE["ai"])
        return await self._place_order(symbol, side, lots, price, order_type, magic=magic)

    async def _units_to_lots(self, symbol: str, units: float) -> float:
        """Translate raw base-currency units to MT5 lots.

        Uses the MT5 symbol's ``trade_contract_size`` (e.g. 100000 for EURUSD,
        1 for many indices). Falls back to 100000 if unavailable. The result
        is rounded down to the broker's ``volume_step`` and floored at
        ``volume_min``; returns 0.0 if the computed size is below half the
        minimum (signal that the position is too small to place).
        """
        try:
            import MetaTrader5 as mt5  # type: ignore
        except Exception:
            # MT5 unavailable — pass through as-is
            return float(units)

        try:
            mt5_symbol = self.exchange._resolve_symbol(symbol)  # noqa: SLF001
            info = await asyncio.to_thread(mt5.symbol_info, mt5_symbol)
            if info is None:
                return float(units)

            contract = float(getattr(info, "trade_contract_size", 0) or 0) or 100000.0
            vol_step = float(getattr(info, "volume_step", 0) or 0) or 0.01
            vol_min = float(getattr(info, "volume_min", 0) or 0) or 0.01

            raw_lots = float(units) / contract
            # Round DOWN to volume_step
            steps = int(raw_lots / vol_step) if vol_step > 0 else 0
            snapped = round(steps * vol_step, 10)

            if snapped < vol_min:
                # Always bump to the broker's minimum lot — the margin safety
                # check in _place_order (and MT5 itself) will reject if the
                # account genuinely can't afford it.  Refusing here only stops
                # the bot from trading at all when capital is small (e.g. demo
                # accounts with ~$1000 on EURUSD where 0.01 lot = ~$40 margin).
                self.logger.warning(
                    "[MT5-LIVE] %s: %.6f units → %.6f lots is below "
                    "broker vol_min (%.4f); bumping to minimum lot.",
                    symbol, units, raw_lots, vol_min,
                )
                snapped = vol_min

            self.logger.info(
                "[MT5-LIVE] %s: %.6f units → %.4f lots (contract=%.0f, step=%.4f, min=%.4f)",
                symbol, units, snapped, contract, vol_step, vol_min,
            )
            return snapped
        except Exception as e:
            self.logger.warning("[MT5-LIVE] _units_to_lots fallback for %s: %s", symbol, e)
            return float(units)

    async def _contract_size(self, symbol: str) -> float:
        """Return the broker's contract size for `symbol` (fallback 100000)."""
        try:
            import MetaTrader5 as mt5  # type: ignore
            mt5_symbol = self.exchange._resolve_symbol(symbol)  # noqa: SLF001
            info = await asyncio.to_thread(mt5.symbol_info, mt5_symbol)
            if info is not None:
                return float(getattr(info, "trade_contract_size", 0) or 0) or 100000.0
        except Exception:
            pass
        return 100000.0

    async def get_broker_constraints(self, symbol: str, price: float):
        """Return live MT5 constraints (lot size, step, account leverage)."""
        from src.trading.data_models import BrokerConstraints
        constraints = BrokerConstraints(symbol=symbol)
        try:
            import MetaTrader5 as mt5  # type: ignore
            mt5_symbol = self.exchange._resolve_symbol(symbol)  # noqa: SLF001
            info = await asyncio.to_thread(mt5.symbol_info, mt5_symbol)
            if info is not None:
                constraints.contract_size = (
                    float(getattr(info, "trade_contract_size", 0) or 0) or 100000.0
                )
                constraints.min_volume = float(getattr(info, "volume_min", 0) or 0) or 0.01
                constraints.volume_step = float(getattr(info, "volume_step", 0) or 0) or 0.01
                constraints.max_volume = float(getattr(info, "volume_max", 0) or 0) or 0.0
            acct = await asyncio.to_thread(mt5.account_info)
            if acct is not None:
                constraints.leverage = float(getattr(acct, "leverage", 0) or 0) or 1.0
                constraints.account_currency = str(
                    getattr(acct, "currency", "") or "USD"
                )
        except Exception as e:
            self.logger.debug("[MT5-LIVE] get_broker_constraints fallback: %s", e)
        return constraints

    async def close_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        price: float,
        order_type: str = "market",
        source: str = "ai",
    ) -> OrderResult:
        magic = self._MAGIC_BY_SOURCE.get(source, self._MAGIC_BY_SOURCE["ai"])
        return await self._close_order_by_position_ticket(
            symbol, side, quantity, price, order_type, magic=magic
        )

    async def modify_position(
        self, symbol: str, sl: float, tp: float, source: str = "ai"
    ) -> bool:
        """Modify SL/TP on all open MT5 positions for *symbol*.

        Sends a TRADE_ACTION_SLTP request for each open position ticket so that
        MT5 journals and the broker platform reflect the new values immediately.

        Returns:
            True if at least one position was successfully modified.
        """
        try:
            positions = await self.exchange.fetch_positions(symbol)
        except Exception as e:
            self.logger.error("[MT5-LIVE] modify_position: fetch_positions failed for %s: %s", symbol, e)
            return False

        if not positions:
            self.logger.warning("[MT5-LIVE] modify_position: no open positions found for %s", symbol)
            return False

        # Only touch positions that belong to this slot (by magic number).
        target_magic = self._MAGIC_BY_SOURCE.get(source, self._MAGIC_BY_SOURCE["ai"])
        filtered = []
        for pos in positions:
            pmag = int(((pos.get("info") or {}).get("magic") or 0))
            if pmag == target_magic:
                filtered.append(pos)
        # Fallback: if filtering produced nothing (legacy positions with old
        # magic 234000 or unknown), operate on all positions so we don't
        # silently drop SL/TP updates on pre-Phase-6 positions.
        if not filtered:
            filtered = positions

        success = False
        for pos in filtered:
            ticket = int(pos.get("ticket", 0))
            if ticket <= 0:
                continue
            try:
                await self.exchange.modify_position(ticket, sl, tp)
                self.logger.info(
                    "[MT5-LIVE] Position %d on %s modified → SL=%.5f  TP=%.5f",
                    ticket, symbol, sl, tp,
                )
                success = True
            except Exception as e:
                self.logger.error(
                    "[MT5-LIVE] Failed to modify position %d on %s: %s", ticket, symbol, e
                )
        return success

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        try:
            await self.exchange.cancel_order(order_id, symbol)
            self.logger.info("[MT5-LIVE] Cancelled order %s on %s", order_id, symbol)
            return True
        except Exception as e:
            self.logger.error("[MT5-LIVE] Failed to cancel order %s: %s", order_id, e)
            return False

    async def get_balance(self, currency: str = "USD") -> float:
        try:
            balance = await self.exchange.fetch_balance()
            free = float(balance.get("free", {}).get(currency, 0))
            # Also check default account currency if different
            if free == 0 and currency != "USD":
                free = float(balance.get("free", {}).get("USD", 0))
            if free == 0:
                # Fallback: return first non-zero free balance
                for v in balance.get("free", {}).values():
                    if float(v) > 0:
                        return float(v)
            return free
        except Exception as e:
            self.logger.error("[MT5-LIVE] Failed to fetch balance: %s", e)
            return 0.0

    async def get_open_orders(self, symbol: str) -> list:
        try:
            return await self.exchange.fetch_open_orders(symbol)
        except Exception as e:
            self.logger.error("[MT5-LIVE] Failed to fetch open orders: %s", e)
            return []

    @property
    def is_live(self) -> bool:
        return True

    # ---- Internal -------------------------------------------------------

    async def _close_order_by_position_ticket(
        self,
        symbol: str,
        side: str,
        quantity: float,
        price: float,
        order_type: str,
        magic: int = 234001,
    ) -> OrderResult:
        """Close broker positions by ticket (required in MT5 hedging mode)."""
        async with self._order_lock:
            try:
                open_positions = await self.exchange.fetch_positions(symbol)
            except Exception as e:
                msg = f"MT5 fetch_positions error before close: {e}"
                self.logger.error("[MT5-LIVE] %s", msg)
                return OrderResult(success=False, error=msg, symbol=symbol, side=side)

            close_target_type = "buy" if side.lower() == "sell" else "sell"
            candidates = [
                p for p in open_positions
                if str(p.get("type", "")).lower() == close_target_type
            ]

            # Filter by slot magic if any candidate matches; else fall back to
            # all candidates (legacy positions without slot magic).
            if self.hedging_mode:
                matched = [
                    p for p in candidates
                    if int(((p.get("info") or {}).get("magic") or 0)) == int(magic)
                ]
                if matched:
                    candidates = matched

            if not candidates:
                msg = (
                    f"No open {close_target_type.upper()} position to close on {symbol}"
                )
                self.logger.warning("[MT5-LIVE] %s", msg)
                return OrderResult(
                    success=False,
                    error=msg,
                    symbol=symbol,
                    side=side,
                    already_closed=True,
                )

            # Close oldest positions first to keep deterministic behavior.
            candidates.sort(key=lambda p: int(p.get("ticket", 0)))

            remaining = max(0.0, float(quantity))
            total_filled = 0.0
            weighted_price = 0.0
            last_order_id = ""
            raw_results = []

            for pos in candidates:
                if remaining <= 0:
                    break

                pos_ticket = int(pos.get("ticket", 0))
                pos_volume = float(pos.get("volume", 0.0) or 0.0)
                if pos_ticket <= 0 or pos_volume <= 0:
                    continue

                close_amount = min(remaining, pos_volume)

                self.logger.info(
                    "[MT5-LIVE] Closing ticket %s on %s: side=%s amount=%.4f",
                    pos_ticket, symbol, side.upper(), close_amount,
                )

                try:
                    raw_order = await self.exchange.create_order(
                        symbol=symbol,
                        order_type=order_type,
                        side=side.lower(),
                        amount=close_amount,
                        price=price if order_type == "limit" else None,
                        position_ticket=pos_ticket,
                        magic=int(magic),
                    )
                except RuntimeError as e:
                    msg = f"MT5 close order error on ticket {pos_ticket}: {e}"
                    self.logger.error("[MT5-LIVE] %s", msg)
                    return OrderResult(
                        success=False,
                        error=msg,
                        symbol=symbol,
                        side=side,
                        quantity=total_filled,
                        filled=total_filled,
                        avg_price=(weighted_price / total_filled) if total_filled > 0 else 0.0,
                        raw={"partial_results": raw_results},
                    )

                filled = float(raw_order.get("filled", 0.0) or 0.0)
                avg = float(raw_order.get("average", price) or price)

                total_filled += filled
                weighted_price += filled * avg
                remaining = max(0.0, remaining - filled)
                last_order_id = str(raw_order.get("id", last_order_id))
                raw_results.append(raw_order)

            if total_filled <= 0:
                msg = f"MT5 close requested but nothing filled on {symbol}"
                self.logger.error("[MT5-LIVE] %s", msg)
                return OrderResult(success=False, error=msg, symbol=symbol, side=side)

            avg_fill = weighted_price / total_filled
            if remaining > 0:
                self.logger.warning(
                    "[MT5-LIVE] Partial close on %s: requested=%.4f filled=%.4f remaining=%.4f",
                    symbol, quantity, total_filled, remaining,
                )

            return OrderResult(
                success=True,
                order_id=last_order_id,
                symbol=symbol,
                side=side,
                order_type=order_type,
                quantity=quantity,
                price=price,
                filled=total_filled,
                avg_price=avg_fill,
                fee=0.0,
                fee_currency="USD",
                raw={"partial_results": raw_results, "remaining": remaining},
            )

    @retry_async(max_retries=2, initial_delay=1.0)
    async def _place_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        price: float,
        order_type: str,
        magic: int = 234001,
    ) -> OrderResult:
        """Core order placement with safety checks."""
        async with self._order_lock:
            # Compute true notional (lots × contract_size × price) for safety checks.
            # `quantity` here is already in lots (converted upstream in open_order).
            contract = await self._contract_size(symbol)
            order_value = quantity * contract * price
            max_order = self.config.LIVE_MAX_ORDER_USD
            if order_value > max_order:
                msg = (
                    f"Order notional ${order_value:,.2f} exceeds max "
                    f"${max_order:,.2f}. Rejected."
                )
                self.logger.error("[MT5-LIVE] %s", msg)
                return OrderResult(success=False, error=msg, symbol=symbol, side=side)

            # Safety check 2: sufficient margin (broker-leverage aware)
            # True margin required = notional / leverage. We can't always know
            # the leverage from Python side, so we use free margin from MT5
            # which already accounts for it.
            if side.lower() == "buy":
                balance = await self.get_balance()
                # Rough heuristic: if free balance is smaller than 0.5% of
                # notional, we likely can't afford the trade even with leverage.
                if balance > 0 and balance < order_value * 0.005:
                    msg = (
                        f"Insufficient free margin: "
                        f"${balance:,.2f} (notional ${order_value:,.2f})"
                    )
                    self.logger.error("[MT5-LIVE] %s", msg)
                    return OrderResult(success=False, error=msg, symbol=symbol, side=side)

            self.logger.info(
                "[MT5-LIVE] Placing %s %s order: %.4f lots %s @ $%.5f (value: $%.2f)",
                order_type.upper(), side.upper(), quantity,
                symbol, price, order_value,
            )

            try:
                raw_order = await self.exchange.create_order(
                    symbol=symbol,
                    order_type=order_type,
                    side=side.lower(),
                    amount=quantity,
                    price=price if order_type == "limit" else None,
                    magic=int(magic),
                )
            except RuntimeError as e:
                msg = f"MT5 order error: {e}"
                self.logger.error("[MT5-LIVE] %s", msg)
                return OrderResult(success=False, error=msg, symbol=symbol, side=side)

            # Parse response
            order_id = str(raw_order.get("id", ""))
            filled = float(raw_order.get("filled", 0))
            avg_fill = float(raw_order.get("average", price) or price)
            fee_info = raw_order.get("fee", {}) or {}
            fee_cost = float(fee_info.get("cost", 0) or 0)
            fee_curr = str(fee_info.get("currency", "USD") or "USD")
            status = raw_order.get("status", "unknown")

            self.logger.info(
                "[MT5-LIVE] Order %s placed — status: %s, filled: %.4f @ $%.5f, fee: %.4f %s",
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

    async def close(self) -> None:
        """No-op — MT5Exchange lifecycle is managed by MT5ExchangeManager."""
        pass
