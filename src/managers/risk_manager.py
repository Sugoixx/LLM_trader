"""Risk Manager for converting signals into actionable trade parameters."""

from typing import Optional, Dict, Any, TYPE_CHECKING
from src.logger.logger import Logger
from src.contracts.risk_contract import RiskManagerProtocol

if TYPE_CHECKING:
    from src.config.protocol import ConfigProtocol
    from src.trading.data_models import RiskAssessment, BrokerConstraints


class RiskManager(RiskManagerProtocol):
    """
    Manages risk calculations including position sizing, stop-loss/take-profit dynamic adjustment,
    and circuit breakers.
    """

    def __init__(self, logger: Logger, config: "ConfigProtocol"):
        self.logger = logger
        self.config = config

    def validate_signal(self, signal: str) -> bool:
        """Validate if a signal is actionable."""
        return signal in ("BUY", "SELL", "CLOSE", "CLOSE_LONG", "CLOSE_SHORT")

    def calculate_entry_parameters(
        self,
        signal: str,
        current_price: float,
        capital: float,
        confidence: str,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        position_size: Optional[float] = None,
        market_conditions: Optional[Dict[str, Any]] = None,
        broker_constraints: Optional["BrokerConstraints"] = None,
    ) -> "RiskAssessment":
        """
        Calculate all risk parameters for a new position entry.
        """
        from src.trading.data_models import RiskAssessment

        market_conditions = market_conditions or {}
        direction = "LONG" if signal == "BUY" else "SHORT"

        # 0. Per-symbol min SL override from config (e.g. CRUDOIL=0.020)
        symbol = market_conditions.get("symbol", "")
        min_sl_override = 0.0
        raw_map = self.config.get_config("safety", "min_sl_per_symbol", "")
        if raw_map and symbol:
            for entry in raw_map.split(","):
                entry = entry.strip()
                if "=" in entry:
                    sym, val = entry.split("=", 1)
                    if sym.strip().upper() == symbol.upper():
                        try:
                            min_sl_override = float(val.strip())
                        except ValueError:
                            pass

        # 1. Extract or Default ATR/Volatility
        atr = market_conditions.get("atr", current_price * 0.02)
        atr_pct = market_conditions.get("atr_percentage", (atr / current_price) * 100)

        # Determine volatility level
        if atr_pct > 3:
            volatility_level = "HIGH"
        elif atr_pct < 1.5:
            volatility_level = "LOW"
        else:
            volatility_level = "MEDIUM"

        # 2. Dynamic SL/TP Calculation (Dynamic Defaults)
        # Use 2x ATR for SL, 4x ATR for TP (2:1 R/R default)
        dynamic_sl_distance = atr * 2
        dynamic_tp_distance = atr * 4

        if direction == "LONG":
            dynamic_sl = current_price - dynamic_sl_distance
            dynamic_tp = current_price + dynamic_tp_distance
        else:  # SHORT
            dynamic_sl = current_price + dynamic_sl_distance
            dynamic_tp = current_price - dynamic_tp_distance

        # 3. Resolve Final SL/TP (AI vs Dynamic)
        if stop_loss and stop_loss > 0:
            final_sl = stop_loss
            self.logger.debug("Using AI-provided SL: $%s", f"{final_sl:,.2f}")
        else:
            final_sl = dynamic_sl
            self.logger.info("Using dynamic SL (2x ATR): $%s", f"{final_sl:,.2f}")

        if take_profit and take_profit > 0:
            final_tp = take_profit
            self.logger.debug("Using AI-provided TP: $%s", f"{final_tp:,.2f}")
        else:
            final_tp = dynamic_tp
            self.logger.info("Using dynamic TP (4x ATR): $%s", f"{final_tp:,.2f}")

        # Trailing stop calculation (1.5x ATR, tighter than initial SL)
        if direction == "LONG":
            trailing_distance = atr * 1.5
        elif direction == "SHORT":
            trailing_distance = atr * 1.5
        else:
            trailing_distance = atr * 1.5

        # 4. Circuit Breakers (Clamp Extreme Values)
        sl_distance_raw = abs(current_price - final_sl) / current_price

        # Clamp SL: min (per-symbol override or 0.5%), max 10%
        min_sl_pct = min_sl_override if min_sl_override > 0 else 0.005
        if sl_distance_raw > 0.10:
            self.logger.warning(
                "SL distance %s exceeds 10%% max, clamping", f"{sl_distance_raw:.1%}"
            )
            if direction == "LONG":
                final_sl = current_price * 0.90
            else:
                final_sl = current_price * 1.10
        elif sl_distance_raw < min_sl_pct:
            self.logger.warning(
                "SL distance %s below %.1f%% min (symbol override=%s), expanding",
                f"{sl_distance_raw:.1%}",
                min_sl_pct * 100,
                min_sl_override,
            )
            if direction == "LONG":
                final_sl = current_price * (1 - min_sl_pct)
            else:
                final_sl = current_price * (1 + min_sl_pct)

        # Validate Logical Consistency
        if direction == "LONG":
            if final_sl >= current_price:
                self.logger.warning(
                    "Invalid SL for LONG (%s >= %s), using dynamic",
                    final_sl,
                    current_price,
                )
                final_sl = dynamic_sl
            if final_tp <= current_price:
                self.logger.warning(
                    "Invalid TP for LONG (%s <= %s), using dynamic",
                    final_tp,
                    current_price,
                )
                final_tp = dynamic_tp
        else:  # SHORT
            if final_sl <= current_price:
                self.logger.warning(
                    "Invalid SL for SHORT (%s <= %s), using dynamic",
                    final_sl,
                    current_price,
                )
                final_sl = dynamic_sl
            if final_tp >= current_price:
                self.logger.warning(
                    "Invalid TP for SHORT (%s >= %s), using dynamic",
                    final_tp,
                    current_price,
                )
                final_tp = dynamic_tp

        # 5. Position Sizing
        if position_size and position_size > 0:
            final_size_pct = position_size
        else:
            # Volatility-adjusted position sizing
            confidence_map = {"HIGH": 0.03, "MEDIUM": 0.02, "LOW": 0.01}
            base_size = confidence_map.get(confidence.upper(), 0.02)

            # Adjust based on volatility (ATR%)
            if atr_pct > 4:  # Very volatile (oil, altcoins)
                vol_factor = 0.6
            elif atr_pct > 2:
                vol_factor = 0.8
            else:
                vol_factor = 1.0

            # Adjust based on asset
            symbol = market_conditions.get("symbol", "").upper()
            if "OIL" in symbol or "CRUDE" in symbol:
                asset_factor = 0.7  # More prudent on oil
            elif "BTC" in symbol:
                asset_factor = 1.0
            else:  # Other riskier cryptos
                asset_factor = 0.8

            final_size_pct = base_size * vol_factor * asset_factor
            self.logger.info(
                "Using volatility-adjusted size: %.1f%% (base=%.1f%%, vol=%.1f, asset=%.1f)",
                final_size_pct * 100,
                base_size * 100,
                vol_factor,
                asset_factor,
            )

        # 6. Calculate Financials
        allocation = capital * final_size_pct
        quantity = allocation / current_price
        entry_fee = allocation * self.config.TRANSACTION_FEE_PERCENT

        # 7. Metrics
        sl_distance_pct = abs(current_price - final_sl) / current_price
        tp_distance_pct = abs(final_tp - current_price) / current_price
        rr_ratio = tp_distance_pct / sl_distance_pct if sl_distance_pct > 0 else 0

        # 8. Broker-aware sizing (leverage, min lot, capital suggestion)
        notional = allocation
        lots = quantity
        leverage_used = 1.0
        margin_required = allocation
        sizing_warning: Optional[str] = None
        capital_suggestion: Optional[Dict[str, Any]] = None
        executable = True

        if broker_constraints is not None:
            leverage_used = max(1.0, float(broker_constraints.leverage or 1.0))
            contract = float(broker_constraints.contract_size or 1.0) or 1.0
            vol_step = float(broker_constraints.volume_step or 0.0)
            vol_min = float(broker_constraints.min_volume or 0.0)
            min_notional = float(broker_constraints.min_notional or 0.0)

            # With leverage, the risk budget (equity × size_pct) buys
            # `leverage` times as much notional exposure. For spot
            # (leverage=1) this reduces to the legacy behaviour.
            target_notional = allocation * leverage_used
            target_units = target_notional / current_price
            target_lots = target_units / contract if contract > 0 else target_units

            # Snap DOWN to broker's volume_step (same as MT5 side)
            if vol_step > 0:
                steps = int(target_lots / vol_step)
                snapped_lots = round(steps * vol_step, 10)
            else:
                snapped_lots = target_lots

            # Broker minimum lot constraint
            min_lot_notional = max(vol_min, 0.0) * contract * current_price
            min_lot_hit = vol_min > 0 and snapped_lots < vol_min
            min_notional_hit = (
                min_notional > 0
                and snapped_lots * contract * current_price < min_notional
            )

            if min_lot_hit or min_notional_hit:
                # The broker won't accept a trade this small. Compute what
                # would be needed to place at least the minimum lot, AND
                # what it would cost/yield if the user topped up.
                required_lot = max(vol_min, 0.0)
                if min_notional_hit and contract * current_price > 0:
                    required_lot = max(
                        required_lot,
                        min_notional / (contract * current_price),
                    )
                if vol_step > 0 and required_lot % vol_step > 0:
                    required_lot = round(
                        (int(required_lot / vol_step) + 1) * vol_step, 10
                    )

                required_notional = required_lot * contract * current_price
                required_margin = required_notional / leverage_used

                # ── Small-account rescue: auto-snap UP to broker minimum ──
                # When the required margin is a small fraction of available
                # capital, trading the broker minimum is safer than refusing
                # the signal entirely. Controlled by MAX_MIN_LOT_MARGIN_PCT
                # (0 disables, default 10%).
                max_margin_pct = float(
                    getattr(self.config, "MAX_MIN_LOT_MARGIN_PCT", 0.0) or 0.0
                )
                can_promote = (
                    max_margin_pct > 0.0
                    and capital > 0
                    and required_margin <= capital * max_margin_pct
                )
                if can_promote:
                    snapped_lots = required_lot
                    lots = snapped_lots
                    quantity = snapped_lots * contract
                    notional = required_notional
                    margin_required = required_margin
                    allocation = margin_required  # for dashboard display
                    # Reflect the promoted sizing so downstream stats are honest
                    final_size_pct = (
                        margin_required / capital if capital > 0 else final_size_pct
                    )
                    entry_fee = notional * self.config.TRANSACTION_FEE_PERCENT
                    executable = True
                    sizing_warning = (
                        f"Position promoted UP to broker minimum "
                        f"({required_lot:.4f} lots = ${required_notional:,.2f} "
                        f"notional, margin ${required_margin:,.2f} = "
                        f"{(required_margin / capital * 100):.2f}% of capital, "
                        f"within {max_margin_pct * 100:.0f}% cap)."
                    )
                    self.logger.info(
                        "Risk manager promoted sizing to broker minimum for small "
                        "account: %.4f lots, margin $%.2f (%.2f%% of capital, cap %.0f%%).",
                        required_lot,
                        required_margin,
                        required_margin / capital * 100 if capital > 0 else 0.0,
                        max_margin_pct * 100,
                    )
                else:
                    # Capital needed to let risk manager allocate this margin
                    # at the current size_pct. E.g. if size_pct=2% and margin
                    # needed=50$, required capital = 2500$.
                    cap_needed_target = (
                        required_margin / final_size_pct
                        if final_size_pct > 0
                        else required_margin
                    )
                    cap_top_up = max(0.0, cap_needed_target - capital)

                    # Hypothetical outcomes at that size
                    expected_gain = required_notional * tp_distance_pct
                    expected_loss = required_notional * sl_distance_pct

                    capital_suggestion = {
                        "capital_needed_total": round(cap_needed_target, 2),
                        "capital_top_up": round(cap_top_up, 2),
                        "required_lots": round(required_lot, 4),
                        "required_notional": round(required_notional, 2),
                        "required_margin": round(required_margin, 2),
                        "expected_gain_at_tp": round(expected_gain, 2),
                        "expected_loss_at_sl": round(expected_loss, 2),
                        "leverage": leverage_used,
                        "min_lot_notional": round(min_lot_notional, 2),
                    }
                    margin_cap_note = ""
                    if max_margin_pct > 0 and capital > 0:
                        margin_cap_note = (
                            f" Auto-snap disabled: required margin "
                            f"${required_margin:,.2f} > "
                            f"{max_margin_pct * 100:.0f}% of capital "
                            f"(${capital * max_margin_pct:,.2f})."
                        )
                    sizing_warning = (
                        f"Position ({snapped_lots:.4f} lots, "
                        f"${snapped_lots * contract * current_price:,.2f} notional) "
                        f"is below broker minimum ({required_lot:.4f} lots = "
                        f"${required_notional:,.2f}). "
                        f"Top up ~${cap_top_up:,.2f} to enable this trade. "
                        f"Hypothesis: +${expected_gain:,.2f} at TP / "
                        f"-${expected_loss:,.2f} at SL."
                        f"{margin_cap_note}"
                    )
                    executable = False

                    # Keep legacy fields sensible even on refusal
                    lots = snapped_lots
                    quantity = snapped_lots * contract
                    notional = snapped_lots * contract * current_price
                    margin_required = notional / leverage_used
            else:
                lots = snapped_lots
                quantity = snapped_lots * contract
                notional = quantity * current_price
                margin_required = notional / leverage_used
                allocation = margin_required  # for dashboard display
                entry_fee = notional * self.config.TRANSACTION_FEE_PERCENT

        return RiskAssessment(
            direction=direction,
            entry_price=current_price,
            stop_loss=final_sl,
            take_profit=final_tp,
            quantity=quantity,
            size_pct=final_size_pct,
            quote_amount=allocation,
            entry_fee=entry_fee,
            sl_distance_pct=sl_distance_pct,
            tp_distance_pct=tp_distance_pct,
            rr_ratio=rr_ratio,
            volatility_level=volatility_level,
            notional=notional,
            lots=lots,
            leverage_used=leverage_used,
            margin_required=margin_required,
            sizing_warning=sizing_warning,
            capital_suggestion=capital_suggestion,
            executable=executable,
        )
