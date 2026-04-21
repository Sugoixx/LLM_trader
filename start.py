"""
Crypto Trading Bot - Entry Point
Automated trading with AI-powered decisions.
"""
# --- Standard Library ---
import asyncio
import atexit
import os
import sys
import time
import warnings
from pathlib import Path
from typing import Optional

# --- Third-party ---
import aiohttp
import torch  # noqa: F401  # needed to initialize PyTorch before sentence-transformers
import chromadb

# --- Local ---
from src.config.loader import config
from src.app import CryptoTradingBot
from sentence_transformers import SentenceTransformer
from src.logger.logger import Logger
from src.utils.graceful_shutdown_manager import GracefulShutdownManager
from src.platforms.alternative_me import AlternativeMeAPI
from src.platforms.defillama import DefiLlamaClient
from src.platforms.coingecko import CoinGeckoAPI
from src.platforms.cryptocompare.news_client import CryptoCompareNewsClient
from src.platforms.cryptocompare.market_api import CryptoCompareMarketAPI
from src.platforms.cryptocompare.categories_api import CryptoCompareCategoriesAPI
from src.platforms.cryptocompare.data_processor import CryptoCompareDataProcessor
from src.platforms.exchange_manager import ExchangeManager
from src.platforms.mt5_manager import MT5ExchangeManager
from src.platforms.commodities_news import CommoditiesNewsClient
from src.analyzer.analysis_engine import AnalysisEngine
from src.rag import RagEngine
from src.utils.token_counter import TokenCounter, CostStorage, ModelPricing
from src.utils.format_utils import FormatUtils
from src.managers.model_manager import ModelManager, ProviderClients, ProviderOrchestrator
from src.factories import ProviderFactory
from src.managers.persistence_manager import PersistenceManager
from src.managers.risk_manager import RiskManager
from src.trading import (
    TradingStrategy, TradingBrainService,
    TradingStatisticsService, TradingMemoryService, PositionExtractor,
    DebateService,
)
from src.trading.vector_memory import VectorMemoryService
from src.dashboard.server import DashboardServer
from src.notifiers import DiscordNotifier, ConsoleNotifier
from src.utils.keyboard_handler import KeyboardHandler
from src.parsing.unified_parser import UnifiedParser
from src.factories import TechnicalIndicatorsFactory, DataFetcherFactory
from src.rag.article_processor import ArticleProcessor
from src.rag.collision_resolver import CategoryCollisionResolver
from src.analyzer.pattern_engine import PatternEngine
from src.analyzer.pattern_engine.indicator_patterns import IndicatorPatternEngine
from src.analyzer.formatters import (
    MarketOverviewFormatter,
    LongTermFormatter,
    MarketFormatter,
    MarketPeriodFormatter
)
from src.rag import (
    RagFileHandler, NewsManager, MarketDataManager,
    IndexManager, ContextBuilder, CategoryFetcher,
    CategoryProcessor, TickerManager, NewsCategoryAnalyzer
)
from src.rag.market_components import (
    MarketDataFetcher,
    MarketDataProcessor,
    MarketDataCache,
    MarketOverviewBuilder
)
from src.analyzer import (
    TechnicalCalculator, PatternAnalyzer, MarketDataCollector,
    MarketMetricsCalculator, AnalysisResultProcessor,
    TechnicalFormatter
)
from src.analyzer.prompts import PromptBuilder
from src.analyzer.prompts.template_manager import TemplateManager
from src.analyzer.prompts.context_builder import ContextBuilder as AnalyzerContextBuilder
from src.analyzer.pattern_engine import ChartGenerator
from src.utils.timeframe_validator import TimeframeValidator
from src.trading.algo_strategies import StrategySignalLayer

# Suppress known deprecation warnings from third-party libraries at runtime
warnings.filterwarnings("ignore", category=SyntaxWarning, module="docopt")
warnings.filterwarnings("ignore", category=DeprecationWarning, module="discord")
warnings.filterwarnings("ignore", category=DeprecationWarning, module="google.genai")


def _get_best_device() -> str:
    """Auto-detect best available hardware accelerator for embeddings.

    Priority: CUDA (NVIDIA) > MPS (Apple Silicon) > CPU.
    """
    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"

try:
    from PyQt6.QtWidgets import QApplication, QMessageBox
    from PyQt6.QtCore import Qt
    PYQT_AVAILABLE = True
except ImportError:
    PYQT_AVAILABLE = False

