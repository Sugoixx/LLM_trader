"""Contracts and protocols for dependency injection"""

from .model_contract import ModelManagerProtocol
from .decision_gate_contract import DecisionGateProtocol, GateVerdict

__all__ = ["ModelManagerProtocol", "DecisionGateProtocol", "GateVerdict"]
