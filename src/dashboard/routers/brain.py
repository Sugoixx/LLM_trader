"""Router for brain state, memory vectors, and positions."""
import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List, Optional, Union

from fastapi import APIRouter, Query, Request

from src.utils.indicator_classifier import (
    build_context_string_from_technical_data,
    build_query_document_from_technical_data,
    classify_adx_label,
    classify_trend_direction,
)


def _read_json_file(file_path: Path) -> Optional[Union[Dict[str, Any], List[Any]]]:
    """Helper to read JSON file synchronously for offloading to a thread."""
    if not file_path.exists():
        return None
    with open(file_path, "r", encoding="utf-8") as f:
        return json.load(f)


def _extract_persisted_technical_data(data: Dict[str, Any]) -> Dict[str, Any]:
    """Return persisted indicator values from new or legacy previous_response shapes."""
    technical_data = data.get("technical_data")
    if isinstance(technical_data, dict) and technical_data:
        return technical_data

    response = data.get("response", {})
    if not isinstance(response, dict):
        return {}

    return {
        key: value
        for key, value in response.items()
        if key != "text_analysis"
    }


def _distance_pct_or_fallback(stored_pct: Optional[float], entry_price: float, target_price: float) -> float:
    """Return stored distance percent or derive it from entry and target prices."""
    if stored_pct and stored_pct > 0:
        return stored_pct
    if entry_price <= 0:
        return 0.0
    return abs(target_price - entry_price) / entry_price


