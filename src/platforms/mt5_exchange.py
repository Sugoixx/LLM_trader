"""
MT5 Exchange Adapter — ccxt-compatible interface for MetaTrader 5.

Wraps the synchronous MetaTrader5 Python package behind an async interface
that matches what DataFetcher and the rest of LLM_Trader expect from a
ccxt.Exchange object (duck typing).

Requires:
  - MetaTrader5 terminal installed and running on Windows
  - pip install MetaTrader5
"""

import asyncio
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Set

from src.logger.logger import Logger

try:
    import MetaTrader5 as mt5

    MT5_AVAILABLE = True
except ImportError:
    MT5_AVAILABLE = False
    mt5 = None  # type: ignore[assignment]

# --------------------------------------------------------------------------- #
# Timeframe mapping: ccxt-style string → MT5 constant
# --------------------------------------------------------------------------- #
_TF_MAP: Dict[str, int] = {}
if MT5_AVAILABLE:
    _TF_MAP = {
        "1m": mt5.TIMEFRAME_M1,
        "2m": mt5.TIMEFRAME_M2,
        "3m": mt5.TIMEFRAME_M3,
        "4m": mt5.TIMEFRAME_M4,
        "5m": mt5.TIMEFRAME_M5,
        "6m": mt5.TIMEFRAME_M6,
        "10m": mt5.TIMEFRAME_M10,
        "12m": mt5.TIMEFRAME_M12,
        "15m": mt5.TIMEFRAME_M15,
        "20m": mt5.TIMEFRAME_M20,
        "30m": mt5.TIMEFRAME_M30,
        "1h": mt5.TIMEFRAME_H1,
        "2h": mt5.TIMEFRAME_H2,
        "3h": mt5.TIMEFRAME_H3,
        "4h": mt5.TIMEFRAME_H4,
        "6h": mt5.TIMEFRAME_H6,
        "8h": mt5.TIMEFRAME_H8,
        "12h": mt5.TIMEFRAME_H12,
        "1d": mt5.TIMEFRAME_D1,
        "1w": mt5.TIMEFRAME_W1,
        "1M": mt5.TIMEFRAME_MN1,
    }


