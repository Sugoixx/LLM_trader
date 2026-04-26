"""Binance trade history analyzer.

Fetches raw fills from Binance via CCXT, reconstructs positions using
FIFO or weighted-average cost (WAC), and computes:
  - Realized PNL (gross + net after fees), per trade and cumulative
  - Unrealized (latent) PNL for any currently open positions
  - Win rate
  - Max drawdown / average drawdown

Usage — as a library:
    async with BinanceHistoryAnalyzer.from_env() as ana:
        report = await ana.run("BTC/USDT", method="fifo")
        print(report.summary())

Usage — CLI:
    python -m src.trading.binance_history_analyzer BTC/USDT --method fifo --days 30
"""

from __future__ import annotations

import asyncio
import os
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Literal, Optional, Tuple

import numpy as np

try:
    import ccxt.async_support as ccxt
except ImportError as exc:  # pragma: no cover
    raise ImportError("ccxt is required: pip install ccxt") from exc


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class Fill:
    """A single exchange trade fill."""
    timestamp: datetime
    symbol: str
    side: str           # "buy" | "sell"
    price: float
    qty: float
    fee: float          # fee in fee_currency units
    fee_currency: str
    trade_id: str


@dataclass
class ClosedLeg:
    """A matched entry/exit pair (or partial match)."""
    symbol: str
    direction: str          # "LONG" | "SHORT"
    entry_price: float
    exit_price: float
    qty: float
    entry_time: datetime
    exit_time: datetime
    fee: float              # sum of entry + exit fees (in quote currency)
    pnl_quote: float        # gross PNL (excl. fees)
    pnl_net_quote: float    # net PNL (after fees)
    pnl_pct: float          # net PNL % relative to entry cost
    is_win: bool


@dataclass
class OpenLeg:
    """Remaining open position for one symbol."""
    symbol: str
    direction: str          # "LONG" | "SHORT"
    avg_price: float
    qty: float
    entry_time: datetime
    total_entry_fee: float  # cumulative fees already paid (quote)
    current_price: float = 0.0

    @property
    def unrealized_pnl_quote(self) -> float:
        if self.current_price <= 0 or self.avg_price <= 0 or self.qty <= 0:
            return 0.0
        if self.direction == "LONG":
            gross = (self.current_price - self.avg_price) * self.qty
        else:
            gross = (self.avg_price - self.current_price) * self.qty
        return gross - self.total_entry_fee

    @property
    def unrealized_pnl_pct(self) -> float:
        cost = self.avg_price * self.qty
        if cost <= 0:
            return 0.0
        return self.unrealized_pnl_quote / cost * 100


