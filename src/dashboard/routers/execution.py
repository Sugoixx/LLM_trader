"""Dashboard router for Layer 2 Execution Engine status and trading controls."""

import json
import re
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional


class ManualTradeRequest(BaseModel):
    """Request body for manual BUY/SELL from dashboard."""
    stop_loss: float
    take_profit: float
    volume: Optional[float] = None  # Lots / contracts (None = auto-size via risk manager)


class ExecutionRouter:
    """Exposes Layer 2 execution engine state and trading controls via REST endpoints."""

    def __init__(self, config, logger, dashboard_state, trading_strategy=None, exchange_manager=None):
        self.router = APIRouter(prefix="/api", tags=["execution"])
        self.config = config
        self.logger = logger
        self.dashboard_state = dashboard_state
        self.trading_strategy = trading_strategy
        self.exchange_manager = exchange_manager
        self._register_routes()

    def _register_routes(self):
        @self.router.get("/execution/status")
        async def execution_status():
            """Return current Layer 2 execution engine status."""
            state = self.dashboard_state
            engine_data = getattr(state, '_execution_snapshot', None) or {}
            return JSONResponse(content={
                "enabled": self.config.EXECUTION_ENGINE_ENABLED,
                "trailing_enabled": self.config.EXECUTION_TRAILING_ENABLED,
                "trailing_atr_multiplier": self.config.EXECUTION_TRAILING_ATR_MULT,
                "trailing_breakeven_on_tp1": self.config.EXECUTION_TRAILING_BREAKEVEN,
                "partial_enabled": self.config.EXECUTION_PARTIAL_ENABLED,
                "partial_targets": self.config.EXECUTION_PARTIAL_TARGETS,
                "timeframe": self.config.TIMEFRAME,
                **engine_data,
            })

        @self.router.get("/execution/monitor")
        async def execution_monitor():
            """Return real-time position monitor state (trailing SL, partial progress)."""
            state = self.dashboard_state
            monitor_data = getattr(state, '_monitor_snapshot', None) or {}
            return JSONResponse(content=monitor_data)

        @self.router.get("/execution/auto-trade")
        async def get_auto_trade():
            """Return current auto-trade status."""
            return JSONResponse(content={
                "enabled": self.dashboard_state.auto_trade_enabled,
            })

        @self.router.post("/execution/auto-trade")
        async def toggle_auto_trade():
            """Toggle auto-trade on/off. Returns new state."""
            new_state = await self.dashboard_state.toggle_auto_trade()
            label = "ENABLED" if new_state else "DISABLED"
            self.logger.warning("Auto-trade %s via dashboard", label)
            return JSONResponse(content={
                "enabled": new_state,
            })

        @self.router.get("/execution/capital-alert")
        async def get_capital_alert():
            """Return current capital top-up alert (or null)."""
            return JSONResponse(content={
                "alert": getattr(self.dashboard_state, "capital_alert", None),
            })

        @self.router.post("/execution/capital-alert/dismiss")
        async def dismiss_capital_alert():
            """Dismiss the current capital alert banner."""
            await self.dashboard_state.update_capital_alert(None)
            return JSONResponse(content={"dismissed": True})

        # ── Manual Trading Endpoints ──────────────────────────────────────

        @self.router.post("/trade/buy")
        async def manual_buy(req: ManualTradeRequest):
            """Open a LONG position manually. AI will monitor SL/TP."""
            return await self._execute_manual_trade("BUY", req)

        @self.router.post("/trade/sell")
        async def manual_sell(req: ManualTradeRequest):
            """Open a SHORT position manually. AI will monitor SL/TP."""
            return await self._execute_manual_trade("SELL", req)

        @self.router.post("/trade/close")
        async def manual_close():
            """Close the current position manually."""
            strategy = self.trading_strategy
            if not strategy:
                return JSONResponse(status_code=503, content={"error": "Trading strategy not available"})
            if not strategy.current_position:
                return JSONResponse(status_code=400, content={"error": "No open position"})

            pos = strategy.current_position
            current_price = await self._get_current_price(pos.symbol)

            self.logger.warning("[MANUAL] Dashboard close requested for %s %s @ ~%.5f", pos.direction, pos.symbol, current_price)
            decision = await strategy.manual_close_position(current_price)
            if decision:
                return JSONResponse(content={"success": True, "action": "CLOSE", "symbol": pos.symbol})
            return JSONResponse(status_code=500, content={"error": "Close failed"})

        @self.router.post("/trade/close-all")
        async def manual_close_all():
            """Close ALL open broker positions for the current symbol."""
            strategy = self.trading_strategy
            if not strategy:
                return JSONResponse(status_code=503, content={"error": "Trading strategy not available"})
            if not strategy.order_executor:
                return JSONResponse(status_code=503, content={"error": "No order executor available"})

            symbol = self.config.CRYPTO_PAIR
            exchange = self._get_exchange()
            if not exchange or not hasattr(exchange, 'fetch_positions'):
                return JSONResponse(status_code=503, content={"error": "Exchange not available"})

            try:
                positions = await exchange.fetch_positions(symbol)
            except Exception as e:
                return JSONResponse(status_code=500, content={"error": f"Failed to fetch positions: {e}"})

            if not positions:
                return JSONResponse(status_code=400, content={"error": "No open positions on broker"})

            current_price = await self._get_current_price(symbol)
            closed_count = 0
            errors = []

            for pos in positions:
                ticket = pos.get('ticket', 0)
                pos_type = pos.get('type', '')  # 'buy' or 'sell'
                volume = float(pos.get('volume', 0.0))
                close_side = 'sell' if pos_type == 'buy' else 'buy'

                try:
                    result = await strategy.order_executor.close_order(
                        symbol=symbol,
                        side=close_side,
                        quantity=volume,
                        price=current_price,
                        order_type='market',
                    )
                    if result.success:
                        closed_count += 1
                        self.logger.info("[CLOSE-ALL] Closed ticket %s (%s %.4f)", ticket, pos_type, volume)
                    else:
                        errors.append(f"Ticket {ticket}: {result.error}")
                except Exception as e:
                    errors.append(f"Ticket {ticket}: {e}")

            # Clear local position state if we closed everything
            if closed_count > 0 and strategy.current_position:
                await strategy.persistence.async_save_position(None)
                strategy.current_position = None

            self.logger.warning(
                "[CLOSE-ALL] Closed %d/%d positions for %s",
                closed_count, len(positions), symbol,
            )

            result_data = {
                "success": closed_count > 0,
                "closed": closed_count,
                "total": len(positions),
                "errors": errors,
            }
            status = 200 if closed_count > 0 else 500
            return JSONResponse(status_code=status, content=result_data)

        @self.router.get("/trade/position")
        async def get_position():
            """Return current position info for the manual trade panel."""
            strategy = self.trading_strategy
            if not strategy or not strategy.current_position:
                return JSONResponse(content={"has_position": False})
            pos = strategy.current_position
            return JSONResponse(content={
                "has_position": True,
                "direction": pos.direction,
                "symbol": pos.symbol,
                "entry_price": pos.entry_price,
                "stop_loss": pos.stop_loss,
                "take_profit": pos.take_profit,
                "quantity": pos.size,
            })

        @self.router.get("/trade/suggestion")
        async def get_trade_suggestion():
            """Return AI-recommended SL/TP/signal from last analysis."""
            return await self._get_ai_suggestion()

    def _get_exchange(self):
        """Get the current exchange instance from exchange_manager or strategy."""
        if self.exchange_manager:
            for ex in getattr(self.exchange_manager, 'exchanges', {}).values():
                return ex
        if self.trading_strategy and hasattr(self.trading_strategy, 'order_executor'):
            executor = self.trading_strategy.order_executor
            if executor and hasattr(executor, 'exchange'):
                return executor.exchange
        return None

    async def _get_current_price(self, symbol: str) -> float:
        """Get real current price: dashboard_state > exchange ticker > fallback 0."""
        # 1. Dashboard state (updated by price stream)
        ds_price = getattr(self.dashboard_state, 'current_price', None)
        if ds_price and ds_price > 0:
            return float(ds_price)

        # 2. Fresh ticker from exchange
        exchange = self._get_exchange()
        if exchange and hasattr(exchange, 'fetch_ticker'):
            try:
                ticker = await exchange.fetch_ticker(symbol)
                price = float(ticker.get('last') or ticker.get('close') or 0)
                if price > 0:
                    return price
            except Exception as e:
                self.logger.debug("Could not fetch ticker for %s: %s", symbol, e)

        return 0.0

    async def _execute_manual_trade(self, signal: str, req: ManualTradeRequest):
        """Execute a manual BUY or SELL trade."""
        strategy = self.trading_strategy
        if not strategy:
            return JSONResponse(status_code=503, content={"error": "Trading strategy not available"})
        if strategy.current_position:
            return JSONResponse(status_code=400, content={"error": "Position already open — close first"})

        symbol = self.config.CRYPTO_PAIR

        # Get real current price from exchange/dashboard
        current_price = await self._get_current_price(symbol)
        if current_price <= 0:
            # Last resort: mid-price from SL/TP
            current_price = (req.stop_loss + req.take_profit) / 2

        self.logger.warning(
            "[MANUAL] Dashboard %s requested: SL=%.5f, TP=%.5f, price=%.5f",
            signal, req.stop_loss, req.take_profit, current_price,
        )

        decision = await strategy.manual_open_position(
            signal=signal,
            current_price=current_price,
            stop_loss=req.stop_loss,
            take_profit=req.take_profit,
            symbol=symbol,
            volume=req.volume,
        )
        if decision:
            return JSONResponse(content={
                "success": True,
                "action": signal,
                "symbol": symbol,
                "price": decision.price,
                "stop_loss": decision.stop_loss,
                "take_profit": decision.take_profit,
            })
        return JSONResponse(status_code=500, content={"error": "Trade execution failed"})

    async def _get_ai_suggestion(self):
        """Extract AI-recommended SL/TP/signal from last analysis in previous_response.json."""
        try:
            data_dir = getattr(self.config, 'DATA_DIR', 'data')
            prev_file = Path(data_dir) / "trading" / "previous_response.json"
            if not prev_file.exists():
                return JSONResponse(content={"has_suggestion": False})

            data = json.loads(prev_file.read_text(encoding='utf-8'))
            text = data.get("response", {}).get("text_analysis", "")
            if not text:
                return JSONResponse(content={"has_suggestion": False})

            # Extract JSON block from AI response
            json_match = re.search(r'```json\s*(.*?)\s*```', text, re.DOTALL | re.IGNORECASE)
            if not json_match:
                return JSONResponse(content={"has_suggestion": False})

            analysis = json.loads(json_match.group(1))
            # Unwrap "analysis" key if present
            if "analysis" in analysis and isinstance(analysis["analysis"], dict):
                analysis = analysis["analysis"]

            signal = analysis.get("signal", "HOLD")
            stop_loss = analysis.get("stop_loss")
            take_profit = analysis.get("take_profit")
            entry_price = analysis.get("entry_price")
            confidence = analysis.get("confidence", 0)

            if stop_loss is None or take_profit is None:
                return JSONResponse(content={"has_suggestion": False})

            def _safe_float(val):
                """Coerce to float, unwrapping list/tuple if the LLM returned one."""
                if isinstance(val, (list, tuple)):
                    val = next((v for v in val if isinstance(v, (int, float))), None)
                if val is None:
                    return None
                return float(val)

            position_size = analysis.get("position_size")
            rating = analysis.get("rating", "")
            reasoning = analysis.get("reasoning", "")
            rr_ratio = analysis.get("risk_reward_ratio")
            notes = analysis.get("notes", {})
            trend = analysis.get("trend", {})

            return JSONResponse(content={
                "has_suggestion": True,
                "signal": signal,
                "stop_loss": _safe_float(stop_loss),
                "take_profit": _safe_float(take_profit),
                "entry_price": _safe_float(entry_price) if entry_price else None,
                "confidence": confidence,
                "position_size": _safe_float(position_size) if position_size else None,
                "rating": rating,
                "reasoning": reasoning,
                "risk_reward_ratio": _safe_float(rr_ratio) if rr_ratio else None,
                "trend_direction": trend.get("direction", "") if isinstance(trend, dict) else "",
                "setup_type": notes.get("setup_type", "") if isinstance(notes, dict) else "",
            })
        except Exception as e:
            self.logger.debug("Could not extract AI suggestion: %s", e)
            return JSONResponse(content={"has_suggestion": False})
