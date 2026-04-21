from unittest.mock import MagicMock

import pytest

from src.trading.mt5_order_executor import MT5OrderExecutor


@pytest.mark.asyncio
async def test_close_order_targets_buy_position_tickets_with_sell_side():
    exchange = MagicMock()

    async def _fetch_positions(_symbol):
        return [
            {"ticket": 10, "type": "buy", "volume": 0.10},
            {"ticket": 11, "type": "sell", "volume": 0.10},
        ]

    calls = []

    async def _create_order(**kwargs):
        calls.append(kwargs)
        return {
            "id": "order-close-1",
            "filled": kwargs["amount"],
            "average": 83.05,
            "fee": {"cost": 0.0, "currency": "USD"},
            "status": "closed",
        }

    exchange.fetch_positions = _fetch_positions
    exchange.create_order = _create_order

    executor = MT5OrderExecutor(
        logger=MagicMock(),
        config=MagicMock(LIVE_MAX_ORDER_USD=999999),
        exchange=exchange,
    )

    result = await executor.close_order(
        symbol="CRUDOIL",
        side="sell",
        quantity=0.10,
        price=83.00,
        order_type="market",
    )

    assert result.success is True
    assert len(calls) == 1
    assert calls[0]["position_ticket"] == 10
    assert calls[0]["side"] == "sell"


@pytest.mark.asyncio
async def test_close_order_splits_across_multiple_tickets_when_needed():
    exchange = MagicMock()

    async def _fetch_positions(_symbol):
        return [
            {"ticket": 21, "type": "buy", "volume": 0.10},
            {"ticket": 22, "type": "buy", "volume": 0.10},
        ]

    calls = []

    async def _create_order(**kwargs):
        calls.append(kwargs)
        return {
            "id": f"order-{kwargs['position_ticket']}",
            "filled": kwargs["amount"],
            "average": 83.10,
            "fee": {"cost": 0.0, "currency": "USD"},
            "status": "closed",
        }

    exchange.fetch_positions = _fetch_positions
    exchange.create_order = _create_order

    executor = MT5OrderExecutor(
        logger=MagicMock(),
        config=MagicMock(LIVE_MAX_ORDER_USD=999999),
        exchange=exchange,
    )

    result = await executor.close_order(
        symbol="CRUDOIL",
        side="sell",
        quantity=0.15,
        price=83.00,
        order_type="market",
    )

    assert result.success is True
    assert len(calls) == 2
    assert calls[0]["position_ticket"] == 21
    assert calls[1]["position_ticket"] == 22
    assert calls[0]["amount"] == pytest.approx(0.10)
    assert calls[1]["amount"] == pytest.approx(0.05)
