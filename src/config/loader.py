"""
Configuration loader for LLM_Trader v2.
Loads private keys from keys.env and public configuration from config.ini.
"""

import configparser
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict
from dotenv import dotenv_values

# Get the root directory (where keys.env is located) and config directory (where config.ini is located)
ROOT_DIR = Path(__file__).parent.parent.parent.resolve()
CONFIG_DIR = ROOT_DIR / "config"
KEYS_ENV_PATH = ROOT_DIR / "keys.env"
CONFIG_INI_PATH = CONFIG_DIR / "config.ini"

VALID_PROVIDERS = {"local", "googleai", "openrouter", "blockrun", "all"}


def _resolve_active_config_path() -> Path:
    """Resolve which config.ini to load.

    Priority order:
    1. ``--profile <name>`` CLI argument → ``config/profiles/<name>.ini``
    2. ``--profile=<name>`` CLI argument
    3. ``LLM_TRADER_CONFIG`` environment variable (absolute path)
    4. ``LLM_TRADER_PROFILE`` environment variable (profile name)
    5. Default ``config/config.ini``
    """
    # CLI scan (raw sys.argv to stay independent of argparse order)
    argv = sys.argv[1:] if sys.argv else []
    for i, arg in enumerate(argv):
        if arg == "--profile" and i + 1 < len(argv):
            candidate = CONFIG_DIR / "profiles" / f"{argv[i + 1]}.ini"
            if candidate.exists():
                return candidate
        elif arg.startswith("--profile="):
            name = arg.split("=", 1)[1]
            candidate = CONFIG_DIR / "profiles" / f"{name}.ini"
            if candidate.exists():
                return candidate

    env_path = os.environ.get("LLM_TRADER_CONFIG")
    if env_path and Path(env_path).exists():
        return Path(env_path)

    env_profile = os.environ.get("LLM_TRADER_PROFILE")
    if env_profile:
        candidate = CONFIG_DIR / "profiles" / f"{env_profile}.ini"
        if candidate.exists():
            return candidate

    return CONFIG_INI_PATH


ACTIVE_CONFIG_PATH = _resolve_active_config_path()