class SingleInstanceLock:
    """Manages a single instance lock file to prevent multiple application instances."""

    def __init__(self, app_name: str = ".llm_trader.lock"):
        self.lock_file_path = Path.home() / app_name
        self._lock_handle: Optional[int] = None
    def acquire(self) -> bool:
        """Attempt to acquire the lock. Returns True if successful."""
        try:
            self._lock_handle = os.open(str(self.lock_file_path), os.O_CREAT | os.O_RDWR)

            if sys.platform == "win32":
                import msvcrt
                try:
                    msvcrt.locking(self._lock_handle, msvcrt.LK_NBLCK, 1)
                except OSError:
                    return False
            else:
                import fcntl  # pylint: disable=import-error
                try:
                    fcntl.flock(self._lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return False

            atexit.register(self.release)
            return True

        except Exception as e:
            print(f"Warning: Could not create lock file: {e}")
            return True

    def release(self) -> None:
        """Release the lock and cleanup."""
        if self._lock_handle is not None:
            try:
                if sys.platform == "win32":
                    import msvcrt
                    msvcrt.locking(self._lock_handle, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl  # pylint: disable=import-error
                    fcntl.flock(self._lock_handle, fcntl.LOCK_UN)
                os.close(self._lock_handle)
            except Exception:
                pass
            self._lock_handle = None

            try:
                self.lock_file_path.unlink(missing_ok=True)
            except Exception:
                pass


class CompositionRoot:
    """Composition Root for the trading bot application.

    Responsible for building and wiring all dependencies following the
    Dependency Injection pattern before injecting them into CryptoTradingBot.
    """

    def __init__(self):
        self.config = config
        self.logger = Logger(logger_name="Bot", logger_debug=config.LOGGER_DEBUG)
        self.logger.install_crash_handler()
        self.loop = None
        self.shutdown_manager = None

    async def build_dependencies(self) -> dict:
        """Build all dependencies for the trading bot via segmented provisions."""
        start_time = time.perf_counter()
        self.logger.info("Initializing Crypto Trading Bot...")

        self._init_directories()
        infra = await self._provision_infrastructure()
        utils = self._provision_utilities()
        apis = await self._provision_platforms(infra, utils)
        rag = await self._provision_rag_layer(infra, apis, utils)
        models = self._provision_model_layer(utils)
        analyzer = await self._provision_analyzer_layer(infra, apis, utils, rag, models)
        trading = self._provision_trading_layer(utils, models, infra)
        notifiers = await self._provision_notifiers(utils)

        end_time = time.perf_counter()
        init_duration = end_time - start_time
        self.logger.info("All dependencies initialized successfully in %.2f seconds", init_duration)

        # Combine everything for the bot and dashboard
        deps = {
            'exchange_manager': infra['exchange_manager'],
            'market_analyzer': analyzer['engine'],
            'trading_strategy': trading['strategy'],
            'discord_notifier': notifiers['notifier'],
            'discord_task': notifiers['task'],
            'keyboard_handler': infra['keyboard_handler'],
            'rag_engine': rag,
            'coingecko_api': apis['coingecko'],
            'news_client': apis['news'],
            'market_api': apis['market'],
            'categories_api': apis['categories'],
            'alternative_me_api': apis['alternative_me'],
            'cryptocompare_session': infra['session'],
            'persistence': trading['persistence'],
            'model_manager': models['manager'],
            'brain_service': trading['brain_service'],
            'statistics_service': trading['statistics_service'],
            'memory_service': trading['memory_service'],
            'order_executor': trading.get('order_executor'),
        }

        # Always instantiate DashboardServer so the 'd' keyboard toggle can start/stop it at runtime.
        # The server socket is NOT opened until start() is called, so this is safe even when disabled.
        dashboard_server = DashboardServer(
            brain_service=trading['brain_service'],
            vector_memory=trading['brain_service'].vector_memory if trading['brain_service'] else None,
            analysis_engine=analyzer['engine'],
            config=self.config,
            logger=self.logger,
            unified_parser=utils['parser'],
            persistence=trading['persistence'],
            exchange_manager=infra['exchange_manager'],
            host=self.config.DASHBOARD_HOST,
            port=self.config.DASHBOARD_PORT
        )

        deps['dashboard_server'] = dashboard_server
        deps['dashboard_state'] = dashboard_server.dashboard_state

        # Inject dashboard_state into trading strategy for auto-trade toggle
        trading['strategy'].dashboard_state = dashboard_server.dashboard_state

        # Inject trading_strategy into dashboard for manual BUY/SELL/STOP buttons
        dashboard_server.set_trading_strategy(trading['strategy'])

        return deps

    def _init_directories(self):
        """Ensure all required directories exist."""
        data_dir = self.config.DATA_DIR
        os.makedirs(data_dir, exist_ok=True)
        os.makedirs(os.path.join(data_dir, "news_cache"), exist_ok=True)
        os.makedirs(os.path.join(data_dir, "trading"), exist_ok=True)
        os.makedirs(os.path.join(data_dir, "charts"), exist_ok=True)

        # Calculate symbol-specific brain dir
        safe_symbol = self.config.CRYPTO_PAIR.replace("/", "_").replace("-", "_")
        brain_dir = os.path.join(data_dir, "trading", f"brain_{safe_symbol}_{self.config.TIMEFRAME}")
        os.makedirs(brain_dir, exist_ok=True)

    async def _provision_infrastructure(self) -> dict:
        """Provision base infrastructure components."""
        # Select exchange manager based on MT5 config
        if self.config.MT5_ENABLED:
            self.logger.info("MT5 mode enabled — using MetaTrader 5 exchange manager")
            exchange_manager = MT5ExchangeManager(logger=self.logger, config=self.config)
        else:
            exchange_manager = ExchangeManager(logger=self.logger, config=self.config)
        await exchange_manager.initialize()

        session = aiohttp.ClientSession()
        keyboard_handler = KeyboardHandler(logger=self.logger)

        return {
            'exchange_manager': exchange_manager,
            'session': session,
            'keyboard_handler': keyboard_handler
        }

    def _provision_utilities(self) -> dict:
        """Provision utility singletons."""
        format_utils = FormatUtils()
        parser = UnifiedParser(self.logger, format_utils=format_utils)
        token_counter = TokenCounter()
        # SentenceSplitter removed (simplified NLP)
        ti_factory = TechnicalIndicatorsFactory()
        timeframe_validator = TimeframeValidator()
        data_fetcher_factory = DataFetcherFactory(self.logger)
        collision_resolver = CategoryCollisionResolver()

        return {
            'format_utils': format_utils,
            'parser': parser,
            'token_counter': token_counter,
            # 'sentence_splitter': sentence_splitter, # Removed
            'ti_factory': ti_factory,
            'timeframe_validator': timeframe_validator,
            'data_fetcher_factory': data_fetcher_factory,
            'collision_resolver': collision_resolver
        }

    async def _provision_platforms(self, infra: dict, utils: dict) -> dict:
        """Provision external API clients."""
        from aiohttp_client_cache import SQLiteBackend
        coingecko_backend = SQLiteBackend(cache_name='cache/coingecko_cache.db', expire_after=-1)

        coingecko = CoinGeckoAPI(
            logger=self.logger,
            cache_backend=coingecko_backend,
            cache_dir='data/market_data',
            api_key=self.config.COINGECKO_API_KEY,
            update_interval_hours=24,
            global_api_url=self.config.RAG_COINGECKO_GLOBAL_API_URL
        )
        await coingecko.initialize()

        news_client = CryptoCompareNewsClient(self.logger, self.config)

        cc_data_processor = CryptoCompareDataProcessor(self.logger)
        categories = CryptoCompareCategoriesAPI(
            logger=self.logger, config=self.config, data_processor=cc_data_processor,
            collision_resolver=utils['collision_resolver'],
            data_dir='data', categories_update_interval_hours=self.config.RAG_CATEGORIES_UPDATE_INTERVAL_HOURS
        )
        await categories.initialize()

        defillama = DefiLlamaClient(
            logger=self.logger, session=infra['session'], cache_dir='cache',
            update_interval_hours=self.config.RAG_DEFILLAMA_UPDATE_INTERVAL_HOURS
        )

        alternative_me = AlternativeMeAPI(logger=self.logger)
        await alternative_me.initialize()

        return {
            'coingecko': coingecko,
            'news': news_client,
            'market': CryptoCompareMarketAPI(logger=self.logger, config=self.config),
            'categories': categories,
            'defillama': defillama,
            'alternative_me': alternative_me
        }

    async def _provision_rag_layer(self, infra: dict, apis: dict, utils: dict) -> RagEngine:
        """Provision the RAG (Retrieval Augmented Generation) engine."""
        article_processor = ArticleProcessor(
            logger=self.logger, unified_parser=utils['parser'],
            format_utils=utils['format_utils']
        )

        file_handler = RagFileHandler(logger=self.logger, config=self.config, unified_parser=utils['parser'])
        news_manager = NewsManager(
            logger=self.logger, file_handler=file_handler, news_client=apis['news'],
            categories_api=apis['categories'], session=infra['session'], article_processor=article_processor
        )

        marker_fetcher = MarketDataFetcher(
            self.logger, apis['coingecko'], infra['exchange_manager'], apis['market'], apis['defillama']
        )
        market_processor = MarketDataProcessor(self.logger, utils['parser'])
        data_manager = MarketDataManager(
            self.logger, file_handler, apis['coingecko'], apis['market'],
            infra['exchange_manager'], unified_parser=utils['parser'],
            fetcher=marker_fetcher, processor=market_processor,
            cache=MarketDataCache(self.logger, file_handler),
            overview_builder=MarketOverviewBuilder(self.logger, market_processor)
        )

        category_processor = CategoryProcessor(self.logger, utils['collision_resolver'], file_handler)

        # Create commodities news client for non-crypto assets (oil, gold, forex, etc.)
        commodities_client = CommoditiesNewsClient(self.logger, self.config)

        engine = RagEngine(
            logger=self.logger, token_counter=utils['token_counter'], config=self.config,
            coingecko_api=apis['coingecko'], exchange_manager=infra['exchange_manager'],
            file_handler=file_handler, news_manager=news_manager, market_data_manager=data_manager,
            index_manager=IndexManager(self.logger, article_processor),
            category_fetcher=CategoryFetcher(self.logger, apis['categories']),
            category_processor=category_processor,
            ticker_manager=TickerManager(self.logger, file_handler, infra['exchange_manager']),
            news_category_analyzer=NewsCategoryAnalyzer(self.logger, category_processor, utils['parser']),
            context_builder=ContextBuilder(self.logger, utils['token_counter'], self.config, article_processor),
            commodities_news_client=commodities_client,
        )
        await engine.initialize()
        return engine

    def _provision_model_layer(self, utils: dict) -> dict:
        """Provision AI model managers and providers."""
        provider_factory = ProviderFactory(self.logger, self.config)
        provider_clients = ProviderClients.from_factory_dict(provider_factory.create_all_clients())
        orchestrator = ProviderOrchestrator(self.logger, self.config, provider_clients)

        manager = ModelManager(
            logger=self.logger, config=self.config, unified_parser=utils['parser'],
            token_counter=utils['token_counter'], cost_storage=CostStorage(),
            model_pricing=ModelPricing(), orchestrator=orchestrator, provider_clients=provider_clients
        )

        return {'manager': manager}

    async def _provision_analyzer_layer(
        self, infra: dict, apis: dict, utils: dict, rag: RagEngine, models: dict
    ) -> dict:
        """Provision the market analysis engine."""
        overview_fmt = MarketOverviewFormatter(self.logger, utils['format_utils'])
        long_term_fmt = LongTermFormatter(self.logger, utils['format_utils'])
        period_fmt = MarketPeriodFormatter(self.logger, utils['format_utils'])

        market_fmt = MarketFormatter(
            self.logger, utils['format_utils'], self.config, utils['token_counter'],
            overview_fmt, period_fmt, long_term_fmt
        )

        tech_calc = TechnicalCalculator(self.logger, utils['format_utils'], utils['ti_factory'])
        pattern_analyzer = PatternAnalyzer(
            pattern_engine=PatternEngine(lookback=5, lookahead=5),
            indicator_pattern_engine=IndicatorPatternEngine(),
            logger=self.logger
        )
        try:
            pattern_analyzer.warmup()
        except Exception as warmup_error:
            self.logger.warning("Pattern analyzer warm-up could not run: %s", warmup_error)

        ctx_builder = AnalyzerContextBuilder(
            self.config.TIMEFRAME, self.logger, utils['format_utils'],
            market_fmt, period_fmt, long_term_fmt, utils['timeframe_validator']
        )
        
        prompt_builder = PromptBuilder(
            self.config.TIMEFRAME, self.logger, tech_calc, self.config, utils['format_utils'],
            overview_fmt, long_term_fmt, TechnicalFormatter(tech_calc, self.logger, utils['format_utils']),
            market_fmt, utils['timeframe_validator'],
            TemplateManager(self.config, self.logger, utils['timeframe_validator']), ctx_builder
        )
        
        engine = AnalysisEngine(
            self.logger, rag, apis['coingecko'], models['manager'], apis['alternative_me'],
            apis['market'], self.config, tech_calc, pattern_analyzer, prompt_builder,
            MarketDataCollector(self.logger, rag, apis['alternative_me'], session=infra['session']),
            MarketMetricsCalculator(self.logger),
            AnalysisResultProcessor(models['manager'], self.logger, utils['parser']),
            ChartGenerator(
                self.logger, self.config, formatter=utils['format_utils'].fmt, format_utils=utils['format_utils']
            ),
            data_fetcher_factory=utils['data_fetcher_factory'],
            signal_layer=StrategySignalLayer(self.logger),
        )
        
        return {'engine': engine}

    def _provision_trading_layer(self, utils: dict, models: dict = None, infra: dict = None) -> dict:
        """Provision trading strategy and memory services."""
        # Route persistence through the profile's DATA_DIR so each instance
        # (crypto/forex/oil) keeps its own positions.json, trade_history.json,
        # statistics.json, etc. — otherwise instances cross-contaminate state.
        trading_data_dir = os.path.join(self.config.DATA_DIR, "trading")
        persistence = PersistenceManager(self.logger, data_dir=trading_data_dir)
        risk_manager = RiskManager(self.logger, self.config)

        # Calculate specialized brain path
        safe_symbol = self.config.CRYPTO_PAIR.replace("/", "_").replace("-", "_")
        brain_path = os.path.join(self.config.DATA_DIR, "trading", f"brain_{safe_symbol}_{self.config.TIMEFRAME}")

        # Create symbol-specific chroma client
        chroma_client = chromadb.PersistentClient(path=brain_path)

        # Auto-detect best hardware accelerator; log for observability
        embed_device = _get_best_device()
        self.logger.info("Embedding device: %s", embed_device)

        # Set HF_TOKEN for authenticated downloads (avoids rate-limit warnings)
        hf_token = os.environ.get("HF_TOKEN") or getattr(self.config, "HF_TOKEN", None)
        if hf_token:
            os.environ["HF_TOKEN"] = hf_token
            self.logger.info("HF_TOKEN set for HuggingFace Hub authentication")
        else:
            self.logger.warning("HF_TOKEN not set — HuggingFace requests will be unauthenticated")

        embedding_model = SentenceTransformer("BAAI/bge-small-en-v1.5", device=embed_device)

        # Inject chroma_client into VectorMemoryService
        vector_memory = VectorMemoryService(self.logger, chroma_client, embedding_model=embedding_model)
        
        brain_service = TradingBrainService(
            self.logger, persistence, vector_memory
        )
        
        memory_service = TradingMemoryService(self.logger, persistence, max_memory=10, vector_memory=vector_memory)
        statistics_service = TradingStatisticsService(self.logger, persistence)

        # Create DebateService if enabled
        debate_service = None
        if models and self.config.DEBATE_ENABLED:
            debate_service = DebateService(
                logger=self.logger,
                model_manager=models['manager'],
                config=self.config,
            )
            self.logger.info("Debate service enabled (quick_model=%s)", self.config.DEBATE_USE_QUICK_MODEL)
        
        from src.factories.position_factory import PositionFactory

        # Create Order Executor (live or demo)
        order_executor = self._create_order_executor(infra)

        strategy = TradingStrategy(
            self.logger, persistence, brain_service, statistics_service, memory_service,
            risk_manager, self.config, PositionExtractor(self.logger, utils['parser']),
            PositionFactory(self.logger), debate_service=debate_service,
            order_executor=order_executor,
        )
        
        return {
            'strategy': strategy,
            'persistence': persistence,
            'brain_service': brain_service,
            'memory_service': memory_service,
            'statistics_service': statistics_service,
            'debate_service': debate_service,
            'order_executor': order_executor,
        }

    def _create_order_executor(self, infra: dict = None):
        """Create the appropriate order executor based on config.

        Returns DemoExecutor for paper trading, LiveExecutor/MT5OrderExecutor for real trading.
        """
        from src.trading.order_executor import DemoExecutor, LiveExecutor

        # MT5: always use MT5OrderExecutor when MT5 is enabled
        # The MT5 demo account IS the paper trading layer — no need for LLM_Trader's DemoExecutor
        if self.config.MT5_ENABLED:
            from src.trading.mt5_order_executor import MT5OrderExecutor
            exchange_manager = infra.get('exchange_manager') if infra else None
            if not exchange_manager or not hasattr(exchange_manager, 'exchanges'):
                self.logger.error(
                    "MT5_ENABLED=true but MT5ExchangeManager not available. "
                    "Falling back to DEMO mode."
                )
                return DemoExecutor(self.logger, self.config)

            mt5_exchange = exchange_manager.exchanges.get('mt5')
            if not mt5_exchange:
                self.logger.error("MT5 exchange not found in manager. Falling back to DEMO.")
                return DemoExecutor(self.logger, self.config)

            mode_label = "LIVE" if self.config.LIVE_TRADING_ENABLED else "DEMO"
            self.logger.warning("=" * 60)
            self.logger.warning("  MT5 TRADING MODE: %s — Orders go to MT5 %s account", mode_label, mode_label.lower())
            self.logger.warning("  Broker: %s | Max order: $%.0f", self.config.MT5_SERVER, self.config.LIVE_MAX_ORDER_USD)
            self.logger.warning("=" * 60)
            return MT5OrderExecutor(self.logger, self.config, mt5_exchange)

        # CCXT path: require explicit live_trading.enabled
        if not self.config.LIVE_TRADING_ENABLED:
            self.logger.info("Trading mode: DEMO (paper trading)")
            return DemoExecutor(self.logger, self.config)

        # CCXT live trading
        api_key = self.config.BINANCE_API_KEY
        api_secret = self.config.BINANCE_API_SECRET
        if not api_key or not api_secret:
            self.logger.error(
                "LIVE_TRADING_ENABLED=true but BINANCE_API_KEY/SECRET not set in keys.env. "
                "Falling back to DEMO mode."
            )
            return DemoExecutor(self.logger, self.config)

        exchange_id = self.config.LIVE_EXCHANGE
        self.logger.warning("=" * 60)
        self.logger.warning("  LIVE TRADING MODE — REAL MONEY AT RISK")
        self.logger.warning("  Exchange: %s | Max order: $%.0f", exchange_id, self.config.LIVE_MAX_ORDER_USD)
        self.logger.warning("=" * 60)

        # Create authenticated CCXT exchange instance
        import ccxt.async_support as ccxt_async
        try:
            exchange_class = ccxt_async.__dict__[exchange_id]
        except KeyError:
            self.logger.error("Exchange '%s' not found in CCXT. Falling back to DEMO.", exchange_id)
            return DemoExecutor(self.logger, self.config)

        exchange = exchange_class({
            'apiKey': api_key,
            'secret': api_secret,
            'enableRateLimit': True,
            'options': {'defaultType': 'spot'},
        })

        # Apply testnet/sandbox mode on the order-execution exchange only
        # (ExchangeManager keeps mainnet for full OHLCV history)
        if self.config.LIVE_TESTNET and hasattr(exchange, 'set_sandbox_mode'):
            try:
                exchange.set_sandbox_mode(True)
                self.logger.info("[LiveExecutor] %s TESTNET sandbox mode enabled", exchange_id)
            except Exception as e:
                self.logger.warning("[LiveExecutor] set_sandbox_mode failed: %s", e)

        return LiveExecutor(self.logger, self.config, exchange)

    async def _create_execution_engine(self, symbol, order_executor, trading_strategy, infra=None):
        """Create and configure the Layer 2 Execution Engine.

        Creates a ccxt.pro WebSocket exchange (or MT5 polling stream) for
        real-time price streaming and wires it into the execution engine.
        """
        from src.execution import ExecutionEngine, SignalBus, PriceStream, PositionMonitor
        from src.dashboard.dashboard_state import dashboard_state

        signal_bus = SignalBus()

        # MT5 mode — use polling-based price stream instead of WebSocket
        if self.config.MT5_ENABLED:
            from src.execution.mt5_price_stream import MT5PriceStream
            exchange_manager = infra.get('exchange_manager') if infra else None
            mt5_exchange = exchange_manager.exchanges.get('mt5') if exchange_manager else None
            if not mt5_exchange:
                self.logger.error("[Engine] MT5 exchange not available. Layer 2 disabled.")
                return None

            price_stream = MT5PriceStream(self.logger, mt5_exchange, symbol, poll_interval=2.0)
            engine_label = "MT5 polling"
        else:
            # CCXT WebSocket mode
            exchange_id = self.config.LIVE_EXCHANGE if self.config.LIVE_TRADING_ENABLED else "binance"
            try:
                import ccxt.pro as ccxt_pro
                exchange_class = getattr(ccxt_pro, exchange_id, None)
                if not exchange_class:
                    self.logger.error("[Engine] Exchange '%s' not found in ccxt.pro. Layer 2 disabled.", exchange_id)
                    return None

                ws_exchange_config = {'enableRateLimit': True, 'options': {'defaultType': 'spot'}}
                if self.config.LIVE_TRADING_ENABLED:
                    api_key = self.config.BINANCE_API_KEY
                    api_secret = self.config.BINANCE_API_SECRET
                    if api_key and api_secret:
                        ws_exchange_config['apiKey'] = api_key
                        ws_exchange_config['secret'] = api_secret

                ws_exchange = exchange_class(ws_exchange_config)

                # Switch WebSocket to testnet (Binance: testnet.binance.vision)
                if self.config.LIVE_TRADING_ENABLED and getattr(self.config, 'LIVE_TESTNET', False):
                    if hasattr(ws_exchange, 'set_sandbox_mode'):
                        try:
                            ws_exchange.set_sandbox_mode(True)
                            self.logger.info("[Engine] %s WebSocket TESTNET mode enabled", exchange_id)
                        except Exception as e:
                            self.logger.warning("[Engine] WS set_sandbox_mode failed: %s", e)
            except Exception as e:
                self.logger.error("[Engine] Failed to create WebSocket exchange: %s. Layer 2 disabled.", e)
                return None

            price_stream = PriceStream(self.logger, ws_exchange, symbol)
            engine_label = f"WebSocket ({exchange_id})"

        # Use the order executor from Layer 1 (shared)
        from src.trading.order_executor import DemoExecutor
        executor = order_executor or DemoExecutor(self.logger, self.config)

        position_monitor = PositionMonitor(self.logger, self.config, executor)
        position_monitor.set_dashboard_state(dashboard_state)

        engine = ExecutionEngine(
            logger=self.logger,
            config=self.config,
            signal_bus=signal_bus,
            price_stream=price_stream,
            position_monitor=position_monitor,
            trading_strategy=trading_strategy,
        )

        self.logger.info(
            "[Engine] Layer 2 configured: symbol=%s, stream=%s, trailing=%s, partial=%s",
            symbol, engine_label,
            self.config.EXECUTION_TRAILING_ENABLED,
            self.config.EXECUTION_PARTIAL_ENABLED,
        )

        return engine

    async def _provision_notifiers(self, utils: dict) -> dict:
        """Provision notification services."""
        notifier = None
        task = None
        
        if self.config.DISCORD_BOT_ENABLED and hasattr(self.config, 'BOT_TOKEN_DISCORD') and self.config.BOT_TOKEN_DISCORD:
            try:
                import discord
                from src.notifiers.filehandler_components import (
                    TrackingPersistence, MessageTracker, CleanupScheduler, MessageDeleter
                )
                from src.notifiers.filehandler import DiscordFileHandler
                
                intents = discord.Intents.default()
                intents.message_content = False
                intents.reactions = False
                intents.typing = False
                intents.presences = False
                
                bot = discord.Client(intents=intents)
                
                persistence = TrackingPersistence("data/tracked_messages.json", self.logger)
                tracker = MessageTracker(persistence, self.logger, self.config)
                scheduler = CleanupScheduler(7200, self.logger)
                deleter = MessageDeleter(bot, self.logger)
                
                file_handler = DiscordFileHandler(
                    bot=bot,
                    logger=self.logger,
                    config=self.config,
                    persistence=persistence,
                    tracker=tracker,
                    scheduler=scheduler,
                    deleter=deleter
                )
                
                notifier = DiscordNotifier(
                    self.logger, self.config, utils['parser'], 
                    utils['format_utils'], bot, file_handler
                )
                
                task = asyncio.create_task(notifier.start())
                await notifier.wait_until_ready()
            except Exception as e:
                self.logger.warning("Discord initialization failed: %s. Falling back to console output.", e)
                notifier = ConsoleNotifier(self.logger, self.config, utils['parser'], utils['format_utils'])
        else:
            notifier = ConsoleNotifier(self.logger, self.config, utils['parser'], utils['format_utils'])
            
        return {'notifier': notifier, 'task': task}
    
    async def run_async(self):
        """Async entry point for the application."""
        def _asyncio_exception_handler(loop, context):
            exc = context.get("exception")
            msg = context.get("message", "Unknown asyncio error")
            if exc is not None:
                if isinstance(exc, KeyboardInterrupt):
                    self.logger.debug("Asyncio task KeyboardInterrupt during shutdown: %s", msg)
                    return
                self.logger.error("Asyncio unhandled exception: %s", msg, exc_info=exc)
            else:
                self.logger.error("Asyncio error: %s", msg)

        if self.loop:
            self.loop.set_exception_handler(_asyncio_exception_handler)

        dependencies = await self.build_dependencies()

        # Extract dashboard_server before passing to bot (bot doesn't accept it)
        dashboard_server = dependencies.pop('dashboard_server', None)
        order_executor = dependencies.pop('order_executor', None)
        # Keep strategy reference for execution engine wiring
        trading_strategy = dependencies['trading_strategy']

        bot = CryptoTradingBot(
            logger=self.logger,
            config=self.config,
            shutdown_manager=self.shutdown_manager,
            **dependencies
        )

        try:
            await bot.initialize()

            # Now that the bot exists, let the dashboard's settings router
            # reach it (for symbol-switch endpoint and friends).
            if dashboard_server and hasattr(dashboard_server, "set_bot"):
                dashboard_server.set_bot(bot)

            # Register live executor for graceful exchange disconnect
            if order_executor and hasattr(order_executor, 'close'):
                self.shutdown_manager.register_shutdown_callback(order_executor.close)

            symbol = self.config.CRYPTO_PAIR
            timeframe = self.config.TIMEFRAME

            # --- Layer 2: Execution Engine (real-time monitoring) ---
            execution_engine = None
            async def _recreate_execution_engine(active_symbol: str):
                """Close any existing execution engine and recreate it for the
                given symbol. Returns the new engine (or None if disabled)."""
                nonlocal execution_engine
                if execution_engine is not None:
                    try:
                        await execution_engine.close()
                    except Exception as close_err:
                        self.logger.warning("[Engine] close() raised during re-creation: %s", close_err)
                    execution_engine = None

                if not self.config.EXECUTION_ENGINE_ENABLED:
                    return None
                engine_infra = {'exchange_manager': dependencies.get('exchange_manager')}
                engine = await self._create_execution_engine(
                    active_symbol, order_executor, trading_strategy, infra=engine_infra
                )
                if engine:
                    await engine.start()
                    self.shutdown_manager.register_shutdown_callback(engine.close)
                    bot.signal_bus = engine.signal_bus
                execution_engine = engine
                return engine

            await _recreate_execution_engine(symbol)

            # Track whether dashboard is currently running
            dashboard_running = False

            async def _toggle_dashboard():
                nonlocal dashboard_running
                if not dashboard_server:
                    return
                if dashboard_running:
                    self.logger.info("Dashboard: stopping (kill switch)...")
                    await dashboard_server.stop()
                    dashboard_running = False
                    self.logger.info("Dashboard stopped. Press 'd' to restart.")
                else:
                    self.logger.info("Dashboard: starting...")
                    await dashboard_server.start()
                    dashboard_running = True
                    self.logger.info("Dashboard live at http://localhost:%s", self.config.DASHBOARD_PORT)

            bot.keyboard_handler.register_command('d', _toggle_dashboard, "Toggle dashboard on/off")

            self.logger.info("Keyboard commands: 'a' = force analysis, 'd' = toggle dashboard, 'h' = help, 'q' = quit")

            # Auto-start dashboard if enabled in config (fire-and-forget task)
            if dashboard_server and self.config.DASHBOARD_ENABLED:
                await dashboard_server.start()
                dashboard_running = True
            elif not self.config.DASHBOARD_ENABLED:
                self.logger.info("Dashboard disabled (config). Press 'd' to start it.")

            # Bot runs independently; dashboard is managed by _toggle_dashboard.
            # Wrap in a loop so an in-process symbol switch (bot._switch_requested)
            # re-launches bot.run() on the new symbol without a full process restart.
            while True:
                await asyncio.create_task(bot.run(symbol, timeframe))

                new_sym = getattr(bot, "_switch_requested", None)
                # Shutdown takes priority over switch
                shutdown_pending = False
                try:
                    shutdown_pending = bool(
                        self.shutdown_manager
                        and getattr(self.shutdown_manager, "_shutting_down", False)
                    )
                except Exception:
                    shutdown_pending = False

                if not new_sym or shutdown_pending:
                    break

                self.logger.warning(
                    "[SWITCH] Bot exited; re-launching on new symbol: %s -> %s",
                    symbol, new_sym,
                )
                bot._switch_requested = None
                symbol = new_sym
                timeframe = self.config.TIMEFRAME  # allow timeframe override via config too
                # Reset runtime flags so bot.run() can restart cleanly
                bot.running = True
                bot._force_analysis.clear()
                # Recreate Layer 2 engine bound to the new symbol
                await _recreate_execution_engine(symbol)

        except asyncio.CancelledError:
            self.logger.info("Trading cancelled, shutting down...")
        finally:
            # Clean up execution engine
            if execution_engine:
                await execution_engine.close()
            # Clean up dashboard server
            if dashboard_server:
                await dashboard_server.stop()
    
    def start(self):
        """Main entry point with clean shutdown delegation."""
        # Use a per-profile lock name so multiple profiles can run in parallel.
        profile_name = os.environ.get("LLM_TRADER_PROFILE", "default")
        single_instance_lock = SingleInstanceLock(app_name=f".llm_trader_{profile_name}.lock")
        
        if not single_instance_lock.acquire():
            if PYQT_AVAILABLE:
                app = QApplication.instance()
                if app is None:
                    app = QApplication(sys.argv)
                    QApplication.setHighDpiScaleFactorRoundingPolicy(
                        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
                    )
                QMessageBox.critical(
                    None,
                    "Crypto Trading Bot",
                    "Another instance of Crypto Trading Bot is already running.",
                    QMessageBox.StandardButton.Ok
                )
            else:
                print("Another instance of Crypto Trading Bot is already running.")
            sys.exit(1)
        
        if sys.platform == 'win32':
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        
        self.shutdown_manager = GracefulShutdownManager(
            self.loop,
            logger=self.logger,
            confirmation_callback=GracefulShutdownManager.show_exit_confirmation
        )
        self.shutdown_manager.setup_signal_handlers()
        
        try:
            self.loop.run_until_complete(self.run_async())
        except KeyboardInterrupt:
            print("\nKeyboardInterrupt received - initiating graceful shutdown...")
            self.loop.run_until_complete(self.shutdown_manager.shutdown_gracefully())
        except Exception:
            self.logger.exception("Unhandled exception in main loop — shutting down")
            self.loop.run_until_complete(self.shutdown_manager.shutdown_gracefully())
        finally:
            self.loop.close()


if __name__ == "__main__":
    # Pre-parse --profile before heavy imports have side effects.
    # The config loader reads sys.argv directly (see _resolve_active_config_path),
    # but we also set the env var so child helpers can read it.
    import argparse
    _pre_parser = argparse.ArgumentParser(add_help=False)
    _pre_parser.add_argument("--profile", type=str, default=None)
    _pre_args, _ = _pre_parser.parse_known_args()
    if _pre_args.profile:
        os.environ["LLM_TRADER_PROFILE"] = _pre_args.profile

    CompositionRoot().start()
