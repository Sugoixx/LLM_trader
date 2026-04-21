from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.app import CryptoTradingBot


class _FakeExchange:
    def __init__(self, status):
        self._status = status

    async def get_market_status(self, _symbol, stale_seconds=1800):
        return self._status(stale_seconds)


def _build_bot(config):
    return CryptoTradingBot(
        logger=MagicMock(),
        config=config,
        shutdown_manager=None,
        exchange_manager=MagicMock(),
        market_analyzer=MagicMock(),
        trading_strategy=MagicMock(current_position=None),
        discord_notifier=None,
        keyboard_handler=MagicMock(),
        rag_engine=MagicMock(),
        coingecko_api=None,
        news_client=None,
        market_api=None,
        categories_api=None,
        alternative_me_api=None,
        cryptocompare_session=None,
        persistence=MagicMock(),
        model_manager=MagicMock(),
        brain_service=MagicMock(),
        statistics_service=MagicMock(),
        memory_service=MagicMock(),
        dashboard_state=None,
    )


@pytest.mark.asyncio
async def test_market_closed_skips_llm_when_outside_preopen_window():
    config = SimpleNamespace(
        MT5_ENABLED=True,
        SKIP_LLM_WHEN_MARKET_CLOSED=True,
        PREOPEN_ANALYSIS_MINUTES=20,
        MT5_MARKET_STALE_TICK_SECONDS=1800,
    )

    def _status(_stale):
        return {
            "is_open": False,
            "reason": "no_live_quotes",
            "next_open_utc": (datetime.now(timezone.utc) + timedelta(minutes=60)).isoformat(),
        }

    bot = _build_bot(config)
    bot.current_symbol = "CRUDOIL"
    bot.current_exchange = _FakeExchange(_status)

    should_skip = await bot._should_skip_llm_for_market_hours(is_candle_close=True)
    assert should_skip is True


@pytest.mark.asyncio
async def test_market_closed_allows_single_preopen_analysis_call():
    config = SimpleNamespace(
        MT5_ENABLED=True,
        SKIP_LLM_WHEN_MARKET_CLOSED=True,
        PREOPEN_ANALYSIS_MINUTES=20,
        MT5_MARKET_STALE_TICK_SECONDS=1800,
    )

    next_open = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()

    def _status(_stale):
        return {
            "is_open": False,
            "reason": "stale_quotes_3600s",
            "next_open_utc": next_open,
        }

    bot = _build_bot(config)
    bot.current_symbol = "CRUDOIL"
    bot.current_exchange = _FakeExchange(_status)

    first = await bot._should_skip_llm_for_market_hours(is_candle_close=True)
    second = await bot._should_skip_llm_for_market_hours(is_candle_close=True)

    assert first is False
    assert second is True


@pytest.mark.asyncio
async def test_market_open_does_not_skip_and_resets_preopen_marker():
    config = SimpleNamespace(
        MT5_ENABLED=True,
        SKIP_LLM_WHEN_MARKET_CLOSED=True,
        PREOPEN_ANALYSIS_MINUTES=20,
        MT5_MARKET_STALE_TICK_SECONDS=1800,
    )

    def _status(_stale):
        return {
            "is_open": True,
            "reason": "open",
        }

    bot = _build_bot(config)
    bot.current_symbol = "CRUDOIL"
    bot.current_exchange = _FakeExchange(_status)
    bot._last_preopen_analysis_target = "marker"

    should_skip = await bot._should_skip_llm_for_market_hours(is_candle_close=True)

    assert should_skip is False
    assert bot._last_preopen_analysis_target is None
