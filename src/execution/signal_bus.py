"""Signal Bus — asyncio.Queue bridge between Layer 1 (AI) and Layer 2 (Execution).

Layer 1 publishes trading signals (BUY/SELL/CLOSE/UPDATE).
Layer 2 consumes them for optimized execution timing.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, auto
from typing import Optional, Dict, Any


class SignalType(Enum):
    """Types of signals flowing through the bus."""
    OPEN = auto()       # New position signal from AI
    CLOSE = auto()      # Close position signal from AI
    UPDATE = auto()     # Update SL/TP from AI analysis
    CANCEL = auto()     # Cancel pending signal


@dataclass(slots=True, kw_only=True)
class Signal:
    """A trading signal published by Layer 1 for Layer 2 consumption."""
    signal_type: SignalType
    symbol: str
    direction: str = ""         # "LONG" or "SHORT"
    confidence: str = ""        # "HIGH", "MEDIUM", "LOW"
    price_at_signal: float = 0.0
    stop_loss: float = 0.0
    take_profit: float = 0.0
    position_size: float = 0.0  # Fraction of capital (0.0-1.0)
    reasoning: str = ""
    rating: str = ""
    # Multi-position source tag: 'ai' (LLM) or 'fast' (algo consensus).
    source: str = "ai"
    metadata: Dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class SignalBus:
    """Async queue for Layer 1 → Layer 2 signal delivery.

    Thread-safe via asyncio.Queue. Supports non-blocking publish
    and blocking consume with timeout.
    """

    def __init__(self, maxsize: int = 100):
        self._queue: asyncio.Queue[Signal] = asyncio.Queue(maxsize=maxsize)
        self._latest_signal: Optional[Signal] = None

    async def publish(self, signal: Signal) -> None:
        """Publish a signal from Layer 1. Drops oldest if full."""
        if self._queue.full():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        await self._queue.put(signal)
        self._latest_signal = signal

    async def consume(self, timeout: Optional[float] = None) -> Optional[Signal]:
        """Consume next signal. Returns None on timeout."""
        try:
            if timeout is not None:
                return await asyncio.wait_for(self._queue.get(), timeout=timeout)
            return await self._queue.get()
        except asyncio.TimeoutError:
            return None

    def peek_latest(self) -> Optional[Signal]:
        """Return the most recently published signal without consuming."""
        return self._latest_signal

    @property
    def pending_count(self) -> int:
        """Number of unconsumed signals in the queue."""
        return self._queue.qsize()

    def clear(self) -> None:
        """Drain all pending signals."""
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
