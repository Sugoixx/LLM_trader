"""Performance regression tests for LLM Trader."""

import time
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock

# Test thresholds (in seconds) - adjust based on current performance
ANALYSIS_CYCLE_THRESHOLD = 60.0  # 60 seconds max for analysis cycle
INITIALIZATION_THRESHOLD = 30.0  # 30 seconds max for initialization
AI_CALL_THRESHOLD = 10.0  # 10 seconds max for AI calls


class TestPerformanceRegression:
    """Test performance regression for key operations."""

    @pytest.mark.asyncio
    async def test_analysis_cycle_performance(self):
        """Test that analysis cycle completes within threshold."""
        # Mock dependencies
        mock_logger = MagicMock()
        mock_config = MagicMock()
        mock_config.LOGGER_DEBUG = True

        # Import and mock analysis engine
        from src.analyzer.analysis_engine import AnalysisEngine
        from src.managers.model_manager import ModelManager

        # Mock model manager
        mock_model_manager = AsyncMock()
        mock_model_manager.send_prompt.return_value = {"response": "BUY", "confidence": 0.8}

        # Mock data fetcher
        mock_data_fetcher = AsyncMock()
        mock_data_fetcher.fetch_market_data.return_value = {
            "price": 50000,
            "volume": 1000000,
            "indicators": {"rsi": 65, "macd": 0.5}
        }

        # Create analysis engine with mocks
        analysis_engine = AnalysisEngine(
            logger=mock_logger,
            config=mock_config,
            model_manager=mock_model_manager,
            data_fetcher=mock_data_fetcher
        )

        # Measure analysis time
        start_time = time.perf_counter()
        result = await analysis_engine.analyze_market("BTC/USDT")
        end_time = time.perf_counter()

        duration = end_time - start_time

        # Assert performance
        assert duration < ANALYSIS_CYCLE_THRESHOLD, f"Analysis cycle took {duration:.2f}s, exceeds threshold {ANALYSIS_CYCLE_THRESHOLD}s"
        assert result is not None, "Analysis result should not be None"

    @pytest.mark.asyncio
    async def test_ai_call_performance(self):
        """Test that AI calls complete within threshold."""
        from src.managers.model_manager import ModelManager

        # Mock dependencies
        mock_logger = MagicMock()
        mock_config = MagicMock()
        mock_config.LOGGER_DEBUG = True

        # Mock orchestrator and clients
        mock_orchestrator = AsyncMock()
        mock_orchestrator.send_prompt.return_value = ("BUY signal detected", 100, 0.05)

        # Create model manager
        model_manager = ModelManager(
            logger=mock_logger,
            config=mock_config,
            orchestrator=mock_orchestrator
        )

        # Measure AI call time
        start_time = time.perf_counter()
        response = await model_manager.send_prompt(
            prompt="Analyze BTC trend",
            system_message="You are a trading analyst"
        )
        end_time = time.perf_counter()

        duration = end_time - start_time

        # Assert performance
        assert duration < AI_CALL_THRESHOLD, f"AI call took {duration:.2f}s, exceeds threshold {AI_CALL_THRESHOLD}s"
        assert response is not None, "AI response should not be None"

    def test_memory_usage_during_operation(self):
        """Test memory usage doesn't exceed reasonable limits."""
        try:
            import psutil
            process = psutil.Process()
            memory_mb = process.memory_info().rss / 1024 / 1024

            # Assert memory usage is reasonable (< 500MB for tests)
            assert memory_mb < 500, f"Memory usage {memory_mb:.2f}MB exceeds 500MB limit"
        except ImportError:
            pytest.skip("psutil not available for memory testing")

    @pytest.mark.asyncio
    async def test_async_task_count(self):
        """Test that async task count doesn't grow excessively."""
        initial_tasks = len([t for t in asyncio.all_tasks() if not t.done()])

        # Simulate some async operations
        tasks = [asyncio.sleep(0.1) for _ in range(5)]
        await asyncio.gather(*tasks)

        final_tasks = len([t for t in asyncio.all_tasks() if not t.done()])

        # Task count should not increase significantly
        task_growth = final_tasks - initial_tasks
        assert task_growth < 10, f"Async task count grew by {task_growth}, may indicate leaks"</content>
<parameter name="filePath">C:\Users\julie\OneDrive\Documents\LISTES\WORKSPACE\LLM_trader\PROJET 1\LLM_TRADER\tests\test_performance.py