from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.platforms import mt5_exchange as mt5_exchange_module
from src.platforms.mt5_exchange import MT5Exchange


@pytest.mark.asyncio
async def test_fetch_ticker_falls_back_to_alias_symbol_when_primary_has_no_tick(monkeypatch):
    exchange = object.__new__(MT5Exchange)
    exchange.logger = MagicMock()
    exchange.symbols = {"CRUDOIL", "XTIUSD"}
    exchange._symbol_descriptions = {
        "CRUDOIL": "Crude Oil CFD",
        "XTIUSD": "WTI Crude Oil CFD",
    }

    selected_symbols = []

    def _symbol_select(name, visible):
        selected_symbols.append((name, visible))
        return True

    def _symbol_info_tick(name):
        if name == "CRUDOIL":
            return None
        if name == "XTIUSD":
            return SimpleNamespace(bid=83.04, ask=83.06, last=83.05, volume=11.0, time=1713636991)
        return None

    def _symbol_info(_name):
        return SimpleNamespace(session_high=84.25, session_low=82.90)

    fake_mt5 = SimpleNamespace(
        symbol_select=_symbol_select,
        symbol_info_tick=_symbol_info_tick,
        symbol_info=_symbol_info,
    )
    monkeypatch.setattr(mt5_exchange_module, "mt5", fake_mt5)

    ticker = await MT5Exchange.fetch_ticker(exchange, "CRUDOIL")

    assert ticker["symbol"] == "CRUDOIL"
    assert ticker["info"]["mt5_symbol"] == "XTIUSD"
    assert ticker["last"] == pytest.approx(83.05)
    assert ticker["high"] == pytest.approx(84.25)
    assert ticker["low"] == pytest.approx(82.90)
    assert selected_symbols == [("CRUDOIL", True), ("XTIUSD", True)]


@pytest.mark.asyncio
async def test_fetch_ticker_raises_when_all_symbol_candidates_have_no_tick(monkeypatch):
    exchange = object.__new__(MT5Exchange)
    exchange.logger = MagicMock()
    exchange.symbols = {"CRUDOIL", "XTIUSD"}
    exchange._symbol_descriptions = {
        "CRUDOIL": "Crude Oil CFD",
        "XTIUSD": "WTI Crude Oil CFD",
    }

    def _symbol_select(_name, _visible):
        return True

    def _symbol_info_tick(_name):
        return None

    fake_mt5 = SimpleNamespace(
        symbol_select=_symbol_select,
        symbol_info_tick=_symbol_info_tick,
        symbol_info=lambda _name: None,
    )
    monkeypatch.setattr(mt5_exchange_module, "mt5", fake_mt5)

    with pytest.raises(RuntimeError, match="symbol_info_tick returned None"):
        await MT5Exchange.fetch_ticker(exchange, "CRUDOIL")