@dataclass
class HistoryReport:
    """Full analysis report across one or more symbols."""
    symbols: List[str]
    method: str                          # "fifo" | "average"
    closed: List[ClosedLeg] = field(default_factory=list)
    open_legs: List[OpenLeg] = field(default_factory=list)
    # ── metrics (populated by compute()) ──
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    total_pnl_quote: float = 0.0
    total_pnl_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    avg_drawdown_pct: float = 0.0
    best_trade_pct: float = 0.0
    worst_trade_pct: float = 0.0
    total_fees_quote: float = 0.0
    unrealized_pnl_quote: float = 0.0
    initial_capital: float = 0.0

    def compute(self) -> None:
        """Populate metrics from closed legs and open positions."""
        if not self.closed:
            self.unrealized_pnl_quote = sum(
                leg.unrealized_pnl_quote for leg in self.open_legs
            )
            return

        self.total_trades = len(self.closed)
        self.winning_trades = sum(1 for t in self.closed if t.is_win)
        self.losing_trades = self.total_trades - self.winning_trades
        self.win_rate = (
            self.winning_trades / self.total_trades * 100
            if self.total_trades > 0
            else 0.0
        )

        pnl_arr = np.array([t.pnl_net_quote for t in self.closed])
        pct_arr = np.array([t.pnl_pct for t in self.closed])

        self.total_pnl_quote = float(np.sum(pnl_arr))
        self.total_fees_quote = float(sum(t.fee for t in self.closed))
        self.best_trade_pct = float(np.max(pct_arr))
        self.worst_trade_pct = float(np.min(pct_arr))

        # Derive initial capital from cost of very first matched position
        self.initial_capital = self.closed[0].entry_price * self.closed[0].qty
        equity = np.zeros(len(pnl_arr) + 1)
        equity[0] = self.initial_capital
        equity[1:] = self.initial_capital + np.cumsum(pnl_arr)
        self.total_pnl_pct = (
            self.total_pnl_quote / self.initial_capital * 100
            if self.initial_capital > 0
            else 0.0
        )

        # Drawdowns
        peaks = np.maximum.accumulate(equity)
        with np.errstate(divide="ignore", invalid="ignore"):
            dds = np.where(peaks > 0, (equity - peaks) / peaks * 100, 0.0)
        self.max_drawdown_pct = float(np.min(dds))
        neg = dds[dds < 0]
        self.avg_drawdown_pct = float(np.mean(neg)) if len(neg) > 0 else 0.0

        self.unrealized_pnl_quote = sum(
            leg.unrealized_pnl_quote for leg in self.open_legs
        )

    def summary(self) -> str:
        bar = "─" * 60
        lines = [
            bar,
            f"  Binance History Analysis  [{self.method.upper()}]",
            f"  Symbols : {', '.join(self.symbols)}",
            bar,
            f"  Closed trades    : {self.total_trades}",
            f"  Win rate         : {self.win_rate:.1f}%"
            f"  ({self.winning_trades}W / {self.losing_trades}L)",
            f"  Best trade       : {self.best_trade_pct:+.2f}%",
            f"  Worst trade      : {self.worst_trade_pct:+.2f}%",
            f"  Realized PNL     : ${self.total_pnl_quote:+,.4f}"
            f"  ({self.total_pnl_pct:+.2f}%)",
            f"  Total fees paid  : ${self.total_fees_quote:,.4f}",
            f"  Max drawdown     : {self.max_drawdown_pct:.2f}%",
            f"  Avg drawdown     : {self.avg_drawdown_pct:.2f}%",
        ]
        if self.open_legs:
            lines.append(bar)
            lines.append("  Open positions (latent PNL):")
            for leg in self.open_legs:
                lines.append(
                    f"    {leg.symbol:<12} {leg.direction:<6}"
                    f" qty={leg.qty:.6f}"
                    f" avg_entry=${leg.avg_price:,.2f}"
                    f" current=${leg.current_price:,.2f}"
                    f" latent=${leg.unrealized_pnl_quote:+,.4f}"
                    f" ({leg.unrealized_pnl_pct:+.2f}%)"
                )
            lines.append(
                f"  Unrealized total : ${self.unrealized_pnl_quote:+,.4f}"
            )
        lines.append(bar)
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Analyzer
# ---------------------------------------------------------------------------