class MT5Exchange:
    """Async adapter that makes MetaTrader 5 look like a ccxt Exchange.

    Only the subset of the ccxt API used by DataFetcher / app.py is
    implemented.  Heavy MT5 calls are offloaded to a thread via
    ``asyncio.to_thread`` so they don't block the event loop.
    """

    def __init__(
        self,
        logger: Logger,
        login: int,
        password: str,
        server: str,
        terminal_path: Optional[str] = None,
    ):
        if not MT5_AVAILABLE:
            raise ImportError(
                "MetaTrader5 package not installed. "
                "Run: pip install MetaTrader5  (Windows only)"
            )

        self.logger = logger
        self._login = login
        self._password = password
        self._server = server
        self._terminal_path = terminal_path

        # ccxt-compatible public attributes
        self.id = "mt5"
        self.name = "MetaTrader 5"
        self.symbols: Set[str] = set()
        self.timeframes: Dict[str, Any] = {k: True for k in _TF_MAP}
        self.has: Dict[str, bool] = {
            "fetchOHLCV": True,
            "fetchTicker": True,
            "fetchOrderBook": False,   # MT5 DOM is symbol-specific, not universal
            "fetchTickers": False,
        }
        self._connected = False

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    async def connect(self) -> bool:
        """Initialize MT5 terminal and authenticate."""
        ok = await asyncio.to_thread(self._sync_connect)
        if ok:
            self._connected = True
            await self.load_markets()
        return ok

    def _sync_connect(self) -> bool:
        """Synchronous MT5 init + login (runs in thread)."""
        kwargs: Dict[str, Any] = {}
        if self._terminal_path:
            kwargs["path"] = self._terminal_path

        if not mt5.initialize(**kwargs):
            err = mt5.last_error()
            self.logger.error("MT5 initialize failed: %s", err)
            return False

        if not mt5.login(self._login, password=self._password, server=self._server):
            err = mt5.last_error()
            self.logger.error("MT5 login failed: %s", err)
            mt5.shutdown()
            return False

        account = mt5.account_info()
        if account:
            self.logger.info(
                "MT5 connected: %s @ %s (balance: %.2f %s)",
                account.login, account.server, account.balance, account.currency,
            )
            # Detect netting vs hedging mode so the strategy can refuse
            # opposing-direction opens in netting accounts.
            try:
                # 0 = ACCOUNT_MARGIN_MODE_RETAIL_NETTING
                # 2 = ACCOUNT_MARGIN_MODE_RETAIL_HEDGING
                margin_mode = int(getattr(account, "margin_mode", 0))
                self.hedging_mode = (margin_mode == 2)
                self.logger.info(
                    "MT5 margin_mode=%s → hedging_mode=%s",
                    margin_mode, self.hedging_mode,
                )
            except Exception:
                # Safe default: assume hedging (permissive) if we can't tell.
                self.hedging_mode = True
        else:
            self.hedging_mode = True
        return True

    async def load_markets(self) -> None:
        """Populate self.symbols from ALL MT5 symbols (not just MarketWatch visible)."""
        symbols_info = await asyncio.to_thread(mt5.symbols_get)
        if symbols_info is None:
            self.logger.warning("MT5 symbols_get returned None")
            return

        self.symbols = set()
        self._symbol_descriptions: Dict[str, str] = {}
        for s in symbols_info:
            self.symbols.add(s.name)
            self._symbol_descriptions[s.name] = s.description or ""
        self.logger.info("MT5 loaded %d symbols (%d visible in MarketWatch)",
                         len(self.symbols),
                         sum(1 for s in symbols_info if s.visible))

    async def close(self) -> None:
        """Shutdown MT5 connection."""
        if self._connected:
            await asyncio.to_thread(mt5.shutdown)
            self._connected = False
            self.logger.info("MT5 connection closed")

    # ------------------------------------------------------------------ #
    # Data methods (ccxt-compatible signatures)
    # ------------------------------------------------------------------ #

    async def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str = "1h",
        since: Optional[int] = None,
        limit: int = 500,
        params: Optional[Dict] = None,
    ) -> List[List[float]]:
        """Fetch OHLCV candles — returns list of [timestamp_ms, O, H, L, C, V].

        This matches the ccxt format that DataFetcher expects.
        """
        mt5_tf = _TF_MAP.get(timeframe)
        if mt5_tf is None:
            raise ValueError(f"Unsupported MT5 timeframe: {timeframe}")

        mt5_symbol = self._resolve_symbol(symbol)

        # Ensure the symbol is selected in MarketWatch
        await asyncio.to_thread(mt5.symbol_select, mt5_symbol, True)

        if since is not None:
            # Fetch from a specific timestamp
            utc_from = datetime.fromtimestamp(since / 1000, tz=timezone.utc)
            rates = await asyncio.to_thread(
                mt5.copy_rates_from, mt5_symbol, mt5_tf, utc_from, limit
            )
        else:
            # Fetch latest N candles
            rates = await asyncio.to_thread(
                mt5.copy_rates_from_pos, mt5_symbol, mt5_tf, 0, limit
            )

        if rates is None or len(rates) == 0:
            self.logger.warning("MT5 returned no OHLCV data for %s %s", mt5_symbol, timeframe)
            return []

        # Convert structured numpy array → ccxt-style list of lists
        # MT5 rates dtype: time(s), open, high, low, close, tick_volume, spread, real_volume
        result = []
        for r in rates:
            result.append([
                int(r["time"]) * 1000,  # timestamp ms
                float(r["open"]),
                float(r["high"]),
                float(r["low"]),
                float(r["close"]),
                float(r["tick_volume"]),  # tick_volume as proxy for volume
            ])
        return result

    async def fetch_ticker(self, symbol: str, params: Optional[Dict] = None) -> Dict[str, Any]:
        """Fetch current tick data — returns ccxt-style ticker dict."""
        resolved_symbol = self._resolve_symbol(symbol)
        mt5_symbol, tick = await self._get_available_tick_symbol(symbol, resolved_symbol)
        if tick is None:
            raise RuntimeError(
                f"MT5 symbol_info_tick returned None for {resolved_symbol} (requested {symbol})"
            )

        info = await asyncio.to_thread(mt5.symbol_info, mt5_symbol)

        bid = float(tick.bid)
        ask = float(tick.ask)
        last = float(tick.last) if tick.last > 0 else (bid + ask) / 2
        volume = float(tick.volume) if hasattr(tick, "volume") else 0.0

        # Session high/low from symbol_info (if available)
        session_high = float(getattr(info, 'session_high', 0)) if info else None
        session_low = float(getattr(info, 'session_low', 0)) if info else None
        session_high = session_high if session_high and session_high > 0 else None
        session_low = session_low if session_low and session_low > 0 else None

        return {
            "symbol": symbol,
            "timestamp": int(tick.time) * 1000,
            "datetime": datetime.fromtimestamp(tick.time, tz=timezone.utc).isoformat(),
            "bid": bid,
            "ask": ask,
            "last": last,
            "close": last,
            "high": session_high,
            "low": session_low,
            "volume": volume,
            "info": {"source": "mt5", "mt5_symbol": mt5_symbol},
        }

    async def fetch_order_book(self, symbol: str, limit: int = 10, params: Optional[Dict] = None) -> Optional[Dict]:
        """MT5 does not provide a universal order book. Returns None."""
        return None

    # ------------------------------------------------------------------ #
    # Trading methods (used by MT5OrderExecutor)
    # ------------------------------------------------------------------ #

    async def create_order(
        self,
        symbol: str,
        order_type: str,
        side: str,
        amount: float,
        price: Optional[float] = None,
        position_ticket: Optional[int] = None,
        magic: int = 234000,
    ) -> Dict[str, Any]:
        """Place an order on MT5 — ccxt-compatible return format.

        Args:
            symbol: Trading symbol (ccxt or MT5 native name)
            order_type: "market" or "limit"
            side: "buy" or "sell"
            amount: Volume in lots
            price: Limit price (required for limit orders)
        """
        mt5_symbol = self._resolve_symbol(symbol)
        await asyncio.to_thread(mt5.symbol_select, mt5_symbol, True)
        info = await asyncio.to_thread(mt5.symbol_info, mt5_symbol)
        if info is None:
            raise RuntimeError(f"MT5 symbol_info returned None for {mt5_symbol}")

        tick = await asyncio.to_thread(mt5.symbol_info_tick, mt5_symbol)
        if tick is None:
            raise RuntimeError(f"MT5 symbol_info_tick returned None for {mt5_symbol}")

        # Normalize volume to MT5 constraints (volume_min, volume_max, volume_step)
        vol_step = info.volume_step
        vol_min = info.volume_min
        vol_max = info.volume_max
        # Round DOWN to nearest volume_step to avoid exceeding allocation
        normalized = max(vol_min, min(vol_max, round(
            int(amount / vol_step) * vol_step, 10
        )))
        if normalized != amount:
            # Log adjustment for transparency
            import logging
            logging.getLogger(__name__).info(
                "MT5 volume normalized: %.6f → %.6f (step=%.4f, min=%.4f)",
                amount, normalized, vol_step, vol_min,
            )
        amount = normalized

        # Determine MT5 order type and action
        if side.lower() == "buy":
            action_type = mt5.ORDER_TYPE_BUY if order_type == "market" else mt5.ORDER_TYPE_BUY_LIMIT
            fill_price = tick.ask if order_type == "market" else price
        else:
            action_type = mt5.ORDER_TYPE_SELL if order_type == "market" else mt5.ORDER_TYPE_SELL_LIMIT
            fill_price = tick.bid if order_type == "market" else price

        request = {
            "action": mt5.TRADE_ACTION_DEAL if order_type == "market" else mt5.TRADE_ACTION_PENDING,
            "symbol": mt5_symbol,
            "volume": float(amount),
            "type": action_type,
            "price": float(fill_price),
            "deviation": info.spread * 2,  # slippage tolerance in points
            "magic": int(magic),  # per-slot magic number (234001 AI / 234002 Fast)
            "comment": "LLM_Trader",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        # In hedging mode, closing a specific position requires the ticket.
        if position_ticket is not None and request["action"] == mt5.TRADE_ACTION_DEAL:
            request["position"] = int(position_ticket)

        result = await asyncio.to_thread(mt5.order_send, request)
        if result is None:
            err = mt5.last_error()
            raise RuntimeError(f"MT5 order_send returned None: {err}")

        if result.retcode != mt5.TRADE_RETCODE_DONE:
            raise RuntimeError(
                f"MT5 order failed: retcode={result.retcode}, comment={result.comment}"
            )

        # Return ccxt-compatible dict
        return {
            "id": str(result.order if result.order else result.deal),
            "symbol": symbol,
            "type": order_type,
            "side": side,
            "amount": amount,
            "price": float(fill_price),
            "filled": float(result.volume),
            "average": float(result.price) if result.price > 0 else float(fill_price),
            "status": "closed" if order_type == "market" else "open",
            "fee": {"cost": 0.0, "currency": "USD"},
            "info": {
                "retcode": result.retcode,
                "deal": result.deal,
                "order": result.order,
                "comment": result.comment,
            },
        }

    async def modify_position(self, ticket: int, sl: float, tp: float) -> None:
        """Modify SL/TP of an open position via TRADE_ACTION_SLTP.

        Args:
            ticket: MT5 position ticket number
            sl: New stop-loss price (0.0 to remove)
            tp: New take-profit price (0.0 to remove)

        Raises:
            RuntimeError: if MT5 rejects the modification
        """
        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "position": int(ticket),
            "sl": float(sl),
            "tp": float(tp),
        }
        result = await asyncio.to_thread(mt5.order_send, request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            err = mt5.last_error() if result is None else result.comment
            raise RuntimeError(
                f"MT5 modify_position failed: ticket={ticket} sl={sl} tp={tp}: {err}"
            )

    async def cancel_order(self, order_id: str, symbol: str = "") -> Dict[str, Any]:
        """Cancel a pending MT5 order."""
        request = {
            "action": mt5.TRADE_ACTION_REMOVE,
            "order": int(order_id),
        }
        result = await asyncio.to_thread(mt5.order_send, request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            err = mt5.last_error() if result is None else result.comment
            raise RuntimeError(f"MT5 cancel failed: {err}")
        return {"id": order_id, "status": "canceled"}

    async def fetch_balance(self) -> Dict[str, Any]:
        """Fetch MT5 account balance — ccxt-compatible format."""
        account = await asyncio.to_thread(mt5.account_info)
        if account is None:
            return {"free": {}, "total": {}, "used": {}}

        currency = account.currency  # e.g. "USD", "EUR"
        return {
            "free": {currency: float(account.margin_free)},
            "total": {currency: float(account.balance)},
            "used": {currency: float(account.margin)},
            "info": {
                "balance": float(account.balance),
                "equity": float(account.equity),
                "margin": float(account.margin),
                "margin_free": float(account.margin_free),
                "margin_level": float(account.margin_level) if account.margin_level else 0.0,
                "profit": float(account.profit),
                "leverage": account.leverage,
            },
        }

    async def fetch_open_orders(self, symbol: str = "") -> List[Dict[str, Any]]:
        """Fetch pending orders for a symbol."""
        mt5_symbol = self._resolve_symbol(symbol) if symbol else None
        if mt5_symbol:
            orders = await asyncio.to_thread(mt5.orders_get, symbol=mt5_symbol)
        else:
            orders = await asyncio.to_thread(mt5.orders_get)

        if orders is None:
            return []

        result = []
        for o in orders:
            result.append({
                "id": str(o.ticket),
                "symbol": o.symbol,
                "type": "limit",
                "side": "buy" if o.type in (mt5.ORDER_TYPE_BUY_LIMIT, mt5.ORDER_TYPE_BUY_STOP) else "sell",
                "amount": float(o.volume_current),
                "price": float(o.price_open),
                "status": "open",
                "info": {"ticket": o.ticket, "magic": o.magic},
            })
        return result

    async def fetch_positions(self, symbol: str = "") -> List[Dict[str, Any]]:
        """Fetch open MT5 positions (not available in ccxt, MT5-specific)."""
        mt5_symbol = self._resolve_symbol(symbol) if symbol else None
        if mt5_symbol:
            positions = await asyncio.to_thread(mt5.positions_get, symbol=mt5_symbol)
        else:
            positions = await asyncio.to_thread(mt5.positions_get)

        if positions is None:
            return []

        result = []
        for p in positions:
            result.append({
                "ticket": p.ticket,
                "symbol": p.symbol,
                "type": "buy" if p.type == mt5.POSITION_TYPE_BUY else "sell",
                "volume": float(p.volume),
                "price_open": float(p.price_open),
                "price_current": float(p.price_current),
                "profit": float(p.profit),
                "swap": float(p.swap),
                "sl": float(p.sl),
                "tp": float(p.tp),
                "magic": p.magic,
                "comment": p.comment,
            })
        return result

    async def get_market_status(self, symbol: str, stale_seconds: int = 1800) -> Dict[str, Any]:
        """Return market-open status for a symbol with best-effort next-open estimate."""
        mt5_symbol = self._resolve_symbol(symbol)
        return await asyncio.to_thread(
            self._sync_get_market_status,
            mt5_symbol,
            max(0, int(stale_seconds)),
        )

    def _sync_get_market_status(self, mt5_symbol: str, stale_seconds: int) -> Dict[str, Any]:
        """Synchronous market status check (runs in thread)."""
        status: Dict[str, Any] = {
            "symbol": mt5_symbol,
            "is_open": True,
            "reason": "open",
            "tick_age_seconds": None,
            "next_open_utc": None,
        }

        try:
            mt5.symbol_select(mt5_symbol, True)
            info = mt5.symbol_info(mt5_symbol)
            if info is None:
                status.update({"is_open": False, "reason": "symbol_info_unavailable"})
                return status

            tick = mt5.symbol_info_tick(mt5_symbol)
            now_ts = time.time()

            has_live_quote = False
            tick_age_seconds: Optional[float] = None
            if tick is not None:
                bid = float(getattr(tick, "bid", 0.0) or 0.0)
                ask = float(getattr(tick, "ask", 0.0) or 0.0)
                last = float(getattr(tick, "last", 0.0) or 0.0)
                has_live_quote = (bid > 0.0) or (ask > 0.0) or (last > 0.0)
                tick_time = float(getattr(tick, "time", 0.0) or 0.0)
                if tick_time > 0:
                    tick_age_seconds = max(0.0, now_ts - tick_time)

            trade_mode = getattr(info, "trade_mode", None)
            disabled_mode = getattr(mt5, "SYMBOL_TRADE_MODE_DISABLED", None)
            trading_disabled = disabled_mode is not None and trade_mode == disabled_mode
            stale_quotes = (
                tick_age_seconds is not None
                and stale_seconds > 0
                and tick_age_seconds > stale_seconds
            )

            is_open = (not trading_disabled) and has_live_quote and (not stale_quotes)

            if trading_disabled:
                reason = "trade_mode_disabled"
            elif not has_live_quote:
                reason = "no_live_quotes"
            elif stale_quotes:
                reason = f"stale_quotes_{int(tick_age_seconds or 0)}s"
            else:
                reason = "open"

            status["is_open"] = is_open
            status["reason"] = reason
            status["tick_age_seconds"] = tick_age_seconds

            if not is_open:
                next_open = self._estimate_next_open_utc(mt5_symbol, now_ts)
                if next_open is not None:
                    status["next_open_utc"] = next_open.isoformat()
            else:
                # Market is open — compute the end of the current session so
                # callers (Fast Trading Mode) can avoid opening new positions
                # just before a close.
                next_close = self._estimate_current_session_close_utc(mt5_symbol, now_ts)
                if next_close is not None:
                    status["next_close_utc"] = next_close.isoformat()

            return status
        except Exception as e:
            self.logger.warning("Failed market-status check for %s: %s", mt5_symbol, e)
            return status

    def _estimate_next_open_utc(self, mt5_symbol: str, now_ts: float) -> Optional[datetime]:
        """Estimate next market open from MT5 session metadata when available."""
        session_trade_fn = getattr(mt5, "symbol_info_session_trade", None)
        if not callable(session_trade_fn):
            return None

        now_dt = datetime.fromtimestamp(now_ts, tz=timezone.utc)
        candidates: List[datetime] = []

        for day_offset in range(0, 8):
            day = (now_dt.weekday() + day_offset) % 7
            day_dt = (now_dt + timedelta(days=day_offset)).replace(
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            )

            session_idx = 0
            while True:
                session = session_trade_fn(mt5_symbol, day, session_idx)
                if session is None:
                    break

                start_sec = self._extract_session_second(session, index=0)
                end_sec = self._extract_session_second(session, index=1)
                session_idx += 1

                if start_sec is None or end_sec is None:
                    continue

                open_dt = day_dt + timedelta(seconds=start_sec)
                close_dt = day_dt + timedelta(seconds=end_sec)

                if day_offset == 0 and open_dt <= now_dt < close_dt:
                    return now_dt

                if open_dt > now_dt:
                    candidates.append(open_dt)

        return min(candidates) if candidates else None

    def _estimate_current_session_close_utc(
        self, mt5_symbol: str, now_ts: float
    ) -> Optional[datetime]:
        """Return the close time of the session currently in progress, if any.

        Used by Fast Trading Mode to avoid opening new positions right before
        a scheduled market close.
        """
        session_trade_fn = getattr(mt5, "symbol_info_session_trade", None)
        if not callable(session_trade_fn):
            return None

        now_dt = datetime.fromtimestamp(now_ts, tz=timezone.utc)

        # Check today and yesterday (sessions spanning midnight UTC).
        for day_offset in (0, -1):
            day = (now_dt.weekday() + day_offset) % 7
            day_dt = (now_dt + timedelta(days=day_offset)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )

            session_idx = 0
            while True:
                session = session_trade_fn(mt5_symbol, day, session_idx)
                if session is None:
                    break

                start_sec = self._extract_session_second(session, index=0)
                end_sec = self._extract_session_second(session, index=1)
                session_idx += 1

                if start_sec is None or end_sec is None:
                    continue

                open_dt = day_dt + timedelta(seconds=start_sec)
                close_dt = day_dt + timedelta(seconds=end_sec)
                if open_dt <= now_dt < close_dt:
                    return close_dt

        return None

    @staticmethod
    def _extract_session_second(session: Any, index: int) -> Optional[int]:
        """Extract session second offset from MT5 tuple/namedtuple/object."""
        value = None

        if isinstance(session, (list, tuple)) and len(session) > index:
            value = session[index]
        elif hasattr(session, "from") and index == 0:
            value = getattr(session, "from")
        elif hasattr(session, "to") and index == 1:
            value = getattr(session, "to")

        if value is None:
            return None

        if isinstance(value, datetime):
            return value.hour * 3600 + value.minute * 60 + value.second

        try:
            as_int = int(value)
        except (TypeError, ValueError):
            return None

        if as_int < 0:
            return None
        return as_int

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    # Commodity/Forex keyword patterns for fuzzy symbol resolution
    # Order matters: more specific terms first to avoid false matches
    _KEYWORD_MAP: Dict[str, List[str]] = {
        "XTIUSD":   ["WTI CRUDE", "CRUDOIL", "LIGHT CRUDE", "WTI"],
        "XBRUSD":   ["BRENT CRUDE", "BRENT"],
        "XAUUSD":   ["GOLD"],
        "XAGUSD":   ["SILVER"],
        "XNGUSD":   ["NATURAL GAS", "NATGAS"],
    }

    async def _get_available_tick_symbol(self, symbol: str, resolved_symbol: str) -> tuple[str, Any]:
        """Return first symbol candidate that provides a tick (or resolved_symbol, None)."""
        for candidate in self._tick_symbol_candidates(symbol, resolved_symbol):
            if candidate not in self.symbols:
                continue

            await asyncio.to_thread(mt5.symbol_select, candidate, True)
            tick = await asyncio.to_thread(mt5.symbol_info_tick, candidate)
            if tick is None:
                continue

            if candidate != resolved_symbol:
                self.logger.info(
                    "MT5 ticker fallback: '%s' -> '%s' (requested '%s')",
                    resolved_symbol,
                    candidate,
                    symbol,
                )
            return candidate, tick

        return resolved_symbol, None

    def _tick_symbol_candidates(self, symbol: str, resolved_symbol: str) -> List[str]:
        """Build ordered symbol candidates for quote retrieval."""
        candidates: List[str] = []

        def _add(value: str) -> None:
            if value and value not in candidates:
                candidates.append(value)

        clean = symbol.replace("/", "")
        upper_clean = clean.upper()
        _add(resolved_symbol)
        _add(symbol)
        _add(clean)

        # Add canonical commodity symbols when input matches known aliases.
        keyword_terms: List[str] = []
        for standard_name, keywords in self._KEYWORD_MAP.items():
            normalized = {k.replace(" ", "").upper() for k in keywords}
            if upper_clean == standard_name or upper_clean in normalized:
                _add(standard_name)
                keyword_terms = keywords
                break

        descriptions = getattr(self, "_symbol_descriptions", {})
        if not keyword_terms:
            keyword_terms = [clean, symbol.replace("/", " ")]

        for mt5_name, desc in descriptions.items():
            desc_upper = (desc or "").upper()
            for term in keyword_terms:
                if term.upper() in desc_upper:
                    _add(mt5_name)
                    break

        return candidates

    def _resolve_symbol(self, symbol: str) -> str:
        """Convert ccxt-style 'BASE/QUOTE' or standard names to the broker's MT5 symbol.

        Resolution order:
        1. Direct match in broker symbols
        2. Slash-stripped match (BTC/USD → BTCUSD)
        3. Static alias table (WTI/USD → try XTIUSD, CRUDOIL, etc.)
        4. Keyword search in symbol descriptions (broker-agnostic)
        5. Fallback: return as-is
        """
        # 1. Direct match
        if symbol in self.symbols:
            return symbol

        # 2. Slash-stripped
        clean = symbol.replace("/", "")
        if clean in self.symbols:
            return clean

        # 3. Static alias table — try each known name for the commodity
        _ALIASES: Dict[str, List[str]] = {
            "WTI/USD":    ["XTIUSD", "CRUDOIL", "WTI", "OIL", "CrudeOilUS"],
            "OIL/USD":    ["XTIUSD", "CRUDOIL", "WTI", "OIL", "CrudeOilUS"],
            "BRENT/USD":  ["XBRUSD", "BRENT", "CrudeOilUK"],
            "GOLD/USD":   ["XAUUSD", "GOLD"],
            "SILVER/USD": ["XAGUSD", "SILVER"],
            "NATGAS/USD": ["XNGUSD", "NATGAS"],
        }
        candidates = _ALIASES.get(symbol.upper(), [])
        # Also try the raw symbol as a keyword
        if not candidates:
            candidates = [clean]

        for candidate in candidates:
            if candidate in self.symbols:
                self.logger.info("Symbol '%s' resolved to '%s' (alias match)", symbol, candidate)
                return candidate

        # 4. Keyword search in broker descriptions (handles any broker naming)
        upper = symbol.upper().replace("/", "")
        # Build search keywords from the keyword map
        search_terms: List[str] = []
        for standard_name, keywords in self._KEYWORD_MAP.items():
            if upper == standard_name or upper in [k.replace(" ", "") for k in keywords]:
                search_terms = keywords
                break
        # If no keyword map hit, use the symbol itself as search term
        if not search_terms:
            search_terms = [clean, symbol.replace("/", " ")]

        descriptions = getattr(self, "_symbol_descriptions", {})
        for mt5_name, desc in descriptions.items():
            desc_upper = desc.upper()
            for term in search_terms:
                if term.upper() in desc_upper:
                    # Prefer CFD symbols (not futures with expiry), skip ETFs/stocks
                    if mt5_name.startswith("#") or mt5_name.startswith("_"):
                        continue
                    self.logger.info(
                        "Symbol '%s' resolved to '%s' via description: '%s'",
                        symbol, mt5_name, desc,
                    )
                    return mt5_name

        # 5. Fallback
        self.logger.warning(
            "Symbol '%s' not found in MT5 (%d symbols). "
            "Check MT5 terminal for the correct name.",
            symbol, len(self.symbols),
        )
        return symbol
