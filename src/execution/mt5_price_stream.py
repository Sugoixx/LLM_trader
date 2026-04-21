"""MT5 Price Stream — polling-based real-time ticker for MetaTrader 5.

Replaces the ccxt.pro WebSocket PriceStream when running in MT5 mode.
Polls MT5Exchange.fetch_ticker() at a configurable interval and dispatches
price ticks to registered callbacks — same interface as PriceStream.
"""

import asyncio
from typing import Optional, Callable, Awaitable, List, Dict, Any

from src.logger.logger import Logger

# Same callback signature as PriceStream
TickCallback = Callable[[str, float, Dict[str, Any]], Awaitable[None]]


class MT5PriceStream:
    """Polling-based price stream using MT5Exchange.fetch_ticker.

    Drop-in replacement for PriceStream (WebSocket) when trading via MT5.
    Polls at `poll_interval` seconds (default 2s — fast enough for 1m+ candles,
    light on CPU since fetch_ticker is a single MT5 call).

    Usage:
        stream = MT5PriceStream(logger, mt5_exchange, "XTIUSD")
        stream.on_tick(my_callback)
        await stream.start()   # runs until stop()
    """

    def __init__(
        self,
        logger: Logger,
        exchange,  # MT5Exchange instance
        symbol: str,
        poll_interval: float = 2.0,
    ):
        self.logger = logger
        self.exchange = exchange
        self.symbol = symbol
        self._poll_interval = poll_interval

        self._callbacks: List[TickCallback] = []
        self._running = False
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
        """Start the polling loop (blocking — run as a task)."""
        self._running = True
        self.logger.info(
            "[MT5PriceStream] Starting polling for %s (interval=%.1fs)",
            self.symbol, self._poll_interval,
        )

        while self._running:
            try:
                ticker = await self.exchange.fetch_ticker(self.symbol)
                price = float(ticker.get("last") or ticker.get("close") or 0)
                if price <= 0:
                    await asyncio.sleep(self._poll_interval)
                    continue

                # Only dispatch if price actually changed
                if price != self._last_price:
                    self._last_price = price
                    self._tick_count += 1

                    if self._callbacks:
                        await asyncio.gather(
                            *(cb(self.symbol, price, ticker) for cb in self._callbacks),
                            return_exceptions=True,
                        )

            except asyncio.CancelledError:
                break
            except Exception as e:
                if not self._running:
                    break
                self.logger.warning("[MT5PriceStream] Tick error: %s", e)

            await asyncio.sleep(self._poll_interval)

        self.logger.info(
            "[MT5PriceStream] Stopped (%d ticks dispatched)", self._tick_count
        )

    async def stop(self) -> None:
        """Stop the polling loop."""
        self._running = False

    async def close(self) -> None:
        """Stop and clean up (no exchange to close — managed by MT5ExchangeManager)."""
        await self.stop()
