# LLM_Trader Architecture Notes

## Recent Changes (April 2026)

### New Modules Added
- `src/trading/order_executor.py` — OrderExecutorProtocol, DemoExecutor, LiveExecutor (Binance via CCXT)
- `src/trading/debate_service.py` — Bull/Bear debate service reducing confirmation bias
- `src/trading/backtest_engine.py` — Historical candle replay backtesting

### Position Extractor — 7-Tuple Contract
All extract methods return: `(signal, confidence, stop_loss, take_profit, position_size, reasoning, rating)`
- Rating is a `Rating` object: STRONG_BUY / BUY / NEUTRAL / SELL / STRONG_SELL
- Breaking change from original 6-tuple — all callers updated

### Data Models Added (`src/trading/data_models.py`)
- `Rating` — 5-tier conviction with numeric score + label
- `DebateArgument` — Single bull/bear argument
- `DebateResult` — Full debate: bull_args, bear_args, synthesis, final_bias, consensus_confidence
- `OrderResult` — (in order_executor.py) Exchange fill result

### Config Sections Added
- `[debate]` — enabled, use_quick_model
- `[backtest]` — (placeholder for future config)
- `[live_trading]` — enabled, exchange, order_type, max_order_usd, confirm_orders

### Config Properties Added to ConfigProtocol
- LIVE_TRADING_ENABLED, LIVE_EXCHANGE, LIVE_ORDER_TYPE, LIVE_MAX_ORDER_USD, LIVE_CONFIRM_ORDERS
- BINANCE_API_KEY, BINANCE_API_SECRET (from keys.env)

### CompositionRoot Changes (start.py)
- `_create_order_executor()` — validates API keys → creates authenticated CCXT exchange → LiveExecutor or DemoExecutor
- order_executor popped from dependencies before CryptoTradingBot creation (bot doesn't accept it)
- LiveExecutor.close() registered via shutdown_manager for graceful disconnect

### TradingStrategy Changes
- Constructor accepts `order_executor: Optional[OrderExecutorProtocol]`
- `_open_new_position()`: confirmation guard → open_order() → revert on failure → update fee/price from fill
- `close_position()`: confirmation guard (signal-based only, not SL/TP) → close_order() → keep position on failure
- `_confirm_live_order()`: keyboard Y/N confirmation for live orders

### ModelManager / ModelManagerProtocol
- Added `send_quick_prompt()` using fallback model — used by DebateService

## Gotchas & Lessons Learned
- `retry_async` parameter is `initial_delay`, NOT `base_delay` (caused import-time TypeError)
- CCXT async exchanges need explicit `await exchange.close()` on shutdown
- `**dependencies` unpacking to CryptoTradingBot — must pop unknown keys first (order_executor, dashboard_server)
- DebateService uses inline `asyncio` import was a bug — now imported at top level
- BacktestEngine should create extractor+risk_manager once before loop, not per candle

## Test Status
- 150 tests passing (pytest, all in tests/)
- Test files: brain_integration, context_builder, dashboard_brain_router, dashboard_performance_router, dashboard_server_cache, indicator_classifier, template_manager, vector_memory

## Trading Module Files
```
src/trading/
├── __init__.py              # Exports all public classes including OrderExecutorProtocol, DemoExecutor, LiveExecutor, OrderResult
├── backtest_engine.py       # BacktestEngine — historical candle replay
├── brain.py                 # TradingBrainService
├── data_models.py           # Position, TradeDecision, Rating, DebateResult, RiskAssessment, etc.
├── debate_service.py        # DebateService — bull/bear dialectic
├── memory.py                # TradingMemoryService
├── order_executor.py        # OrderExecutorProtocol, DemoExecutor, LiveExecutor, OrderResult
├── position_extractor.py    # PositionExtractor — 7-tuple extraction
├── statistics.py            # TradingStatisticsService
├── statistics_calculator.py # StatisticsCalculator
├── trading_strategy.py      # TradingStrategy — main strategy with order execution
├── vector_memory.py         # VectorMemoryService (base)
├── vector_memory_analytics.py
├── vector_memory_context.py
└── vector_memory_rules.py
```