class Config:
    """Configuration class that loads settings from environment and INI files.

    Implements ConfigProtocol for type safety and dependency injection.
    """

    def __init__(self):
        self._env_vars = {}
        self._config_data = {}
        # Resolved INI path (may differ from the module default when a
        # profile is active, e.g. ``--profile forex``).
        self._config_path: Path = ACTIVE_CONFIG_PATH
        self._load_environment()
        self._load_ini_config()
        self._validate_provider()
        self._build_dynamic_urls()
        self._build_model_configs()

    def _load_environment(self):
        """Load environment variables from keys.env file using python-dotenv."""
        if not KEYS_ENV_PATH.exists():
            raise FileNotFoundError(
                f"Private keys file not found: {KEYS_ENV_PATH}. "
                "Please create keys.env in the root directory with your API keys."
            )

        try:
            # Use dotenv_values to parse the .env file
            env_vars = dotenv_values(KEYS_ENV_PATH)

            # Convert values to appropriate types
            for key, value in env_vars.items():
                if value is not None:
                    # Convert numeric strings to integers
                    if value.isdigit():
                        value = int(value)
                    self._env_vars[key] = value

        except Exception as e:
            raise RuntimeError(f"Error loading environment file {KEYS_ENV_PATH}: {e}") from e

    def _load_ini_config(self):
        """Load configuration from config.ini file."""
        if not self._config_path.exists():
            raise FileNotFoundError(
                f"Configuration file not found: {self._config_path}. "
                "Please create config.ini in the config directory."
            )

        try:
            config = configparser.ConfigParser()
            config.read(self._config_path, encoding='utf-8')

            for section_name in config.sections():
                section_data = {}
                for key, value in config.items(section_name):
                    # Type conversion
                    section_data[key] = self._convert_value(value)
                self._config_data[section_name] = section_data
        except Exception as e:
            raise RuntimeError(f"Error loading configuration file {self._config_path}: {e}") from e

    def persist_config_value(self, section: str, key: str, value) -> None:
        """Write a value back to the INI file and update the in-memory cache.

        Used by the dashboard when the user changes a runtime-persistable
        setting such as the active trading symbol.
        """
        cp = configparser.ConfigParser()
        if self._config_path.exists():
            cp.read(self._config_path, encoding='utf-8')
        if section not in cp:
            cp[section] = {}
        cp[section][key] = str(value)
        with self._config_path.open('w', encoding='utf-8') as f:
            cp.write(f)
        # Refresh in-memory cache so subsequent reads see the new value
        if section not in self._config_data:
            self._config_data[section] = {}
        self._config_data[section][key] = self._convert_value(str(value))

    def _validate_provider(self):
        """Validate that the configured AI provider is supported."""
        provider = self.PROVIDER.lower()
        if provider not in VALID_PROVIDERS:
            # Create a formatted list of valid options
            valid_options = ", ".join(f'"{p}"' for p in sorted(VALID_PROVIDERS))
            error_msg = (
                f"Invalid AI provider '{provider}' in config.ini.\n"
                f"Supported values are: {valid_options}.\n"
                f"Please update the [ai_providers] -> provider setting."
            )
            logging.critical(error_msg)
            raise ValueError(error_msg)

    @staticmethod
    def _convert_value(value: str) -> Any:
        """Convert string values to appropriate Python types."""
        if value.lower() in ('true', 'yes', 'on', '1'):
            return True
        elif value.lower() in ('false', 'no', 'off', '0'):
            return False
        if value.isdigit():
            return int(value)
        try:
            if '.' in value:
                return float(value)
        except ValueError:
            pass
        if ',' in value:
            return [item.strip() for item in value.split(',')]
        return value

    def _build_dynamic_urls(self):
        """Build dynamic URLs that depend on API keys.

        NOTE: API keys are intentionally NOT appended here to prevent leakage
        in logs if these URLs are printed. The key is appended at request time.
        """
        # Base URLs without API keys
        self.RAG_NEWS_API_URL = "https://min-api.cryptocompare.com/data/v2/news/?lang=EN&limit=200&extraParams=LLM_Trader_v2"
        self.RAG_NEWS_FILTER_SOURCES = self.get_config('rag', 'news_filter_sources', True)
        self.RAG_NEWS_ALLOWED_FEEDS = self.get_config('rag', 'news_allowed_feeds', None)
        self.RAG_CATEGORIES_API_URL = "https://min-api.cryptocompare.com/data/news/categories"
        self.RAG_PRICE_API_URL = "https://min-api.cryptocompare.com/data/pricemultifull?fsyms=BTC,ETH,BNB,SOL,XRP&tsyms=USD"

    def _build_model_configs(self):
        """Build model configuration dictionaries as instance variables."""
        default_max_tokens = self.get_config('model_config', 'max_tokens', None)
        if default_max_tokens is None:
            raise RuntimeError("`max_tokens` is required in [model_config] of config.ini")

        self._default_model_config = {
            "temperature": self.get_config('model_config', 'temperature', None),
            "top_p": self.get_config('model_config', 'top_p', None),
            "top_k": self.get_config('model_config', 'top_k', None),
            "freq_penalty": self.get_config('model_config', 'freq_penalty', None),
            "pres_penalty": self.get_config('model_config', 'pres_penalty', None),
            "max_tokens": default_max_tokens
        }

        google_max_tokens = self.get_config('model_config', 'google_max_tokens', None)

        # Only enforce Google config if we are actually using it
        if google_max_tokens is None and self.PROVIDER in ('googleai', 'all'):
            raise RuntimeError("`google_max_tokens` is required in [model_config] of config.ini when using Google models")

        self._google_model_config = {
            "temperature": self.get_config('model_config', 'google_temperature', None),
            "top_p": self.get_config('model_config', 'google_top_p', None),
            "top_k": self.get_config('model_config', 'google_top_k', None),
            "max_tokens": google_max_tokens,
            "thinking_level": self.get_config('model_config', 'google_thinking_level', 'high'),
            "google_code_execution": self.get_config('model_config', 'google_code_execution', False)
        }

    def get_env(self, key: str, default: Any = None) -> Any:
        """Get environment variable."""
        return self._env_vars.get(key, default)

    def get_config(self, section: str, key: str, default: Any = None) -> Any:
        """Get configuration value from INI file."""
        return self._config_data.get(section, {}).get(key, default)

    def get_section(self, section: str) -> Dict[str, Any]:
        """Get entire configuration section."""
        return self._config_data.get(section, {})

    # Environment variables (private keys and sensitive data)
    @property
    def BOT_TOKEN_DISCORD(self):
        return self.get_env('BOT_TOKEN_DISCORD')

    @property
    def GUILD_ID_DISCORD(self):
        return self.get_env('GUILD_ID_DISCORD')

    @property
    def MAIN_CHANNEL_ID(self):
        return self.get_env('MAIN_CHANNEL_ID')

    @property
    def TEMPORARY_CHANNEL_ID_DISCORD(self):
        return self.get_env('TEMPORARY_CHANNEL_ID_DISCORD')

    @property
    def OPENROUTER_API_KEY(self):
        return self.get_env('OPENROUTER_API_KEY')

    @property
    def GOOGLE_STUDIO_API_KEY(self):
        return self.get_env('GOOGLE_STUDIO_API_KEY')

    @property
    def GOOGLE_STUDIO_PAID_API_KEY(self):
        return self.get_env('GOOGLE_STUDIO_PAID_API_KEY')

    @property
    def CRYPTOCOMPARE_API_KEY(self):
        return self.get_env('CRYPTOCOMPARE_API_KEY')

    @property
    def COINGECKO_API_KEY(self):
        return self.get_env('COINGECKO_API_KEY')

    @property
    def BLOCKRUN_WALLET_KEY(self):
        return self.get_env('BLOCKRUN_WALLET_KEY')

    @property
    def ADMIN_USER_IDS(self):
        """Get list of admin user IDs from environment."""
        admin_ids = self.get_env('ADMIN_USER_IDS', '')
        if not admin_ids:
            return []

        # Handle single integer values produced by automatic type conversion
        if isinstance(admin_ids, int):
            return [admin_ids]

        # Handle pre-parsed iterables (lists/tuples) defensively
        if isinstance(admin_ids, (list, tuple)):
            parsed_ids = []
            for raw_id in admin_ids:
                try:
                    parsed_ids.append(int(str(raw_id).strip()))
                except (TypeError, ValueError):
                    logging.warning("Invalid ADMIN_USER_IDS entry '%s' in keys.env. Expected integers.", raw_id)
                    return []
            return parsed_ids

        if isinstance(admin_ids, str):
            try:
                return [int(uid.strip()) for uid in admin_ids.split(',') if uid.strip()]
            except ValueError:
                logging.warning("Invalid ADMIN_USER_IDS format in keys.env. Expected comma-separated integers.")
                return []

        logging.warning(
            "Unsupported ADMIN_USER_IDS type %s encountered. Expected string, int, list, or tuple.",
            type(admin_ids).__name__
        )
        return []

    # AI Provider Configuration
    @property
    def PROVIDER(self):
        return self.get_config('ai_providers', 'provider', 'googleai')

    @property
    def LM_STUDIO_BASE_URL(self):
        return self.get_config('ai_providers', 'lm_studio_base_url', 'http://localhost:1234/v1')

    @property
    def LM_STUDIO_MODEL(self):
        return self.get_config('ai_providers', 'lm_studio_model', 'local-model')

    @property
    def LM_STUDIO_STREAMING(self):
        return self.get_config('ai_providers', 'lm_studio_streaming', True)

    @property
    def OPENROUTER_BASE_URL(self):
        return self.get_config('ai_providers', 'openrouter_base_url', 'https://openrouter.ai/api/v1')

    @property
    def OPENROUTER_BASE_MODEL(self):
        return self.get_config('ai_providers', 'openrouter_base_model', 'google/gemini-2.5-pro')

    @property
    def OPENROUTER_FALLBACK_MODEL(self):
        return self.get_config('ai_providers', 'openrouter_fallback_model', 'deepseek/deepseek-r1:free')

    @property
    def GOOGLE_STUDIO_MODEL(self):
        return self.get_config('ai_providers', 'google_studio_model', 'gemini-2.5-flash')

    @property
    def BLOCKRUN_BASE_URL(self):
        return self.get_config('ai_providers', 'blockrun_base_url', 'https://blockrun.ai/api')

    @property
    def BLOCKRUN_MODEL(self):
        return self.get_config('ai_providers', 'blockrun_model', 'openai/gpt-4o')

    # General Configuration
    @property
    def LOGGER_DEBUG(self):
        return self.get_config('debug', 'logger_debug', False)



    @property
    def CRYPTO_PAIR(self):
        return self.get_config('general', 'crypto_pair', 'BTC/USDT')

    @property
    def DISCORD_BOT_ENABLED(self):
        return self.get_config('general', 'discord_bot', False)

    @property
    def TIMEFRAME(self):
        return self.get_config('general', 'timeframe', '1h')

    @property
    def ANALYSIS_COOLDOWN_MINUTES(self):
        """Minimum minutes between LLM analysis calls (default 0 = every candle)."""
        return int(self.get_config('general', 'analysis_cooldown_minutes', 0))

    @property
    def SKIP_LLM_WHEN_MARKET_CLOSED(self):
        """Skip LLM analysis on candle close when MT5 market is closed."""
        return self.get_config('general', 'skip_llm_when_market_closed', True)

    @property
    def PREOPEN_ANALYSIS_MINUTES(self):
        """Run one pre-open analysis when next market open is within this window."""
        return int(self.get_config('general', 'preopen_analysis_minutes', 20))

    @property
    def MT5_MARKET_STALE_TICK_SECONDS(self):
        """How old a quote can be before treating MT5 market as effectively closed."""
        return int(self.get_config('mt5', 'market_stale_tick_seconds', 1800))

    @property
    def CANDLE_LIMIT(self):
        return self.get_config('general', 'candle_limit', 999)

    @property
    def AI_CHART_CANDLE_LIMIT(self):
        """Configured candle limit to use for AI chart images (must be present in config.ini)."""
        return int(self.get_config('general', 'ai_chart_candle_limit', 200))

    @property
    def INCLUDE_COIN_DESCRIPTION(self) -> bool:
        """Whether to include project description in coin details section."""
        return self.get_config('general', 'include_coin_description', False)

    # Debug Configuration
    @property
    def DEBUG_SAVE_CHARTS(self):
        return self.get_config('debug', 'save_chart_images', False)

    @property
    def DEBUG_CHART_SAVE_PATH(self):
        return self.get_config('debug', 'chart_save_path', 'test_images')

    # Directory Configuration
    @property
    def LOG_DIR(self):
        return self.get_config('directories', 'log_dir', 'logs')

    @property
    def DATA_DIR(self):
        return self.get_config('directories', 'data_dir', 'data')

    # Dashboard Configuration
    @property
    def DASHBOARD_ENABLED(self):
        return self.get_config('dashboard', 'enabled', True)

    @property
    def DASHBOARD_HOST(self):
        return self.get_config('dashboard', 'host', '0.0.0.0')

    @property
    def DASHBOARD_PORT(self):
        return int(self.get_config('dashboard', 'port', 8000))

    @property
    def DASHBOARD_ENABLE_CORS(self):
        return self.get_config('dashboard', 'enable_cors', False)

    @property
    def DASHBOARD_CORS_ORIGINS(self):
        origins = self.get_config('dashboard', 'cors_origins', [])
        if isinstance(origins, str):
            if origins.strip() == '*':
                return ["*"]
            return [o.strip() for o in origins.split(',')]
        return origins

    # Cooldown Configuration
    @property
    def FILE_MESSAGE_EXPIRY(self):
        """Get file message expiry time in seconds (configured in hours in config.ini)."""
        hours = self.get_config('cooldowns', 'file_message_expiry', 168)
        return hours * 3600

    # RAG Configuration
    @property
    def RAG_UPDATE_INTERVAL_HOURS(self):
        return self.get_config('rag', 'update_interval_hours', 4)

    @property
    def RAG_CATEGORIES_UPDATE_INTERVAL_HOURS(self):
        return self.get_config('rag', 'categories_update_interval_hours', 24)

    @property
    def RAG_COINGECKO_UPDATE_INTERVAL_HOURS(self):
        return self.get_config('rag', 'coingecko_update_interval_hours', 24)

    @property
    def RAG_DEFILLAMA_UPDATE_INTERVAL_HOURS(self):
        return float(self.get_config('rag', 'defillama_update_interval_hours', 0.25))

    @property
    def RAG_COINGECKO_GLOBAL_API_URL(self):
        return self.get_config('rag', 'coingecko_global_api_url', 'https://api.coingecko.com/api/v3/global')

    @property
    def RAG_NEWS_LIMIT(self):
        """Maximum number of news articles to include in context (configurable via [rag] news_limit)."""
        return int(self.get_config('rag', 'news_limit', 5))

    @property
    def RAG_ARTICLE_MAX_TOKENS(self):
        """Maximum number of tokens per article (configurable via [rag] article_max_tokens)."""
        return int(self.get_config('rag', 'article_max_tokens', 256))

    @property
    def RAG_DENSITY_PENALTY_THRESHOLD(self):
        """Body length below which articles are penalized (default 300 chars)."""
        return int(self.get_config('rag', 'density_penalty_threshold', 300))

    @property
    def RAG_DENSITY_BOOST_THRESHOLD(self):
        """Body length above which articles get a boost (default 1000 chars)."""
        return int(self.get_config('rag', 'density_boost_threshold', 1000))

    @property
    def RAG_DENSITY_PENALTY_MULTIPLIER(self):
        """Score multiplier for short articles (default 0.5)."""
        return float(self.get_config('rag', 'density_penalty_multiplier', 0.5))

    @property
    def RAG_DENSITY_BOOST_MULTIPLIER(self):
        """Score multiplier for long articles (default 1.2)."""
        return float(self.get_config('rag', 'density_boost_multiplier', 1.2))

    @property
    def RAG_COOCCURRENCE_MULTIPLIER(self):
        """Score multiplier when all query keywords appear in article (default 1.5)."""
        return float(self.get_config('rag', 'cooccurrence_multiplier', 1.5))
    @property
    def SUPPORTED_EXCHANGES(self):
        """Returns list of supported exchanges in priority order."""
        return self.get_config('exchanges', 'supported', ['binance', 'kucoin', 'gateio'])

    @property
    def MARKET_REFRESH_HOURS(self):
        return self.get_config('exchanges', 'market_refresh_hours', 24)

    # Demo Trading Configuration
    @property
    def TRANSACTION_FEE_PERCENT(self):
        """Transaction fee percentage for limit orders (default 0.075%)."""
        return float(self.get_config('demo_trading', 'transaction_fee_percent', 0.00075))

    @property
    def DEMO_QUOTE_CAPITAL(self):
        """Initial capital for demo trading (default 10000)."""
        return float(self.get_config('demo_trading', 'demo_quote_capital', 10000.0))

    # Debate Configuration
    @property
    def DEBATE_ENABLED(self):
        """Enable Bull/Bear debate service (default True)."""
        return self.get_config('debate', 'enabled', True)

    @property
    def DEBATE_USE_QUICK_MODEL(self):
        """Use quick model for debate arguments (default True)."""
        return self.get_config('debate', 'use_quick_model', True)

    @property
    def DEBATE_SKIP_FOR_HOLD(self):
        """Skip debate for HOLD/CLOSE signals (default True)."""
        return self.get_config('debate', 'skip_for_hold', True)

    # Backtest Configuration
    @property
    def BACKTEST_INITIAL_CAPITAL(self):
        """Initial capital for backtesting (default 10000)."""
        return float(self.get_config('backtest', 'initial_capital', 10000.0))

    # Execution Engine (Layer 2) Configuration
    @property
    def EXECUTION_ENGINE_ENABLED(self):
        """Enable Layer 2 real-time execution engine (default True)."""
        return self.get_config('execution_engine', 'enabled', True)

    @property
    def EXECUTION_TRAILING_ENABLED(self):
        """Enable trailing stop in execution engine (default True)."""
        return self.get_config('execution_engine', 'trailing_enabled', True)

    @property
    def EXECUTION_TRAILING_ATR_MULT(self):
        """ATR multiplier for trailing stop distance (default 2.0)."""
        return float(self.get_config('execution_engine', 'trailing_atr_multiplier', 2.0))

    @property
    def EXECUTION_TRAILING_BREAKEVEN(self):
        """Move SL to entry after first partial TP hit (default True)."""
        return self.get_config('execution_engine', 'trailing_breakeven_on_tp1', True)

    @property
    def EXECUTION_PARTIAL_ENABLED(self):
        """Enable partial take-profit in execution engine (default False)."""
        return self.get_config('execution_engine', 'partial_enabled', False)

    @property
    def EXECUTION_PARTIAL_TARGETS(self):
        """Partial close targets as list of (distance_fraction, close_fraction) tuples.

        Parsed from comma-separated 'dist:close' pairs in config.ini.
        Default: [(0.5, 0.5), (1.0, 1.0)]
        """
        raw = self.get_config('execution_engine', 'partial_targets', '0.5:0.5, 1.0:1.0')
        if isinstance(raw, str):
            targets = []
            for pair in raw.split(','):
                pair = pair.strip()
                if ':' in pair:
                    dist, frac = pair.split(':', 1)
                    targets.append((float(dist.strip()), float(frac.strip())))
            return targets if targets else [(0.5, 0.5), (1.0, 1.0)]
        return [(0.5, 0.5), (1.0, 1.0)]

    # Fast Trading Mode Safety Guards ----------------------------------------
    @property
    def FAST_MIN_INTERVAL_SECONDS(self):
        """Minimum seconds between fast-mode trades (default 900 = 15 min)."""
        return int(self.get_config('fast_trading', 'min_interval_seconds', 900))

    @property
    def FAST_DAILY_LOSS_PCT_LIMIT(self):
        """Daily realised PnL% limit (default -3.0). Trading pauses if reached."""
        return float(self.get_config('fast_trading', 'daily_loss_pct_limit', -3.0))

    @property
    def FAST_CONSECUTIVE_LOSS_THRESHOLD(self):
        """Consecutive losses before cooldown trips (default 3)."""
        return int(self.get_config('fast_trading', 'consecutive_loss_threshold', 3))

    @property
    def FAST_CONSECUTIVE_LOSS_COOLDOWN_SECONDS(self):
        """Cooldown seconds after losing-streak trip (default 7200 = 2h)."""
        return int(self.get_config('fast_trading', 'consecutive_loss_cooldown_seconds', 7200))

    @property
    def FAST_POLL_INTERVAL_SECONDS(self):
        """Interval in seconds between fast-trading algo polls (default 300 = 5 min)."""
        return int(self.get_config('fast_trading', 'poll_interval_seconds', 300))

    @property
    def FAST_BLOCK_MINUTES_BEFORE_CLOSE(self):
        """Block opening NEW fast-mode positions within N minutes of market close (default 15).
        Set to 0 to disable. CLOSE signals are always allowed."""
        return int(self.get_config('fast_trading', 'block_minutes_before_close', 15))

    @property
    def FAST_ALLOW_GAP_TRADING(self):
        """If True, bypass the pre-close block (intentional gap plays). Default False."""
        return bool(self.get_config('fast_trading', 'allow_gap_trading', False))

    # Live Trading Configuration
    @property
    def LIVE_TRADING_ENABLED(self):
        """Enable real order execution (default False)."""
        return self.get_config('live_trading', 'enabled', False)

    @property
    def LIVE_EXCHANGE(self):
        """Exchange to use for live trading (default 'binance')."""
        return str(self.get_config('live_trading', 'exchange', 'binance'))

    @property
    def LIVE_ORDER_TYPE(self):
        """Order type for live trading: 'limit' or 'market' (default 'limit')."""
        return str(self.get_config('live_trading', 'order_type', 'limit'))

    @property
    def LIVE_MAX_ORDER_USD(self):
        """Maximum single order value in USD (default 500). Legacy global cap."""
        return float(self.get_config('live_trading', 'max_order_usd', 500.0))

    @property
    def LIVE_MAX_ORDER_USD_AI(self):
        """Max USD cap for AI slot. Falls back to LIVE_MAX_ORDER_USD when unset."""
        val = self.get_config('live_trading', 'max_order_usd_ai', None)
        if val is None or val == '':
            return self.LIVE_MAX_ORDER_USD
        return float(val)

    @property
    def LIVE_MAX_ORDER_USD_FAST(self):
        """Max USD cap for Fast slot. Falls back to LIVE_MAX_ORDER_USD when unset."""
        val = self.get_config('live_trading', 'max_order_usd_fast', None)
        if val is None or val == '':
            return self.LIVE_MAX_ORDER_USD
        return float(val)

    @property
    def DOUBLE_TRADE_ENABLED(self):
        """Allow AI slot + Fast slot to be open simultaneously (default False)."""
        val = self.get_config('live_trading', 'double_trade_enabled', False)
        if isinstance(val, str):
            return val.strip().lower() in ('true', '1', 'yes', 'on')
        return bool(val)

    @property
    def MAX_MIN_LOT_MARGIN_PCT(self):
        """Allow auto-snap UP to broker minimum lot when the required margin is
        at most this fraction of total capital (default 0.10 = 10%). Set to 0
        to disable (legacy refuse-below-min behaviour). Typical small-account
        setups: 0.05 to 0.15.
        """
        val = self.get_config('live_trading', 'max_min_lot_margin_pct', 0.10)
        try:
            return max(0.0, float(val))
        except (TypeError, ValueError):
            return 0.10

    @property
    def LIVE_CONFIRM_ORDERS(self):
        """Require manual confirmation before live orders (default True)."""
        return self.get_config('live_trading', 'confirm_orders', True)

    @property
    def LIVE_TESTNET(self):
        """Use exchange testnet/sandbox (Binance testnet.binance.vision). Default False."""
        return bool(self.get_config('live_trading', 'testnet', False))

    @property
    def BINANCE_API_KEY(self):
        """Binance API key from keys.env."""
        return self.get_env('BINANCE_API_KEY')

    @property
    def BINANCE_API_SECRET(self):
        """Binance API secret from keys.env."""
        return self.get_env('BINANCE_API_SECRET')

    @property
    def HF_TOKEN(self):
        """HuggingFace Hub token from keys.env (optional, for higher rate limits)."""
        return self.get_env('HF_TOKEN')

    # MT5 Configuration
    @property
    def MT5_ENABLED(self):
        """Enable MetaTrader 5 mode instead of CCXT crypto exchanges."""
        return self.get_config('mt5', 'enabled', False)

    @property
    def MT5_LOGIN(self):
        """MT5 account login number from keys.env."""
        return self.get_env('MT5_LOGIN')

    @property
    def MT5_PASSWORD(self):
        """MT5 account password from keys.env."""
        return self.get_env('MT5_PASSWORD')

    @property
    def MT5_SERVER(self):
        """MT5 broker server name from keys.env."""
        return self.get_env('MT5_SERVER')

    @property
    def MT5_TERMINAL_PATH(self):
        """Optional path to MT5 terminal executable."""
        return self.get_config('mt5', 'terminal_path', None)

    # Risk Management Defaults
    @property
    def DEFAULT_POSITION_SIZE(self):
        """Default position size as decimal (e.g. 0.02) if AI doesn't specify."""
        return float(self.get_config('risk_management', 'default_position_size', 0.02))

    @property
    def DEFAULT_STOP_LOSS_PCT(self):
        """Default stop loss percentage as decimal (e.g. 0.02) if AI doesn't specify."""
        return float(self.get_config('risk_management', 'default_stop_loss_pct', 0.02))

    @property
    def DEFAULT_TAKE_PROFIT_PCT(self):
        """Default take profit percentage as decimal (e.g. 0.04) if AI doesn't specify."""
        return float(self.get_config('risk_management', 'default_take_profit_pct', 0.04))

    @property
    def QUOTE_CURRENCY(self):
        """Extract quote currency from CRYPTO_PAIR (e.g., 'USDC' from 'BTC/USDC')."""
        pair = self.CRYPTO_PAIR
        if '/' in pair:
            return pair.split('/')[1]
        return 'USDC'

    @property
    def ASSET_CLASS(self) -> str:
        """Infer asset class from active profile.

        Returns one of: 'crypto', 'forex', 'oil', 'metals', 'macro'.
        Used to pick the right news feeds, keyword filters and dashboard labels.
        """
        symbol = (self.CRYPTO_PAIR or '').upper().replace('/', '').replace(' ', '')
        if not self.MT5_ENABLED:
            return 'crypto'
        oil_keys = ('OIL', 'WTI', 'BRENT', 'NATGAS', 'CRUD', 'USOIL', 'UKOIL',
                    'XTIUSD', 'XBRUSD', 'XNGUSD')
        metals_keys = ('GOLD', 'SILVER', 'XAUUSD', 'XAGUSD', 'XPTUSD', 'XPDUSD')
        if any(k in symbol for k in oil_keys):
            return 'oil'
        if any(k in symbol for k in metals_keys):
            return 'metals'
        if len(symbol) == 6 and symbol.isalpha():
            return 'forex'
        return 'macro'


    def get_model_config(self, model_name: str, overrides: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        Get configuration parameters for a specific model.

        Args:
            model_name: The name of the model
            overrides: Optional parameter overrides for this specific call

        Returns:
            A dictionary with configuration parameters
        """
        if self._is_google_model(model_name):
            base = self._google_model_config.copy()
        else:
            base = self._default_model_config.copy()

        if overrides:
            base.update(overrides)

        cleaned = {k: v for k, v in base.items() if v is not None}
        return cleaned

    def _is_google_model(self, model_name: str) -> bool:
        """Determine if a model should use Google-specific configuration."""
        return model_name == self.GOOGLE_STUDIO_MODEL

    def reload(self):
        """Reload both keys.env and config.ini files.

        This allows runtime configuration changes without restarting the application.
        """
        logging.info("Reloading configuration files...")
        try:
            self._env_vars = {}
            self._config_data = {}
            self._load_environment()
            self._load_ini_config()
            self._build_dynamic_urls()
            self._build_model_configs()
            logging.info("Configuration reloaded successfully")
        except Exception as e:
            logging.error("Error reloading configuration: %s", e)
            raise


# Create global config instance
config = Config()
