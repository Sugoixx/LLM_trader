"""Price Stream — WebSocket real-time ticker via ccxt.pro.

Connects to an exchange WebSocket and pushes price ticks
to registered callbacks at sub-second latency.
"""

import asyncio
from typing import Optional, Callable, Awaitable, List, Dict, Any

from src.logger.logger import Logger


# Callback signature: async def on_tick(symbol, price, ticker_data)
TickCallback = Callable[[str, float, Dict[str, Any]], Awaitable[None]]


class PriceStream:
    """WebSocket price stream using ccxt.pro watch_ticker.

    Usage:
        stream = PriceStream(logger, exchange_pro, "BTC/USDC")
        stream.on_tick(my_callback)
        await stream.start()   # runs until stop()
    """

    def __init__(
        self,
        logger: Logger,
        exchange,  # ccxt.pro exchange instance
        symbol: str,
        reconnect_delay: float = 5.0,
        max_reconnect_delay: float = 60.0,
    ):
        self.logger = logger
        self.exchange = exchange
        self.symbol = symbol
        self._reconnect_delay = reconnect_delay
        self._max_reconnect_delay = max_reconnect_delay

        self._callbacks: List[TickCallback] = []
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._last_price: float = 0.0
        self._tick_count: int = 0

    def on_tick(self, callback: TickCallback) -> None:
        """Register a callback invoked on each price tick."""
        self._callbacks.append(callback)

    @property
    def last_price(self) -> float:
        """Most recent price from the stream."""
        return self._last_price

    @property
    def is_running(self) -> bool:
        return self._running

    async def start(self) -> None:
        """Start the WebSocket stream in the current task (blocking)."""
        self._running = True
        delay = self._reconnect_delay
        self.logger.info("[PriceStream] Starting WebSocket for %s", self.symbol)

        while self._running:
            try:
                await self._stream_loop()
            except asyncio.CancelledError:
                self.logger.info("[PriceStream] Cancelled")
                break
            except Exception as e:
                if not self._running:
                    break
                self.logger.warning(
                    "[PriceStream] WebSocket error: %s — reconnecting in %.0fs", e, delay
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, self._max_reconnect_delay)

        self.logger.info("[PriceStream] Stopped (%d ticks received)", self._tick_count)

    async def stop(self) -> None:
        """Stop the WebSocket stream gracefully."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _stream_loop(self) -> None:
        """Inner loop — watches ticker and dispatches callbacks."""
        while self._running:
            ticker = await self.exchange.watch_ticker(self.symbol)

            price = float(ticker.get("last") or ticker.get("close") or 0)
            if price <= 0:
                continue

            self._last_price = price
            self._tick_count += 1

            # Dispatch to all registered callbacks concurrently
            if self._callbacks:
                await asyncio.gather(
                    *(cb(self.symbol, price, ticker) for cb in self._callbacks),
                    return_exceptions=True,
                )

    async def close(self) -> None:
        """Stop stream and close the exchange connection."""
        await self.stop()
        try:
            await self.exchange.close()
            self.logger.info("[PriceStream] Exchange WebSocket connection closed")
        except Exception as e:
            self.logger.warning("[PriceStream] Error closing exchange: %s", e)
