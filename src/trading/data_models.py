"""Dataclasses for trading system."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any


from src.utils.data_utils import SerializableMixin


@dataclass(slots=True)
class Position(SerializableMixin):

    """Represents an active trading position.

    Includes confluence_factors from entry for brain learning on close.
    """
    entry_price: float
    stop_loss: float
    take_profit: float
    size: float  # Quantity in base currency (e.g., BTC)
    entry_time: datetime
    confidence: str  # HIGH, MEDIUM, LOW
    direction: str   # LONG, SHORT
    symbol: str
    # Confluence factors at entry time for factor performance learning
    # Stored as tuple of (name, score) pairs for frozen dataclass compatibility
    confluence_factors: tuple = field(default_factory=tuple)
    # Transaction fee paid at entry (in USDT)
    entry_fee: float = 0.0
    quote_amount: float = 0.0   # Invested annual quote currency (e.g. USDT)
    # AI's suggested position size as percentage of capital (0.0-1.0)
    size_pct: float = 0.0
    # Market conditions at entry for Brain learning
    atr_at_entry: float = 0.0           # ATR value when position opened
    volatility_level: str = "MEDIUM"    # HIGH, MEDIUM, LOW (derived from ATR%)
    sl_distance_pct: float = 0.0        # abs(entry - SL) / entry as decimal
    tp_distance_pct: float = 0.0        # abs(TP - entry) / entry as decimal
    rr_ratio_at_entry: float = 0.0      # tp_distance / sl_distance
    adx_at_entry: float = 0.0           # ADX value at entry time
    rsi_at_entry: float = 50.0          # RSI value at entry time for threshold learning
    # Extended market snapshot at entry for full brain context reconstruction
    trend_direction_at_entry: str = "NEUTRAL"      # BULLISH/BEARISH/NEUTRAL
    macd_signal_at_entry: str = "NEUTRAL"           # BULLISH/BEARISH/NEUTRAL
    bb_position_at_entry: str = "MIDDLE"            # UPPER/MIDDLE/LOWER
    volume_state_at_entry: str = "NORMAL"           # ACCUMULATION/NORMAL/DISTRIBUTION
    market_sentiment_at_entry: str = "NEUTRAL"      # EXTREME_FEAR/FEAR/NEUTRAL/GREED/EXTREME_GREED
    # Performance metrics (MAE/MFE)
    max_drawdown_pct: float = 0.0       # Max adverse excursion (MAE)
    max_profit_pct: float = 0.0         # Max favorable excursion (MFE)
    # Multi-position source tag: 'ai' (LLM decision) or 'fast' (algo consensus).
    # Kept last + with default to preserve backward-compat with older JSON.
    source: str = "ai"

    def calculate_pnl(self, current_price: float) -> float:
        """Calculate unrealized P&L percentage."""
        if self.direction == 'LONG':
            return ((current_price - self.entry_price) / self.entry_price) * 100
        else:  # SHORT
            return ((self.entry_price - current_price) / self.entry_price) * 100

    def update_metrics(self, current_price: float) -> None:
        """Update live performance metrics (MAE/MFE)."""
        pnl = self.calculate_pnl(current_price)

        # Update Maximum Adverse Excursion (lowest negative P&L)
        if pnl < 0 and pnl < self.max_drawdown_pct:
            self.max_drawdown_pct = pnl

        # Update Maximum Favorable Excursion (highest positive P&L)
        if pnl > 0 and pnl > self.max_profit_pct:
            self.max_profit_pct = pnl

    def calculate_closing_fee(self, close_price: float, fee_percent: float) -> float:
        """Calculate the transaction fee for closing this position.

        Args:
            close_price: Price at which position is closed
            fee_percent: Fee percentage (default 0.075% for limit orders)

        Returns:
            Fee amount in USDT
        """
        return close_price * self.size * fee_percent

    def is_stop_hit(self, current_price: float) -> bool:
        """Check if stop loss is hit."""
        if self.direction == 'LONG':
            return current_price <= self.stop_loss
        else:
            return current_price >= self.stop_loss

    def is_target_hit(self, current_price: float) -> bool:
        """Check if take profit is hit."""
        if self.direction == 'LONG':
            return current_price >= self.take_profit
        else:
            return current_price <= self.take_profit


@dataclass(slots=True)
class TradeDecision(SerializableMixin):
    """Represents a trading decision from the AI."""
    timestamp: datetime
    symbol: str
    action: str  # BUY, SELL, HOLD, CLOSE, CLOSE_LONG, CLOSE_SHORT, UPDATE
    confidence: str  # HIGH, MEDIUM, LOW
    price: float
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    position_size: float = 0.0  # AI's suggested percentage of capital (0.0-1.0)
    quote_amount: float = 0.0   # Invested quote currency amount (e.g. USDT)
    quantity: float = 0.0  # Actual quantity in base currency (e.g., BTC)
    fee: float = 0.0  # Transaction fee in quote currency (e.g. USDT)
    reasoning: str = ""
    # 5-tier rating: BUY, OVERWEIGHT, HOLD, UNDERWEIGHT, SELL
    rating: str = ""
    # Debate outcome (if debate service is enabled)
    debate_verdict: Optional[str] = None
    debate_confidence_delta: float = 0.0  # Shift in confidence after debate
    # Slot that owns this decision — "ai" (LLM) or "fast" (algo). Used by
    # downstream handlers to look up the exact position without relying on
    # the current_position compat-shim (which only returns one slot).
    source: str = "ai"


# 5-tier rating constants for clarity
class Rating:
    """Five-tier rating scale inspired by institutional research."""
    BUY = "BUY"              # Strong conviction to enter or add
    OVERWEIGHT = "OVERWEIGHT"  # Favorable, gradually increase
    HOLD = "HOLD"            # Maintain, no action
    UNDERWEIGHT = "UNDERWEIGHT"  # Reduce exposure
    SELL = "SELL"            # Exit or avoid

    # Mapping from rating to action for backward compatibility
    RATING_TO_ACTION = {
        BUY: "BUY",
        OVERWEIGHT: "BUY",
        HOLD: "HOLD",
        UNDERWEIGHT: "SELL",
        SELL: "SELL",
    }

    # Mapping from action+confidence to rating
    ACTION_TO_RATING = {
        ("BUY", "HIGH"): BUY,
        ("BUY", "MEDIUM"): OVERWEIGHT,
        ("BUY", "LOW"): OVERWEIGHT,
        ("SELL", "HIGH"): SELL,
        ("SELL", "MEDIUM"): UNDERWEIGHT,
        ("SELL", "LOW"): UNDERWEIGHT,
        ("HOLD", "HIGH"): HOLD,
        ("HOLD", "MEDIUM"): HOLD,
        ("HOLD", "LOW"): HOLD,
    }

    ALL = [BUY, OVERWEIGHT, HOLD, UNDERWEIGHT, SELL]


@dataclass(slots=True)
class DebateArgument(SerializableMixin):
    """A single argument from a debate participant."""
    participant: str  # "bull" or "bear"
    argument: str
    key_points: tuple = field(default_factory=tuple)
    confidence_impact: float = 0.0  # -1.0 to +1.0 shift


@dataclass(slots=True)
class DebateResult(SerializableMixin):
    """Result of a Bull/Bear debate on a trading decision."""
    original_signal: str
    original_confidence: str
    bull_arguments: tuple = field(default_factory=tuple)  # Tuple of DebateArgument
    bear_arguments: tuple = field(default_factory=tuple)  # Tuple of DebateArgument
    verdict: str = ""  # Final verdict: BULL_WINS, BEAR_WINS, NEUTRAL
    final_signal: str = ""  # Signal after debate
    final_confidence: str = ""  # Confidence after debate
    confidence_delta: float = 0.0  # Net confidence shift
    summary: str = ""  # Human-readable debate summary


@dataclass(slots=True)
class TradingMemory(SerializableMixin):
    """Rolling memory of recent trading decisions for context."""
    decisions: List[TradeDecision] = field(default_factory=list)
    max_decisions: int = 10

    def add_decision(self, decision: TradeDecision) -> None:
        """Add a decision to memory, maintaining max size."""
        self.decisions.append(decision)
        if len(self.decisions) > self.max_decisions:
            self.decisions.pop(0)

    def get_recent_decisions(self, n: int = 5) -> List[TradeDecision]:
        """Get the n most recent decisions."""
        return self.decisions[-n:]

    def get_context_summary(self, full_history: Optional[List['TradeDecision']] = None) -> str:
        """Generate a concise summary for prompt injection.

        Args:
            full_history: Complete trade history for calculating overall performance.
                          When explicitly passed (even if empty), takes priority over self.decisions.

        Returns:
            Formatted summary of last 5 decisions with overall P&L data from all trades
        """
        # When full_history is explicitly provided (including empty []), use it exclusively.
        # Only fall back to self.decisions when full_history is None (not provided).
        if full_history is not None:
            if not full_history:
                return "No previous trading decisions for this symbol."
            recent_source = full_history
            history_to_analyze = full_history
        else:
            if not self.decisions:
                return "No previous trading decisions."
            recent_source = self.decisions
            history_to_analyze = self.decisions

        recent = recent_source[-5:]  # Last 5 decisions for context
        lines = []
        if recent:
            lines.append("## Recent Trading History (Last 5 Decisions):")

        # Calculate P&L from FULL trade history, not just recent decisions
        # Helper to ensure timezone-aware timestamps for sorting
        def _ensure_utc(dt: datetime) -> datetime:
            if dt.tzinfo is None:
                return dt.replace(tzinfo=timezone.utc)
            return dt
        # Ensure chronological order for P&L calculation (handle mixed tz-aware/naive)
        history_to_analyze = sorted(history_to_analyze, key=lambda x: _ensure_utc(x.timestamp))
        total_pnl_quote = 0.0
        total_pnl_pct = 0.0
        closed_trades = 0
        winning_trades = 0

        # Track open positions to calculate P&L across entire history
        open_position = None
        for decision in history_to_analyze:
            if decision.action in ['BUY', 'SELL']:
                open_position = decision
            elif decision.action in ['CLOSE', 'CLOSE_LONG', 'CLOSE_SHORT'] and open_position:
                # Calculate P&L for closed trade
                if open_position.action == 'BUY':
                    pnl_pct = ((decision.price - open_position.price) / open_position.price) * 100
                    pnl_quote = (decision.price - open_position.price) * open_position.quantity
                else:  # SELL
                    pnl_pct = ((open_position.price - decision.price) / open_position.price) * 100
                    pnl_quote = (open_position.price - decision.price) * open_position.quantity

                total_pnl_quote += pnl_quote
                total_pnl_pct += pnl_pct
                closed_trades += 1
                if pnl_pct > 0:
                    winning_trades += 1
                open_position = None
        
        # Format each recent decision for context
        for decision in recent:
            time_str = decision.timestamp.strftime("%Y-%m-%d %H:%M")
            # Keep full reasoning for better AI context (no truncation)
            lines.append(
                f"- [{time_str}] {decision.action} @ ${decision.price:,.2f} "
                f"(Conf: {decision.confidence}) - {decision.reasoning}"
            )

        # Add overall performance summary from ALL closed trades
        if closed_trades > 0:
            avg_pnl_pct = total_pnl_pct / closed_trades
            win_rate = (winning_trades / closed_trades) * 100
            lines.append("")
            lines.append(f"## Overall Performance ({closed_trades} Total Closed Trades):")
            lines.append(f"- Total P&L: ${total_pnl_quote:+,.2f} ({total_pnl_pct:+.2f}%)")
            lines.append(f"- Average P&L per Trade: {avg_pnl_pct:+.2f}%")
            lines.append(f"- Win Rate: {win_rate:.1f}% ({winning_trades}/{closed_trades} trades)")

        return "\n".join(lines)

    def to_list(self) -> List[Dict[str, Any]]:
        """Convert to list of dictionaries for JSON serialization."""
        return [d.to_dict() for d in self.decisions]

    @classmethod
    def from_list(cls, data: List[Dict[str, Any]], max_decisions: int = 10) -> 'TradingMemory':
        """Create TradingMemory from list of dictionaries."""
        memory = cls(max_decisions=max_decisions)
        for item in data:
            memory.decisions.append(TradeDecision.from_dict(item))
        return memory


@dataclass(slots=True)
class VectorSearchResult(SerializableMixin):
    """Represents a search result from VectorMemory."""
    id: str
    document: str
    similarity: float
    recency: float
    hybrid_score: float
    metadata: Dict[str, Any]


@dataclass(slots=True)
class BrokerConstraints(SerializableMixin):
    """Broker-side execution constraints for a symbol.

    Used by the risk manager to compute leverage-aware position sizing and
    to detect when a requested trade falls below broker minimums.

    leverage:
        Effective account leverage. 1.0 for spot (Binance spot, ETF, etc.),
        up to 500 for retail MT5 forex. Used to scale notional exposure vs
        free margin.
    contract_size:
        Units of base currency per lot/unit. 1.0 for crypto spot, 100000 for
        major FX, 100 for XAUUSD, 10 for WTI/BRENT CFD (varies).
    min_volume / volume_step / max_volume:
        Broker granularity. MT5 uses lots (0.01 step typical); CCXT exposes
        this via markets[symbol]['limits']['amount'].
    min_notional:
        Minimum dollar value of an order (Binance MIN_NOTIONAL filter, etc.).
        Optional — many brokers only enforce min_volume.
    account_currency:
        e.g. "USD", "EUR", "USDC". For margin/balance conversion.
    """
    symbol: str
    leverage: float = 1.0
    contract_size: float = 1.0
    min_volume: float = 0.0
    volume_step: float = 0.0
    max_volume: float = 0.0
    min_notional: float = 0.0
    account_currency: str = "USD"


@dataclass(slots=True)
class RiskAssessment(SerializableMixin):
    """Represents the calculated risk parameters for a trade."""
    direction: str
    entry_price: float
    stop_loss: float
    take_profit: float
    quantity: float
    size_pct: float
    quote_amount: float
    entry_fee: float
    sl_distance_pct: float
    tp_distance_pct: float
    rr_ratio: float
    volatility_level: str
    # --- Leverage / broker-aware fields (optional, populated when a
    # BrokerConstraints was provided to the risk manager) -----------------
    notional: float = 0.0              # lots * contract_size * price
    lots: float = 0.0                  # Broker-native size (may equal quantity)
    leverage_used: float = 1.0
    margin_required: float = 0.0       # notional / leverage_used
    # Set when the computed size could not be placed as-is. Holds a
    # human-readable hypothesis the caller can log or surface in the UI.
    sizing_warning: Optional[str] = None
    # Structured capital-top-up suggestion. Keys (when present):
    #   capital_needed_min, capital_needed_target, capital_currency,
    #   expected_gain_at_tp, expected_loss_at_sl, min_lot_notional
    capital_suggestion: Optional[Dict[str, float]] = None
    # True if the trade is executable with current capital; False if the
    # caller should abort (size below broker minimum beyond tolerance).
    executable: bool = True


@dataclass(slots=True)
class ConfidenceLevelStats(SerializableMixin):
    """Statistics for a single confidence level (HIGH/MEDIUM/LOW)."""
    win_rate: float
    avg_pnl: float
    total_trades: int


@dataclass(slots=True)
class ADXBucketStats(SerializableMixin):
    """Performance statistics for an ADX range bucket."""
    bucket: str  # e.g., "0-20", "20-40"
    win_rate: float
    avg_pnl: float
    total_trades: int


@dataclass(slots=True)
class FactorPerformance(SerializableMixin):
    """Performance metrics for a confluence factor."""
    factor_name: str
    win_rate: float
    avg_score: float
    sample_size: int


@dataclass(slots=True)
class SemanticRule(SerializableMixin):
    """A semantic trading rule learned from trade clusters."""
    rule_id: str
    rule_text: str
    win_rate: Optional[float] = None
    source_trades: Optional[int] = None
    created_at: Optional[datetime] = None
    similarity: float = 0.0


@dataclass(slots=True)
class ClosedTradeResult(SerializableMixin):
    """Result of a closed trade for statistics calculation."""
    entry_price: float
    exit_price: float
    pnl_pct: float
    pnl_quote: float
    quantity: float
    direction: str  # LONG, SHORT


@dataclass(slots=True)
class TokenUsageStats(SerializableMixin):
    """Token usage statistics from a single API request."""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost: Optional[float] = None


@dataclass(slots=True)
class SessionCosts(SerializableMixin):
    """Cumulative session costs by provider."""
    openrouter: float = 0.0
    google: float = 0.0
    lmstudio: float = 0.0

    @property
    def total(self) -> float:
        """Get total cost across all providers."""
        return self.openrouter + self.google + self.lmstudio


@dataclass(slots=True)
class ProviderCostStats(SerializableMixin):
    """Persistent cost statistics for a single provider."""
    total_cost: float = 0.0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
