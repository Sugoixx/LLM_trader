"""
MT5 Exchange Manager — drop-in replacement for ExchangeManager when
trading non-crypto assets via MetaTrader 5 (e.g. Oil, Forex, Indices).

Implements the same public API surface that CryptoTradingBot and
AnalysisEngine use from ExchangeManager:
  - initialize()
  - shutdown()
  - find_symbol_exchange(symbol) -> (exchange, exchange_id)
  - get_all_symbols() -> Set[str]
"""

import asyncio
from typing import Optional, Set, Tuple, TYPE_CHECKING

from src.logger.logger import Logger
from src.platforms.mt5_exchange import MT5Exchange, MT5_AVAILABLE

if TYPE_CHECKING:
    from src.config.protocol import ConfigProtocol


class MT5ExchangeManager:
    """Manages the MT5 terminal connection and symbol discovery.

    Acts as a drop-in replacement for ExchangeManager when the bot
    is configured for MT5 trading (Forex / CFD / Commodities).
    """

    def __init__(self, logger: Logger, config: "ConfigProtocol"):
        self.logger = logger
        self.config = config
        self._exchange: Optional[MT5Exchange] = None
        # Maintain same dict interface as ExchangeManager so RAG/dashboard don't break
        self.exchanges: dict = {}
        self.symbols_by_exchange: dict = {}

    async def initialize(self) -> None:
        """Connect to MT5 terminal and load symbols."""
        if not MT5_AVAILABLE:
            self.logger.error(
                "MetaTrader5 package not installed. "
                "Install with: pip install MetaTrader5  (Windows only)"
            )
            return

        login = self.config.MT5_LOGIN
        password = self.config.MT5_PASSWORD
        server = self.config.MT5_SERVER

        if not login or not password or not server:
            self.logger.error(
                "MT5 credentials missing. Set MT5_LOGIN, MT5_PASSWORD, "
                "MT5_SERVER in keys.env"
            )
            return

        terminal_path = self.config.MT5_TERMINAL_PATH

        self._exchange = MT5Exchange(
            logger=self.logger,
            login=int(login),
            password=password,
            server=server,
            terminal_path=terminal_path if terminal_path else None,
        )

        connected = await self._exchange.connect()
        if connected:
            self.exchanges["mt5"] = self._exchange
            self.symbols_by_exchange["mt5"] = self._exchange.symbols
            self.logger.info(
                "MT5ExchangeManager ready — %d symbols available",
                len(self._exchange.symbols),
            )
        else:
            self.logger.error("MT5 connection failed — trading will not be available")
            self._exchange = None

    async def shutdown(self) -> None:
        """Disconnect from MT5 terminal."""
        if self._exchange:
            await self._exchange.close()
            self._exchange = None
        self.exchanges.clear()
        self.symbols_by_exchange.clear()
        self.logger.info("MT5ExchangeManager shutdown complete")

    async def find_symbol_exchange(
        self, symbol: str
    ) -> Tuple[Optional[MT5Exchange], Optional[str]]:
        """Find the exchange that supports the given symbol.

        For MT5 there's only one 'exchange' (the broker terminal).
        Returns (MT5Exchange, 'mt5') if the symbol is found.
        """
        if not self._exchange:
            self.logger.error("MT5 not connected — cannot look up symbol %s", symbol)
            return None, None

        # Resolve the symbol (handles aliases like WTI/USD → XTIUSD)
        resolved = self._exchange._resolve_symbol(symbol)

        if resolved in self._exchange.symbols or symbol in self._exchange.symbols:
            self.logger.debug("Found symbol %s on MT5 (resolved: %s)", symbol, resolved)
            return self._exchange, "mt5"

        # Symbol not visible — try enabling it in MarketWatch
        try:
            import MetaTrader5 as mt5_lib
            ok = await asyncio.to_thread(mt5_lib.symbol_select, resolved, True)
            if ok:
                self._exchange.symbols.add(resolved)
                self.symbols_by_exchange["mt5"] = self._exchange.symbols
                self.logger.info("Enabled symbol %s in MT5 MarketWatch", resolved)
                return self._exchange, "mt5"
        except Exception as e:
            self.logger.warning("Failed to enable %s in MT5: %s", resolved, e)

        self.logger.warning("Symbol %s not found in MT5 MarketWatch", symbol)
        return None, None

    def get_all_symbols(self) -> Set[str]:
        """Get all visible MT5 symbols."""
        if self._exchange:
            return self._exchange.symbols
        return set()