def _safe_float(value: Any, default: float = 0.0) -> float:
    """Convert value to float defensively."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default

def _extract_market_status(data: Dict[str, Any], unified_parser=None) -> Dict[str, Any]:
    """Helper to extract market status from previous_response data."""
    response = data.get("response", {})
    text = response.get("text_analysis", "")
    technical_data = _extract_persisted_technical_data(data)
    status = {
        "trend": "NEUTRAL",
        "action": "--",
        "confidence": "--",
        "adx": response.get("adx"),
        "rsi": response.get("rsi")
    }

    if technical_data:
        status["adx"] = technical_data.get("adx", status["adx"])
        status["rsi"] = technical_data.get("rsi", status["rsi"])
        status["trend"] = classify_trend_direction(technical_data)

    signal_match = re.search(r'\bSIGNAL\s*:\s*([A-Z_]+)\b', text, re.IGNORECASE)
    if signal_match:
        status["action"] = signal_match.group(1).upper()

    confidence_match = re.search(r'\bConfidence\s*:\s*(\d+(?:\.\d+)?)\s*%', text, re.IGNORECASE)
    if confidence_match:
        confidence_value = float(confidence_match.group(1))
        status["confidence"] = int(confidence_value) if confidence_value.is_integer() else confidence_value

    if status["trend"] == "NEUTRAL":
        if "BEARISH" in text.upper():
            status["trend"] = "BEARISH"
        elif "BULLISH" in text.upper():
            status["trend"] = "BULLISH"
    return status

def _build_current_market_context(config, logger, unified_parser=None) -> tuple[str, str]:
    """Build rich context query string from current market conditions.

    Reads ``technical_data`` from ``previous_response.json`` (persisted by the
    analysis engine after each run) and applies the same indicator classification
    logic used during live trading so that similarity queries are semantically
    identical to the documents stored in vector memory.

    Returns:
        Tuple of (display_context, query_document). display_context is the
        categorical string for display; query_document is the enriched string
        for embedding search. Both are empty strings on failure.
    """
    data_dir = config.DATA_DIR
    prev_response_file = Path(data_dir) / "trading" / "previous_response.json"
    if not prev_response_file.exists():
        return "", ""
    try:
        data = _read_json_file(prev_response_file)
        if not data:
            return "", ""
        technical_data = _extract_persisted_technical_data(data)
        if not technical_data:
            status = _extract_market_status(data, unified_parser)
            adx = status["adx"] or 0
            adx_label = classify_adx_label(adx)
            fallback = f"{status['trend']} + {adx_label} + MEDIUM Volatility"
            return fallback, fallback
        current_price: Optional[float] = None
        response = data.get("response", {})
        if isinstance(response, dict):
            current_price = response.get("current_price")
        sentiment_data: Optional[Dict[str, Any]] = data.get("sentiment")
        is_weekend = datetime.now().weekday() >= 5
        shared_kwargs: Dict[str, Any] = {
            "technical_data": technical_data,
            "current_price": current_price,
            "sentiment_data": sentiment_data,
            "is_weekend": is_weekend,
        }
        display_context = build_context_string_from_technical_data(**shared_kwargs)
        query_document = build_query_document_from_technical_data(**shared_kwargs)
        return display_context, query_document
    except Exception:  # pylint: disable=broad-exception-caught
        logger.error("Failed to build market context", exc_info=True)
        return "", ""


class BrainRouter:
    """Handles endpoints for the trading brain status, rules, and memory."""
    # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-instance-attributes
    def __init__(self, config, logger, dashboard_state, vector_memory, unified_parser, persistence, exchange_manager):
        self.router = APIRouter(prefix="/api/brain", tags=["brain"])
        self.config = config
        self.logger = logger
        self.dashboard_state = dashboard_state
        self.vector_memory = vector_memory
        self.unified_parser = unified_parser
        self.persistence = persistence
        self.exchange_manager = exchange_manager

        self.router.add_api_route("/status", self.get_brain_status, methods=["GET"])
        self.router.add_api_route("/memory", self.get_vector_memory, methods=["GET"])
        self.router.add_api_route("/rules", self.get_active_rules, methods=["GET"])
        self.router.add_api_route("/vectors", self.get_vector_details, methods=["GET"])
        self.router.add_api_route("/position", self.get_current_position, methods=["GET"])
        self.router.add_api_route("/refresh-price", self.refresh_current_price, methods=["GET"])

    async def _load_broker_positions(self) -> List[Dict[str, Any]]:
        """Load open positions from broker when supported (MT5 fallback)."""
        if not self.exchange_manager:
            return []

        symbol = getattr(self.config, "CRYPTO_PAIR", "")
        exchange = None

        exchanges = getattr(self.exchange_manager, "exchanges", {})
        if isinstance(exchanges, dict) and exchanges:
            # Reuse connected exchange to avoid repetitive symbol lookup spam.
            exchange = next(iter(exchanges.values()))

        if not exchange:
            try:
                if symbol:
                    exchange, _ = await self.exchange_manager.find_symbol_exchange(symbol)
            except Exception:
                self.logger.warning("Broker symbol lookup failed for %s", symbol, exc_info=True)

        if not exchange or not hasattr(exchange, "fetch_positions"):
            return []

        # Skip on spot-only exchanges: spot has balances, not positions
        try:
            default_type = (getattr(exchange, 'options', {}) or {}).get('defaultType', 'spot')
            if default_type == 'spot':
                return []
        except Exception:
            pass
        # Also skip without credentials (public mode)
        if not getattr(exchange, 'apiKey', None):
            return []

        try:
            # Binance (and some other CCXT exchanges) require an array of symbols, not a bare string
            if symbol:
                positions = await exchange.fetch_positions([symbol])
            else:
                positions = await exchange.fetch_positions()
        except TypeError:
            try:
                positions = await exchange.fetch_positions()
            except Exception:
                self.logger.warning("Failed to fetch broker positions", exc_info=True)
                return []
        except Exception:
            self.logger.warning("Failed to fetch broker positions", exc_info=True)
            return []

        if not isinstance(positions, list):
            return []

        return [
            p for p in positions
            if isinstance(p, dict) and _safe_float(p.get("volume"), 0.0) > 0
        ]

    def _build_broker_position_payload(
        self,
        broker_position: Dict[str, Any],
        current_price: Optional[float],
        open_count: int,
    ) -> Dict[str, Any]:
        """Map MT5/ccxt broker position shape to dashboard payload shape."""
        position_type = str(broker_position.get("type", "")).lower()
        direction = "LONG" if position_type == "buy" else "SHORT"

        entry_price = _safe_float(broker_position.get("price_open"), 0.0)
        stop_loss = _safe_float(broker_position.get("sl"), 0.0)
        take_profit = _safe_float(broker_position.get("tp"), 0.0)
        broker_price = _safe_float(broker_position.get("price_current"), 0.0)
        effective_price = current_price if current_price is not None else (broker_price if broker_price > 0 else None)

        sl_distance_pct = (
            abs(stop_loss - entry_price) / entry_price
            if entry_price > 0 and stop_loss > 0
            else 0.0
        )
        tp_distance_pct = (
            abs(take_profit - entry_price) / entry_price
            if entry_price > 0 and take_profit > 0
            else 0.0
        )
        rr_ratio = (tp_distance_pct / sl_distance_pct) if sl_distance_pct > 0 else 0.0

        result = {
            "has_position": True,
            "position_source": "broker_mt5",
            "current_price": effective_price,
            "direction": direction,
            "symbol": str(broker_position.get("symbol") or getattr(self.config, "CRYPTO_PAIR", "")),
            "entry_price": entry_price,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "entry_time": datetime.now(timezone.utc).isoformat(),
            "sl_distance_pct": sl_distance_pct,
            "tp_distance_pct": tp_distance_pct,
            "rr_ratio": rr_ratio,
            "confidence": "LIVE",
            "size": _safe_float(broker_position.get("volume"), 0.0),
            "size_pct": 0.0,
            "quote_amount": 0.0,
            "adx_at_entry": None,
            "rsi_at_entry": None,
            "max_drawdown_pct": 0.0,
            "max_profit_pct": 0.0,
            "confluence_factors": [],
            "broker_ticket": broker_position.get("ticket"),
            "broker_position_count": open_count,
        }

        if open_count > 1:
            result["sync_warning"] = (
                f"{open_count} open broker positions detected. Dashboard shows first position only."
            )

        return result

    async def get_brain_status(self) -> Dict[str, Any]:
        """Get the current thought process/status of the brain."""
        cached = self.dashboard_state.get_cached("brain_status", ttl_seconds=30.0)
        if cached:
            cached["market_hours"] = getattr(self.dashboard_state, "market_hours_status", None)
            return cached
        try:
            data_dir = self.config.DATA_DIR
        except AttributeError:
            data_dir = "data"
        prev_response_file = Path(data_dir) / "trading" / "previous_response.json"
        stats_file = Path(data_dir) / "trading" / "statistics.json"
        status = {"status": "active", "trend": "--", "confidence": "--", "action": "--", "adx": None, "rsi": None}
        try:
            prev_data = await asyncio.to_thread(_read_json_file, prev_response_file)
            if prev_data is not None:
                extracted = _extract_market_status(prev_data, self.unified_parser)
                status.update(extracted)
        except Exception:
            self.logger.error("Failed to load brain status", exc_info=True)

        try:
            stats = await asyncio.to_thread(_read_json_file, stats_file)
            if stats is not None:
                status.update({
                    "total_trades": stats.get("total_trades", 0),
                    "win_rate": stats.get("win_rate", 0),
                    "current_capital": stats.get("current_capital", 0)
                })
        except Exception:
            self.logger.error("Failed to load statistics", exc_info=True)

        status["market_hours"] = getattr(self.dashboard_state, "market_hours_status", None)
        self.dashboard_state.set_cached("brain_status", status)
        return status

    async def get_vector_memory(self, limit: int = Query(default=100, ge=1, le=500)) -> Dict[str, Any]:
        """Get recent vector memories (synapses)."""
        cached = self.dashboard_state.get_cached(f"memory_{limit}", ttl_seconds=30.0)
        if cached:
            return cached
        data_dir = getattr(self.config, "DATA_DIR", "data")
        result = {
            "experience_count": 0,
            "trades": [],
            "stats": {}
        }
        if self.vector_memory:
            result["experience_count"] = self.vector_memory.experience_count
            result["stats"] = self.vector_memory.compute_confidence_stats()
        trade_history_file = Path(data_dir) / "trading" / "trade_history.json"
        try:
            trades = await asyncio.to_thread(_read_json_file, trade_history_file)
            if trades is not None:
                result["trades"] = [
                    {
                        "id": f"trade_{i}",
                        "timestamp": t.get("timestamp"),
                        "action": t.get("action"),
                        "price": t.get("price"),
                        "confidence": t.get("confidence"),
                        "reasoning": t.get("reasoning", "")[:100]
                    }
                    for i, t in enumerate(trades[-limit:])
                ]
        except Exception:
            self.logger.error("Failed to load trade history for memory", exc_info=True)

        self.dashboard_state.set_cached(f"memory_{limit}", result)
        return result

    async def get_active_rules(self) -> List[Dict[str, Any]]:
        """Get currently active semantic rules."""
        cached = self.dashboard_state.get_cached("rules", ttl_seconds=30.0)
        if cached:
            return cached
        if not self.vector_memory:
            return []
        try:
            raw_rules = self.vector_memory.get_active_rules(n_results=20)
            rules = []
            for r in raw_rules:
                mapped_rule = dict(r)
                mapped_rule["rule_text"] = r.get("text", "")
                meta = r.get("metadata", {})
                mapped_rule["win_rate"] = meta.get("win_rate")
                mapped_rule["source_trades"] = meta.get("source_trades")
                rules.append(mapped_rule)
            self.dashboard_state.set_cached("rules", rules)
            return rules
        except Exception:
            self.logger.error("Failed to retrieve active rules", exc_info=True)
            return []

    async def get_vector_details(
        self, request: Request, query: str = Query(default=None, max_length=500), limit: int = Query(default=50, ge=1, le=500)
    ) -> Dict[str, Any]:
        """Get detailed vector memory contents from ChromaDB."""
        # Validate inputs to prevent DoS via unbounded cache keys
        sort_by = request.query_params.get("sort_by", "date")
        if sort_by not in ["date", "similarity", "pnl", "outcome", "confidence", "direction"]:
            sort_by = "date"

        order = request.query_params.get("order", "desc")
        if order not in ["asc", "desc"]:
            order = "desc"

        cache_key = f"vectors_{limit}_{sort_by}_{order}"
        if not query:
            cached = self.dashboard_state.get_cached(cache_key, ttl_seconds=30.0)
            if cached:
                return cached
        result = {
            "experience_count": 0,
            "experiences": [],
            "confidence_stats": {},
            "adx_stats": {},
            "factor_stats": {},
            "rule_count": 0,
            "current_context": None
        }
        if not self.vector_memory:
            return result
        try:
            result["experience_count"] = self.vector_memory.trade_count
            result["rule_count"] = self.vector_memory.semantic_rule_count
            result["confidence_stats"] = self.vector_memory.compute_confidence_stats()
            result["adx_stats"] = self.vector_memory.compute_adx_performance()
            result["factor_stats"] = self.vector_memory.compute_factor_performance()
            where_filter = {"outcome": {"$ne": "UPDATE"}}
            embed_query = query
            display_context = query
            if not embed_query:
                display_context, embed_query = await asyncio.to_thread(
                    _build_current_market_context, self.config, self.logger, self.unified_parser
                )
                if display_context:
                    result["current_context"] = display_context
            if embed_query:
                experiences = self.vector_memory.retrieve_similar_experiences(
                    embed_query, k=limit, where=where_filter
                )
            else:
                experiences = self.vector_memory.get_all_experiences(limit=limit, where=where_filter)
            reverse = order == "desc"
            def get_sort_key(item):
                # pylint: disable=too-many-return-statements
                meta = item.metadata
                if sort_by == "date":
                    return meta.get("timestamp", "")
                if sort_by == "similarity":
                    return item.similarity
                if sort_by == "pnl":
                    return meta.get("pnl_pct", 0)
                if sort_by == "outcome":
                    return meta.get("outcome", "")
                if sort_by == "confidence":
                    conf_map = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}
                    return conf_map.get(meta.get("confidence", "LOW"), 0)
                if sort_by == "direction":
                    return meta.get("direction", "")
                return 0
            experiences.sort(key=get_sort_key, reverse=reverse)
            experiences_list = []
            for exp in experiences[:limit]:
                experiences_list.append({
                    "id": exp.id,
                    "document": exp.document,
                    "similarity": exp.similarity,
                    "recency": exp.recency,
                    "hybrid_score": exp.hybrid_score,
                    "metadata": exp.metadata
                })
            result["experiences"] = experiences_list
            if not query:
                self.dashboard_state.set_cached(cache_key, result)
        except Exception:
            self.logger.error("Failed to retrieve vector details", exc_info=True)
            result["error"] = "Internal error retrieving vector details"
        return result

    async def get_current_position(self) -> Dict[str, Any]:
        """Get current open position details."""
        cached = self.dashboard_state.get_cached("position", ttl_seconds=10.0)
        if cached:
            return cached
        current_price = self.dashboard_state.current_price
        if current_price is None:
            data_dir = self.config.DATA_DIR
            prev_response_file = Path(data_dir) / "trading" / "previous_response.json"
            try:
                data = await asyncio.to_thread(_read_json_file, prev_response_file)
                if data is not None:
                    prompt = data.get("prompt", "")
                    match = re.search(r"Current Price:\s*\$?([\d,]+\.?\d*)", prompt)
                    if match:
                        current_price = float(match.group(1).replace(",", ""))
            except Exception:
                self.logger.error("Failed to parse current price from prompt", exc_info=True)

        if not self.persistence:
            return {"has_position": False, "error": "Persistence not available"}

        broker_positions = await self._load_broker_positions()
        broker_count = len(broker_positions)

        position = self.persistence.load_position()
        if not position:
            if broker_positions:
                broker_payload = self._build_broker_position_payload(
                    broker_position=broker_positions[0],
                    current_price=current_price,
                    open_count=broker_count,
                )
                self.dashboard_state.set_cached("position", broker_payload)
                return broker_payload

            res = {"has_position": False, "current_price": current_price}
            self.dashboard_state.set_cached("position", res)
            return res
        sl_distance_pct = _distance_pct_or_fallback(
            position.sl_distance_pct,
            position.entry_price,
            position.stop_loss,
        )
        tp_distance_pct = _distance_pct_or_fallback(
            position.tp_distance_pct,
            position.entry_price,
            position.take_profit,
        )
        res = {
            "has_position": True,
            "position_source": "local_persistence",
            "current_price": current_price,
            "direction": position.direction,
            "symbol": position.symbol,
            "entry_price": position.entry_price,
            "stop_loss": position.stop_loss,
            "take_profit": position.take_profit,
            "entry_time": position.entry_time.isoformat(),
            "sl_distance_pct": sl_distance_pct,
            "tp_distance_pct": tp_distance_pct,
            "rr_ratio": position.rr_ratio_at_entry,
            "confidence": position.confidence,
            "size": position.size,
            "size_pct": position.size_pct,
            "quote_amount": position.quote_amount,
            "adx_at_entry": position.adx_at_entry,
            "rsi_at_entry": position.rsi_at_entry,
            "max_drawdown_pct": position.max_drawdown_pct,
            "max_profit_pct": position.max_profit_pct,
            "confluence_factors": position.confluence_factors
        }

        if broker_count > 1:
            res["broker_position_count"] = broker_count
            res["sync_warning"] = (
                f"Broker reports {broker_count} open positions while strategy tracks one local position."
            )
        elif broker_count == 0 and getattr(self.config, "MT5_ENABLED", False):
            res["broker_position_count"] = 0
            res["sync_warning"] = (
                "Strategy has a local position but broker reports none. Possible stale local state."
            )

        self.dashboard_state.set_cached("position", res)
        return res

    async def refresh_current_price(self) -> Dict[str, Any]:
        """Fetch fresh price from exchange and update dashboard state."""
        if not self.exchange_manager:
            return {"success": False, "error": "Exchange manager not available"}
        try:
            symbol = self.config.CRYPTO_PAIR
            exchange, _ = await self.exchange_manager.find_symbol_exchange(symbol)
            if not exchange:
                return {"success": False, "error": f"No exchange found for {symbol}"}
            ticker = await exchange.fetch_ticker(symbol)
            if not ticker:
                return {"success": False, "error": "Failed to fetch ticker"}
            price = float(ticker.get('last', ticker.get('close', 0)))
            if price > 0 and self.dashboard_state:
                await self.dashboard_state.update_price(price)
            return {"success": True, "current_price": price, "symbol": symbol}
        except Exception:
            self.logger.error("Internal error during price refresh", exc_info=True)
            return {"success": False, "error": "Internal error during price refresh"}
