"""Router for Binance trade history analysis.

Exposes:
  GET /api/history/analyze   - reconstruct positions + metrics
  GET /api/history/fills     - raw fills list
"""
from __future__ import annotations

import time
from typing import Optional

from fastapi import APIRouter, Query


class HistoryRouter:
    """Handles Binance raw fill fetching and position analysis."""

    def __init__(self, config, logger, dashboard_state):
        self.router = APIRouter(prefix="/api/history", tags=["history"])
        self.config = config
        self.logger = logger
        self.dashboard_state = dashboard_state

        self.router.add_api_route(
            "/analyze",
            self.analyze,
            methods=["GET"],
        )
        self.router.add_api_route(
            "/fills",
            self.get_fills,
            methods=["GET"],
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_exchange(self):
        """Build a CCXT Binance instance from config credentials."""
        import ccxt.async_support as ccxt

        api_key = getattr(self.config, "BINANCE_API_KEY", "") or ""
        api_secret = getattr(self.config, "BINANCE_API_SECRET", "") or ""
        testnet = bool(getattr(self.config, "LIVE_USE_TESTNET", False))

        exchange = ccxt.binance(
            {
                "apiKey": api_key,
                "secret": api_secret,
                "enableRateLimit": True,
                "options": {"defaultType": "spot"},
            }
        )
        if testnet:
            exchange.set_sandbox_mode(True)
        return exchange

    # ------------------------------------------------------------------
    # Endpoints
    # ------------------------------------------------------------------

    async def analyze(
        self,
        symbol: str = Query("BTC/USDT", description="Trading pair, e.g. BTC/USDT"),
        method: str = Query("fifo", description="fifo or average"),
        days: Optional[int] = Query(None, description="Look-back window in days (None = all history)"),
    ):
        """Fetch fills from Binance and reconstruct positions + metrics."""
        cache_key = f"history_analyze_{symbol}_{method}_{days}"
        cached = self.dashboard_state.get_cached(cache_key, ttl_seconds=120.0)
        if cached:
            return cached

        from src.trading.binance_history_analyzer import BinanceHistoryAnalyzer

        since: Optional[int] = None
        if days:
            since = int((time.time() - days * 86400) * 1000)

        exchange = self._get_exchange()
        try:
            ana = BinanceHistoryAnalyzer(exchange)
            report = await ana.run(symbol, method=method, since=since)
        except Exception as exc:
            self.logger.error("[HistoryRouter] analyze error: %s", exc)
            return {"error": str(exc)}
        finally:
            try:
                await exchange.close()
            except Exception:
                pass

        # Serialize closed legs
        closed = [
            {
                "symbol": leg.symbol,
                "direction": leg.direction,
                "entry_price": leg.entry_price,
                "exit_price": leg.exit_price,
                "qty": leg.qty,
                "entry_time": leg.entry_time.isoformat(),
                "exit_time": leg.exit_time.isoformat(),
                "fee": leg.fee,
                "pnl_quote": leg.pnl_quote,
                "pnl_net_quote": leg.pnl_net_quote,
                "pnl_pct": leg.pnl_pct,
                "is_win": leg.is_win,
            }
            for leg in report.closed
        ]

        # Serialize open legs
        open_legs = [
            {
                "symbol": leg.symbol,
                "direction": leg.direction,
                "avg_price": leg.avg_price,
                "qty": leg.qty,
                "entry_time": leg.entry_time.isoformat(),
                "current_price": leg.current_price,
                "unrealized_pnl_quote": leg.unrealized_pnl_quote,
                "unrealized_pnl_pct": leg.unrealized_pnl_pct,
            }
            for leg in report.open_legs
        ]

        result = {
            "symbol": symbol,
            "method": method,
            "days": days,
            "metrics": {
                "total_trades": report.total_trades,
                "winning_trades": report.winning_trades,
                "losing_trades": report.losing_trades,
                "win_rate": report.win_rate,
                "total_pnl_quote": report.total_pnl_quote,
                "total_pnl_pct": report.total_pnl_pct,
                "total_fees_quote": report.total_fees_quote,
                "best_trade_pct": report.best_trade_pct,
                "worst_trade_pct": report.worst_trade_pct,
                "max_drawdown_pct": report.max_drawdown_pct,
                "avg_drawdown_pct": report.avg_drawdown_pct,
                "unrealized_pnl_quote": report.unrealized_pnl_quote,
            },
            "closed": closed,
            "open_legs": open_legs,
        }

        self.dashboard_state.set_cached(cache_key, result)
        return result

    async def get_fills(
        self,
        symbol: str = Query("BTC/USDT"),
        days: Optional[int] = Query(30),
    ):
        """Return raw fills list for a symbol (last N days)."""
        from src.trading.binance_history_analyzer import BinanceHistoryAnalyzer

        since: Optional[int] = None
        if days:
            since = int((time.time() - days * 86400) * 1000)

        exchange = self._get_exchange()
        try:
            ana = BinanceHistoryAnalyzer(exchange)
            fills = await ana.fetch_fills(symbol, since=since)
        except Exception as exc:
            self.logger.error("[HistoryRouter] fills error: %s", exc)
            return {"error": str(exc)}
        finally:
            try:
                await exchange.close()
            except Exception:
                pass

        return {
            "symbol": symbol,
            "count": len(fills),
            "fills": [
                {
                    "time": f.timestamp.isoformat(),
                    "side": f.side,
                    "price": f.price,
                    "qty": f.qty,
                    "fee": f.fee,
                    "fee_currency": f.fee_currency,
                    "trade_id": f.trade_id,
                }
                for f in fills
            ],
        }
