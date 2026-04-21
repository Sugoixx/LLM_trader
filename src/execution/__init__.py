"""Layer 2 — Execution Engine for real-time position monitoring and speed trading.

Provides WebSocket price streaming, real-time SL/TP monitoring, trailing stops,
and signal bus for decoupled communication with Layer 1 (AI analysis).
"""

from .signal_bus import SignalBus, Signal, SignalType
from .price_stream import PriceStream
from .position_monitor import PositionMonitor
from .execution_engine import ExecutionEngine
from .decision_gate import DecisionGate

__all__ = [
    "SignalBus",
    "Signal",
    "SignalType",
    "PriceStream",
    "PositionMonitor",
    "ExecutionEngine",
    "DecisionGate",
]