class BinanceHistoryAnalyzer:
    """Fetch and analyse raw Binance trade fills."""

    _MAX_FILLS_PER_PAGE = 1000  # Binance hard limit

    def __init__(self, exchange: "ccxt.Exchange") -> None:
        self.exchange = exchange

    # ── Construction ───────────────────────────────────────────────────────

    @classmethod
    def from_env(
        cls,
        env_file: str = "keys.env",
        testnet: bool = False,
    ) -> "BinanceHistoryAnalyzer":
        """Build from BINANCE_API_KEY / BINANCE_API_SECRET in *env_file*."""
        # Load env file if it exists (silently skip if not found)
        if os.path.isfile(env_file):
            import dotenv
            dotenv.load_dotenv(env_file, override=False)

        api_key = os.environ.get("BINANCE_API_KEY", "")
        api_secret = os.environ.get("BINANCE_API_SECRET", "")
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
        return cls(exchange)

    # ── Async context manager ──────────────────────────────────────────────

    async def __aenter__(self) -> "BinanceHistoryAnalyzer":
        return self

    async def __aexit__(self, *_) -> None:
        await self.exchange.close()

    # ── Public API ─────────────────────────────────────────────────────────

    async def fetch_fills(
        self,
        symbol: str,
        since: Optional[int] = None,
    ) -> List[Fill]:
        """Return all trade fills for *symbol*, paginating automatically.

        Args:
            symbol: CCXT pair, e.g. ``"BTC/USDT"``.
            since:  Unix timestamp in **milliseconds**; fetch from this point.
        """
        all_fills: List[Fill] = []
        cursor = since
        while True:
            raw = await self.exchange.fetch_my_trades(
                symbol, since=cursor, limit=self._MAX_FILLS_PER_PAGE
            )
            if not raw:
                break
            for t in raw:
                fee_cost = 0.0
                fee_cur = ""
                if t.get("fee"):
                    fee_cost = float(t["fee"].get("cost") or 0)
                    fee_cur = str(t["fee"].get("currency") or "")
                all_fills.append(
                    Fill(
                        timestamp=datetime.fromtimestamp(
                            t["timestamp"] / 1000, tz=timezone.utc
                        ),
                        symbol=t["symbol"],
                        side=str(t["side"]).lower(),
                        price=float(t["price"]),
                        qty=float(t["amount"]),
                        fee=fee_cost,
                        fee_currency=fee_cur,
                        trade_id=str(t["id"]),
                    )
                )
            if len(raw) < self._MAX_FILLS_PER_PAGE:
                break
            # Advance cursor past last timestamp to avoid duplicates
            cursor = raw[-1]["timestamp"] + 1

        all_fills.sort(key=lambda f: (f.timestamp, f.trade_id))
        return all_fills

    async def run(
        self,
        symbols: "str | List[str]",
        method: Literal["fifo", "average"] = "fifo",
        since: Optional[int] = None,
    ) -> HistoryReport:
        """Fetch fills and return a fully computed :class:`HistoryReport`.

        Args:
            symbols: One or more CCXT pairs, e.g. ``"BTC/USDT"`` or
                     ``["BTC/USDT", "ETH/USDT"]``.
            method:  ``"fifo"`` (default) or ``"average"`` (WAC).
            since:   Unix timestamp in milliseconds to start from.
        """
        if isinstance(symbols, str):
            symbols = [symbols]

        all_fills: List[Fill] = []
        for sym in symbols:
            fills = await self.fetch_fills(sym, since=since)
            all_fills.extend(fills)
        all_fills.sort(key=lambda f: (f.timestamp, f.trade_id))

        if method == "average":
            closed, open_map = self._reconstruct_average(all_fills)
        else:
            closed, open_map = self._reconstruct_fifo(all_fills)

        # Fetch current prices for open positions
        open_legs: List[OpenLeg] = []
        for sym, (direction, avg_price, qty, entry_time, entry_fee) in open_map.items():
            current_price = 0.0
            try:
                ticker = await self.exchange.fetch_ticker(sym)
                current_price = float(ticker.get("last") or 0)
            except Exception:
                pass
            open_legs.append(
                OpenLeg(
                    symbol=sym,
                    direction=direction,
                    avg_price=avg_price,
                    qty=qty,
                    entry_time=entry_time,
                    total_entry_fee=entry_fee,
                    current_price=current_price,
                )
            )

        report = HistoryReport(
            symbols=list(symbols),
            method=method,
            closed=closed,
            open_legs=open_legs,
        )
        report.compute()
        return report

    # ── Position reconstruction ────────────────────────────────────────────

    def _fee_in_quote(self, fill: Fill, qty: float) -> float:
        """Approximate fee in quote currency.

        Binance spot often charges in base asset (e.g. BTC). Convert using
        the fill price so all fees are comparable in quote (e.g. USDT).
        """
        fee = fill.fee * (qty / fill.qty) if fill.qty > 0 else fill.fee
        # If fee currency matches the base asset, convert to quote
        base = fill.symbol.split("/")[0] if "/" in fill.symbol else ""
        if fill.fee_currency and fill.fee_currency == base:
            fee = fee * fill.price
        return fee

    def _reconstruct_fifo(
        self, fills: List[Fill]
    ) -> Tuple[List[ClosedLeg], Dict[str, tuple]]:
        """FIFO matching: oldest entry lots are closed first."""
        # Queue per symbol: deque of [price, qty, timestamp, fee_per_qty_in_quote]
        queues: Dict[str, deque] = {}
        # Current side: "LONG" or "SHORT" per symbol
        sides: Dict[str, str] = {}
        closed: List[ClosedLeg] = []

        for fill in fills:
            sym = fill.symbol
            if sym not in queues:
                queues[sym] = deque()
                sides[sym] = "LONG" if fill.side == "buy" else "SHORT"

            direction = sides[sym]
            is_entry = (
                (fill.side == "buy" and direction == "LONG")
                or (fill.side == "sell" and direction == "SHORT")
                or not queues[sym]
            )

            if is_entry:
                # Push entire fill onto the queue
                if not queues[sym]:
                    # Queue was empty — determine direction from this fill
                    sides[sym] = "LONG" if fill.side == "buy" else "SHORT"
                fee_per_qty = self._fee_in_quote(fill, fill.qty) / fill.qty if fill.qty > 0 else 0.0
                queues[sym].append([fill.price, fill.qty, fill.timestamp, fee_per_qty])
            else:
                # Closing existing lots (FIFO)
                remaining = fill.qty
                while remaining > 1e-12 and queues[sym]:
                    entry = queues[sym][0]
                    entry_price, entry_qty, entry_time, entry_fee_per_qty = entry
                    matched = min(remaining, entry_qty)

                    entry_fee = entry_fee_per_qty * matched
                    exit_fee = self._fee_in_quote(fill, matched)

                    if direction == "LONG":
                        gross = (fill.price - entry_price) * matched
                    else:
                        gross = (entry_price - fill.price) * matched

                    net = gross - entry_fee - exit_fee
                    cost = entry_price * matched
                    closed.append(
                        ClosedLeg(
                            symbol=sym,
                            direction=direction,
                            entry_price=entry_price,
                            exit_price=fill.price,
                            qty=matched,
                            entry_time=entry_time,
                            exit_time=fill.timestamp,
                            fee=entry_fee + exit_fee,
                            pnl_quote=gross,
                            pnl_net_quote=net,
                            pnl_pct=(net / cost * 100) if cost > 0 else 0.0,
                            is_win=net > 0,
                        )
                    )

                    if matched < entry_qty:
                        queues[sym][0] = [entry_price, entry_qty - matched, entry_time, entry_fee_per_qty]
                    else:
                        queues[sym].popleft()
                    remaining -= matched

                # Excess quantity → opens a reverse position
                if remaining > 1e-12:
                    new_dir = "LONG" if fill.side == "buy" else "SHORT"
                    sides[sym] = new_dir
                    fee_per_qty = self._fee_in_quote(fill, remaining) / remaining
                    queues[sym].append([fill.price, remaining, fill.timestamp, fee_per_qty])

        # Summarise remaining open lots
        open_map: Dict[str, tuple] = {}
        for sym, q in queues.items():
            if not q:
                continue
            total_qty = sum(item[1] for item in q)
            if total_qty <= 1e-12:
                continue
            avg_price = sum(item[0] * item[1] for item in q) / total_qty
            earliest = q[0][2]
            total_entry_fee = sum(item[3] * item[1] for item in q)
            open_map[sym] = (sides[sym], avg_price, total_qty, earliest, total_entry_fee)

        return closed, open_map

    def _reconstruct_average(
        self, fills: List[Fill]
    ) -> Tuple[List[ClosedLeg], Dict[str, tuple]]:
        """Weighted average cost (WAC) matching: single blended entry price."""
        # State per symbol: [direction, avg_price, total_qty, entry_time, total_entry_fee]
        state: Dict[str, list] = {}
        closed: List[ClosedLeg] = []

        for fill in fills:
            sym = fill.symbol
            if sym not in state:
                state[sym] = [
                    "LONG" if fill.side == "buy" else "SHORT",
                    fill.price,
                    0.0,
                    fill.timestamp,
                    0.0,
                ]

            direction, avg_price, total_qty, entry_time, total_entry_fee = state[sym]

            is_entry = (
                (fill.side == "buy" and direction == "LONG")
                or (fill.side == "sell" and direction == "SHORT")
                or total_qty <= 1e-12
            )

            if is_entry:
                new_qty = total_qty + fill.qty
                if total_qty <= 1e-12:
                    # Fresh position
                    direction = "LONG" if fill.side == "buy" else "SHORT"
                    avg_price = fill.price
                    entry_time = fill.timestamp
                    total_entry_fee = self._fee_in_quote(fill, fill.qty)
                else:
                    avg_price = (avg_price * total_qty + fill.price * fill.qty) / new_qty
                    total_entry_fee += self._fee_in_quote(fill, fill.qty)
                state[sym] = [direction, avg_price, new_qty, entry_time, total_entry_fee]
            else:
                # Closing (partial or full)
                close_qty = min(fill.qty, total_qty)
                proportion = close_qty / total_qty if total_qty > 0 else 1.0

                if direction == "LONG":
                    gross = (fill.price - avg_price) * close_qty
                else:
                    gross = (avg_price - fill.price) * close_qty

                entry_fee_share = total_entry_fee * proportion
                exit_fee = self._fee_in_quote(fill, close_qty)
                net = gross - entry_fee_share - exit_fee
                cost = avg_price * close_qty

                closed.append(
                    ClosedLeg(
                        symbol=sym,
                        direction=direction,
                        entry_price=avg_price,
                        exit_price=fill.price,
                        qty=close_qty,
                        entry_time=entry_time,
                        exit_time=fill.timestamp,
                        fee=entry_fee_share + exit_fee,
                        pnl_quote=gross,
                        pnl_net_quote=net,
                        pnl_pct=(net / cost * 100) if cost > 0 else 0.0,
                        is_win=net > 0,
                    )
                )

                remaining_qty = total_qty - close_qty
                if remaining_qty > 1e-12:
                    remaining_fee = total_entry_fee * (1 - proportion)
                    state[sym] = [direction, avg_price, remaining_qty, entry_time, remaining_fee]
                else:
                    state[sym] = [direction, 0.0, 0.0, fill.timestamp, 0.0]

                # Excess → opens reverse position
                excess = fill.qty - close_qty
                if excess > 1e-12:
                    new_dir = "LONG" if fill.side == "buy" else "SHORT"
                    excess_fee = self._fee_in_quote(fill, excess)
                    state[sym] = [new_dir, fill.price, excess, fill.timestamp, excess_fee]

        # Build open_map from remaining state
        open_map: Dict[str, tuple] = {}
        for sym, st in state.items():
            direction, avg_price, total_qty, entry_time, total_entry_fee = st
            if total_qty > 1e-12:
                open_map[sym] = (direction, avg_price, total_qty, entry_time, total_entry_fee)

        return closed, open_map


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

