"""Tests for broker position synchronization at startup."""

from unittest.mock import MagicMock, AsyncMock
from datetime import datetime, timezone

import pytest

from src.trading.trading_strategy import TradingStrategy
from src.trading.data_models import Position


@pytest.mark.asyncio
async def test_sync_broker_positions_loads_first_position_when_no_local():
    """When no local position but 2 positions on broker, load first one."""
    strategy = MagicMock(spec=TradingStrategy)
    strategy.current_position = None  # No local position
    strategy.persistence = MagicMock()
    strategy.persistence.async_save_position = AsyncMock()
    strategy.logger = MagicMock()

    broker_positions = [
        {
            "ticket": 10,
            "symbol": "CRUDOIL",
            "type": "buy",
            "volume": 0.1,
            "price_open": 83.03,
            "price_current": 83.05,
            "sl": 82.50,
            "tp": 84.20,
            "profit": 0.2,
            "swap": 0.0,
            "magic": 234000,
            "comment": "LLM_Trader",
        },
        {
            "ticket": 11,
            "symbol": "CRUDOIL",
            "type": "sell",
            "volume": 0.05,
            "price_open": 83.10,
            "price_current": 83.05,
            "sl": 83.50,
            "tp": 82.90,
            "profit": 0.25,
            "swap": 0.0,
            "magic": 234000,
            "comment": "LLM_Trader",
        },
    ]

    exchange = MagicMock()
    exchange.fetch_positions = AsyncMock(return_value=broker_positions)

    # Real call with mocked persistence and logger
    real_strategy = TradingStrategy(
        logger=strategy.logger,
        exchange_manager=MagicMock(),
        persistence=strategy.persistence,
        brain_service=MagicMock(),
        statistics_service=MagicMock(),
        memory_service=MagicMock(),
        risk_manager=MagicMock(),
        config=MagicMock(),
        position_extractor=MagicMock(),
        position_factory=MagicMock(),
        debate_service=None,
        order_executor=None,
    )
    real_strategy.current_position = None  # No local position

    # Call sync
    count = await real_strategy.sync_broker_positions("CRUDOIL", exchange)

    # Verify
    assert count == 2
    assert real_strategy.current_position is not None
    assert real_strategy.current_position.direction == "LONG"
    assert real_strategy.current_position.entry_price == 83.03
    assert real_strategy.current_position.stop_loss == 82.50
    assert real_strategy.current_position.take_profit == 84.20
    assert real_strategy.current_position.size == 0.1
    assert real_strategy.current_position.symbol == "CRUDOIL"


@pytest.mark.asyncio
async def test_sync_broker_positions_returns_0_when_no_positions():
    """When no positions on broker, return 0 and leave current_position unchanged."""
    strategy_logger = MagicMock()
    persistence = MagicMock()
    persistence.async_save_position = AsyncMock()

    exchange = MagicMock()
    exchange.fetch_positions = AsyncMock(return_value=[])

    real_strategy = TradingStrategy(
        logger=strategy_logger,
        exchange_manager=MagicMock(),
        persistence=persistence,
        brain_service=MagicMock(),
        statistics_service=MagicMock(),
        memory_service=MagicMock(),
        risk_manager=MagicMock(),
        config=MagicMock(),
        position_extractor=MagicMock(),
        position_factory=MagicMock(),
        debate_service=None,
        order_executor=None,
    )
    real_strategy.current_position = None

    count = await real_strategy.sync_broker_positions("CRUDOIL", exchange)

    assert count == 0
    assert real_strategy.current_position is None


@pytest.mark.asyncio
async def test_sync_broker_positions_skips_when_exchange_has_no_fetch_positions():
    """When exchange has no fetch_positions method, return 0."""
    strategy_logger = MagicMock()
    persistence = MagicMock()

    exchange = MagicMock(spec=[])  # Empty spec, no fetch_positions

    real_strategy = TradingStrategy(
        logger=strategy_logger,
        exchange_manager=MagicMock(),
        persistence=persistence,
        brain_service=MagicMock(),
        statistics_service=MagicMock(),
        memory_service=MagicMock(),
        risk_manager=MagicMock(),
        config=MagicMock(),
        position_extractor=MagicMock(),
        position_factory=MagicMock(),
        debate_service=None,
        order_executor=None,
    )

    count = await real_strategy.sync_broker_positions("CRUDOIL", exchange)

    assert count == 0
