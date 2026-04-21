"""Static catalog of tradable symbols, organised by category with metadata.

Used by the dashboard to show a nice categorised picker (Forex / Metals /
Energy / Indices / Crypto) with volatility, liquidity and market-hours hints.

Metadata conventions
--------------------
volatility : "low" | "medium" | "high" | "very_high"
liquidity  : "low" | "medium" | "high" | "very_high"
hours      : human-readable string (e.g. "24/5", "24/7", "London+NY")
session    : "forex" | "ny" | "london" | "crypto" | "asia" | "all"
venue      : "mt5" | "ccxt" | "both"  — which backend can serve it
asset_class: "forex" | "metals" | "energy" | "indices" | "crypto"
"""

from __future__ import annotations

from typing import Dict, List, TypedDict


class SymbolEntry(TypedDict, total=False):
    symbol: str          # canonical symbol used by the bot/exchange
    label: str           # display label
    description: str
    volatility: str
    liquidity: str
    hours: str
    session: str
    venue: str
    asset_class: str
    icon: str            # emoji or unicode icon


class CategoryEntry(TypedDict):
    id: str
    label: str
    icon: str
    description: str
    symbols: List[SymbolEntry]


SYMBOL_CATALOG: List[CategoryEntry] = [
    {
        "id": "forex",
        "label": "Forex",
        "icon": "💱",
        "description": "Major currency pairs — deep liquidity, narrow spreads",
        "symbols": [
            {"symbol": "EURUSD", "label": "EUR/USD", "description": "Euro vs US Dollar — most traded pair",
             "volatility": "low", "liquidity": "very_high", "hours": "24/5",
             "session": "london+ny", "venue": "mt5", "asset_class": "forex", "icon": "🇪🇺"},
            {"symbol": "GBPUSD", "label": "GBP/USD", "description": "British Pound vs US Dollar — 'Cable'",
             "volatility": "medium", "liquidity": "very_high", "hours": "24/5",
             "session": "london+ny", "venue": "mt5", "asset_class": "forex", "icon": "🇬🇧"},
            {"symbol": "USDJPY", "label": "USD/JPY", "description": "US Dollar vs Japanese Yen",
             "volatility": "low", "liquidity": "very_high", "hours": "24/5",
             "session": "asia+ny", "venue": "mt5", "asset_class": "forex", "icon": "🇯🇵"},
            {"symbol": "AUDUSD", "label": "AUD/USD", "description": "Australian Dollar vs US Dollar — commodity FX",
             "volatility": "medium", "liquidity": "high", "hours": "24/5",
             "session": "asia", "venue": "mt5", "asset_class": "forex", "icon": "🇦🇺"},
            {"symbol": "USDCAD", "label": "USD/CAD", "description": "US Dollar vs Canadian Dollar — correlated to oil",
             "volatility": "medium", "liquidity": "high", "hours": "24/5",
             "session": "ny", "venue": "mt5", "asset_class": "forex", "icon": "🇨🇦"},
            {"symbol": "USDCHF", "label": "USD/CHF", "description": "US Dollar vs Swiss Franc — safe-haven",
             "volatility": "low", "liquidity": "high", "hours": "24/5",
             "session": "london", "venue": "mt5", "asset_class": "forex", "icon": "🇨🇭"},
            {"symbol": "NZDUSD", "label": "NZD/USD", "description": "NZ Dollar vs US Dollar",
             "volatility": "medium", "liquidity": "medium", "hours": "24/5",
             "session": "asia", "venue": "mt5", "asset_class": "forex", "icon": "🇳🇿"},
        ],
    },
    {
        "id": "metals",
        "label": "Métaux",
        "icon": "🥇",
        "description": "Métaux précieux — haute volatilité, sensibles aux crises",
        "symbols": [
            {"symbol": "XAUUSD", "label": "Gold / USD", "description": "Or vs USD — valeur refuge",
             "volatility": "high", "liquidity": "very_high", "hours": "24/5",
             "session": "all", "venue": "mt5", "asset_class": "metals", "icon": "🥇"},
            {"symbol": "XAGUSD", "label": "Silver / USD", "description": "Argent vs USD — plus volatil que l'or",
             "volatility": "very_high", "liquidity": "high", "hours": "24/5",
             "session": "all", "venue": "mt5", "asset_class": "metals", "icon": "🥈"},
        ],
    },
    {
        "id": "energy",
        "label": "Énergie",
        "icon": "🛢️",
        "description": "Pétrole et gaz — très volatils, gap de week-end",
        "symbols": [
            {"symbol": "CRUDOIL", "label": "WTI Oil", "description": "Pétrole brut West Texas Intermediate",
             "volatility": "very_high", "liquidity": "high", "hours": "23/5",
             "session": "ny", "venue": "mt5", "asset_class": "energy", "icon": "🛢️"},
            {"symbol": "XTIUSD", "label": "WTI (XTIUSD)", "description": "WTI — variante symbole broker",
             "volatility": "very_high", "liquidity": "high", "hours": "23/5",
             "session": "ny", "venue": "mt5", "asset_class": "energy", "icon": "🛢️"},
            {"symbol": "XBRUSD", "label": "Brent Oil", "description": "Brent Crude Oil — référence mondiale",
             "volatility": "very_high", "liquidity": "high", "hours": "23/5",
             "session": "london", "venue": "mt5", "asset_class": "energy", "icon": "🛢️"},
            {"symbol": "XNGUSD", "label": "Natural Gas", "description": "Gaz naturel — extrêmement volatil",
             "volatility": "very_high", "liquidity": "medium", "hours": "23/5",
             "session": "ny", "venue": "mt5", "asset_class": "energy", "icon": "🔥"},
        ],
    },
    {
        "id": "indices",
        "label": "Indices",
        "icon": "📈",
        "description": "Indices boursiers — horaires de marché stricts",
        "symbols": [
            {"symbol": "US30", "label": "Dow Jones 30", "description": "Dow Jones Industrial Average",
             "volatility": "high", "liquidity": "very_high", "hours": "NY 9:30-16:00",
             "session": "ny", "venue": "mt5", "asset_class": "indices", "icon": "🇺🇸"},
            {"symbol": "US500", "label": "S&P 500", "description": "Standard & Poor's 500 — benchmark US",
             "volatility": "medium", "liquidity": "very_high", "hours": "NY 9:30-16:00",
             "session": "ny", "venue": "mt5", "asset_class": "indices", "icon": "🇺🇸"},
            {"symbol": "US100", "label": "Nasdaq 100", "description": "Tech-heavy Nasdaq 100",
             "volatility": "high", "liquidity": "very_high", "hours": "NY 9:30-16:00",
             "session": "ny", "venue": "mt5", "asset_class": "indices", "icon": "💻"},
            {"symbol": "DE30", "label": "DAX 30", "description": "Deutscher Aktien Index — Allemagne",
             "volatility": "medium", "liquidity": "high", "hours": "Frankfurt 9:00-17:30",
             "session": "london", "venue": "mt5", "asset_class": "indices", "icon": "🇩🇪"},
            {"symbol": "UK100", "label": "FTSE 100", "description": "FTSE 100 — Royaume-Uni",
             "volatility": "medium", "liquidity": "high", "hours": "London 8:00-16:30",
             "session": "london", "venue": "mt5", "asset_class": "indices", "icon": "🇬🇧"},
            {"symbol": "JP225", "label": "Nikkei 225", "description": "Nikkei 225 — Japon",
             "volatility": "medium", "liquidity": "high", "hours": "Tokyo 9:00-15:00",
             "session": "asia", "venue": "mt5", "asset_class": "indices", "icon": "🇯🇵"},
        ],
    },
    {
        "id": "crypto",
        "label": "Crypto",
        "icon": "₿",
        "description": "Cryptomonnaies — marché 24/7, très volatiles",
        "symbols": [
            {"symbol": "BTC/USDT", "label": "Bitcoin", "description": "Bitcoin vs USDT — la plus liquide",
             "volatility": "high", "liquidity": "very_high", "hours": "24/7",
             "session": "crypto", "venue": "ccxt", "asset_class": "crypto", "icon": "₿"},
            {"symbol": "ETH/USDT", "label": "Ethereum", "description": "Ethereum vs USDT",
             "volatility": "high", "liquidity": "very_high", "hours": "24/7",
             "session": "crypto", "venue": "ccxt", "asset_class": "crypto", "icon": "Ξ"},
            {"symbol": "SOL/USDT", "label": "Solana", "description": "Solana — L1 haute perf",
             "volatility": "very_high", "liquidity": "high", "hours": "24/7",
             "session": "crypto", "venue": "ccxt", "asset_class": "crypto", "icon": "◎"},
            {"symbol": "BNB/USDT", "label": "BNB", "description": "Binance Coin",
             "volatility": "high", "liquidity": "high", "hours": "24/7",
             "session": "crypto", "venue": "ccxt", "asset_class": "crypto", "icon": "🔶"},
            {"symbol": "XRP/USDT", "label": "XRP", "description": "Ripple — paiements cross-border",
             "volatility": "high", "liquidity": "high", "hours": "24/7",
             "session": "crypto", "venue": "ccxt", "asset_class": "crypto", "icon": "🌊"},
            {"symbol": "ADA/USDT", "label": "Cardano", "description": "Cardano",
             "volatility": "high", "liquidity": "medium", "hours": "24/7",
             "session": "crypto", "venue": "ccxt", "asset_class": "crypto", "icon": "💧"},
            {"symbol": "DOGE/USDT", "label": "Dogecoin", "description": "Dogecoin — meme coin majeur",
             "volatility": "very_high", "liquidity": "high", "hours": "24/7",
             "session": "crypto", "venue": "ccxt", "asset_class": "crypto", "icon": "🐕"},
            {"symbol": "AVAX/USDT", "label": "Avalanche", "description": "Avalanche L1",
             "volatility": "very_high", "liquidity": "medium", "hours": "24/7",
             "session": "crypto", "venue": "ccxt", "asset_class": "crypto", "icon": "🔺"},
        ],
    },
]


def get_all_symbols() -> List[str]:
    """Return a flat list of all canonical symbols in the catalog."""
    return [s["symbol"] for cat in SYMBOL_CATALOG for s in cat["symbols"]]


def find_symbol(symbol: str) -> Dict:
    """Look up a symbol entry. Returns empty dict if not found."""
    for cat in SYMBOL_CATALOG:
        for entry in cat["symbols"]:
            if entry["symbol"].upper() == symbol.upper():
                return {**entry, "category": cat["id"], "category_label": cat["label"]}
    return {}


def is_known_symbol(symbol: str) -> bool:
    """Check whether a symbol is present in the catalog (case-insensitive)."""
    target = symbol.upper()
    return any(entry["symbol"].upper() == target
               for cat in SYMBOL_CATALOG for entry in cat["symbols"])