async def _main() -> None:
    import argparse
    import time

    parser = argparse.ArgumentParser(
        description="Analyse Binance trade history from the command line.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Example:\n  python -m src.trading.binance_history_analyzer BTC/USDT --method fifo --days 30",
    )
    parser.add_argument(
        "symbols",
        nargs="+",
        help='Trading pair(s), e.g. "BTC/USDT" "ETH/USDT"',
    )
    parser.add_argument(
        "--method",
        choices=["fifo", "average"],
        default="fifo",
        help="Position reconstruction method (default: fifo)",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="Only look back N days (default: all available history)",
    )
    parser.add_argument(
        "--testnet",
        action="store_true",
        help="Use Binance testnet / demo API",
    )
    parser.add_argument(
        "--env",
        default="keys.env",
        help="Path to env file containing BINANCE_API_KEY/BINANCE_API_SECRET (default: keys.env)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print per-trade breakdown table",
    )
    args = parser.parse_args()

    since: Optional[int] = None
    if args.days:
        since = int((time.time() - args.days * 86400) * 1000)

    async with BinanceHistoryAnalyzer.from_env(args.env, testnet=args.testnet) as ana:
        report = await ana.run(args.symbols, method=args.method, since=since)

    print(report.summary())

    if args.verbose and report.closed:
        header = (
            f"\n  {'#':>4}  {'Symbol':<12} {'Dir':<6} "
            f"{'Entry':>11} {'Exit':>11} {'Qty':>11} "
            f"{'Net PNL':>11} {'PNL%':>8}  W/L"
        )
        sep = "  " + "─" * (len(header) - 2)
        print(header)
        print(sep)
        for i, leg in enumerate(report.closed, 1):
            wl = "WIN " if leg.is_win else "LOSS"
            print(
                f"  {i:>4}  {leg.symbol:<12} {leg.direction:<6}"
                f" {leg.entry_price:>11.4f} {leg.exit_price:>11.4f}"
                f" {leg.qty:>11.6f} {leg.pnl_net_quote:>+11.4f}"
                f" {leg.pnl_pct:>+8.2f}%  {wl}"
            )


if __name__ == "__main__":
    asyncio.run(_main())
