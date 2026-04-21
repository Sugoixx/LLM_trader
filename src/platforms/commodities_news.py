"""
Multi-source commodities news client for RAG Engine.

Fetches real-time news from multiple free sources optimised for speed:
  ── No API key (RSS feeds — always active) ────────────────────────────────
  1. Google News RSS      — keyword-filtered, near real-time (~5 min delay)
  2. Yahoo Finance RSS    — commodities section, market moves
  3. CNBC RSS             — energy, economy, markets
  4. Investing.com RSS    — commodities, economic calendar
  5. Reuters RSS          — commodities via Google News site:reuters.com
  6. MarketWatch RSS      — energy, markets, economy

  ── Optional API keys (higher quality, structured data) ───────────────────
  7. NewsAPI.org           — 100 req/day free, keyword search, 80k+ sources
  8. GNews.io              — 100 req/day free, fast, multi-language
  9. Finnhub               — 60 calls/min free, market news + sentiment
 10. MarketAux             — 100 req/day free, entity recognition
 11. TheNewsAPI            — 150 req/day free, multi-category
 12. Alpha Vantage News    — 25 req/day free, sentiment scored

All sources are queried in parallel; results are deduplicated and merged.
"""

import asyncio
import hashlib
import html
import re
import time
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional, Set, TYPE_CHECKING
from xml.etree import ElementTree

import aiohttp

from src.logger.logger import Logger

if TYPE_CHECKING:
    from src.config.protocol import ConfigProtocol

# ── Asset-class keyword maps ──────────────────────────────────────────────
# Search queries used to find relevant articles across all sources.
# Focused on oil/energy and geopolitical factors that move crude prices.
ASSET_KEYWORDS: Dict[str, List[str]] = {
    "oil": [
        "crude oil price",
        "WTI crude",
        "Brent crude",
        "OPEC production cut",
        "oil supply demand",
        "petroleum inventory",
        "EIA crude oil",
        "oil embargo sanctions",
    ],
    "geopolitics": [
        "Strait of Hormuz",
        "Iran oil sanctions",
        "Iran USA tensions",
        "Middle East oil",
        "Russia oil embargo",
        "OPEC+ meeting",
        "Saudi Arabia oil production",
        "oil war conflict",
    ],
    "energy": [
        "energy crisis",
        "oil refinery",
        "natural gas oil",
        "oil pipeline",
        "oil tanker shipping",
        "barrel oil price",
    ],
    "macro": [
        "Federal Reserve interest rate",
        "US dollar oil",
        "inflation energy prices",
        "recession oil demand",
    ],
    # Forex / central-bank / currency-specific coverage
    "forex": [
        "EUR USD forecast",
        "euro dollar exchange rate",
        "ECB rate decision",
        "Federal Reserve rate decision",
        "US dollar index DXY",
        "non farm payrolls",
        "CPI inflation report",
        "GBP USD pound sterling",
        "USD JPY yen intervention",
        "Bank of England rate",
        "Bank of Japan BOJ",
    ],
    # Crypto-only keywords (used when Commodities client backs crypto profile)
    "crypto": [
        "bitcoin price",
        "ethereum price",
        "BTC ETF flows",
        "stablecoin USDC USDT",
        "crypto regulation SEC",
        "altcoin rally",
    ],
    # Precious metals
    "metals": [
        "gold price forecast",
        "XAUUSD gold",
        "silver price XAGUSD",
        "platinum palladium",
        "safe haven gold",
        "Fed rate gold",
    ],
}

# ── Per-asset relevance term lists ────────────────────────────────────────
# Used to filter out irrelevant RSS noise after aggregation. Articles must
# contain at least one term (case-insensitive) from the matching asset list.
RELEVANCE_TERMS_BY_CLASS: Dict[str, Set[str]] = {
    "oil": {
        "oil", "crude", "wti", "brent", "opec", "petroleum", "barrel",
        "energy", "refinery", "pipeline", "tanker", "lng", "natural gas",
        "hormuz", "iran", "iraq", "saudi", "russia", "yemen", "houthi",
        "libya", "venezuela", "nigeria", "embargo", "sanction",
        "war", "conflict", "tension", "military", "missile", "drone",
        "middle east", "gulf", "strait",
        "opec+", "opec plus", "production cut", "output",
        "eia", "inventory", "stockpile", "reserves", "supply",
        "demand", "surplus", "shortage", "disruption",
        "inflation", "recession", "fed", "interest rate", "dollar",
    },
    "forex": {
        "eur", "euro", "usd", "dollar", "gbp", "pound", "sterling",
        "jpy", "yen", "chf", "franc", "aud", "cad", "nzd",
        "ecb", "fed", "fomc", "boe", "boj", "snb", "rba", "boc",
        "interest rate", "rate hike", "rate cut", "central bank",
        "inflation", "cpi", "ppi", "pce", "nonfarm", "nfp",
        "unemployment", "gdp", "recession", "tariff", "trade war",
        "currency", "exchange rate", "dxy", "forex", "fx",
    },
    "metals": {
        "gold", "xau", "silver", "xag", "platinum", "palladium",
        "precious metal", "bullion", "safe haven",
        "fed", "interest rate", "inflation", "dollar", "dxy",
        "geopolitic", "war", "tension", "recession",
    },
    "macro": {
        "fed", "fomc", "ecb", "boj", "boe", "interest rate",
        "inflation", "cpi", "recession", "gdp", "unemployment",
        "nonfarm", "dollar", "dxy", "bond yield", "treasury",
        "tariff", "trade war", "geopolitic",
    },
    # Crypto profile: the CommoditiesNewsClient is skipped for crypto
    # (CryptoCompareNewsClient handles it) but we keep a term set for safety.
    "crypto": {
        "bitcoin", "btc", "ethereum", "eth", "crypto", "stablecoin",
        "usdc", "usdt", "sec", "etf", "altcoin", "defi", "nft",
        "blockchain", "binance", "coinbase",
    },
}

# Keyword-group selection per asset class — defines which groups from
# ASSET_KEYWORDS are used when that class is active.
KEYWORD_GROUPS_BY_CLASS: Dict[str, List[str]] = {
    "oil":    ["oil", "geopolitics", "energy", "macro"],
    "forex":  ["forex", "macro"],
    "metals": ["metals", "macro"],
    "macro":  ["macro"],
    "crypto": ["crypto", "macro"],
}

# Map trading symbols → asset classes for automatic keyword selection
SYMBOL_ASSET_MAP: Dict[str, str] = {
    "CRUDOIL": "oil", "XTIUSD": "oil", "WTI": "oil", "USOIL": "oil",
    "BRENT": "oil", "XBRUSD": "oil", "UKOIL": "oil",
    "NATGAS": "oil", "XNGUSD": "oil",
    "GOLD": "metals", "XAUUSD": "metals",
    "SILVER": "metals", "XAGUSD": "metals",
    "PLATINUM": "metals", "XPTUSD": "metals",
    "PALLADIUM": "metals", "XPDUSD": "metals",
}

# ── Direct RSS feeds (no API key, always active) ──────────────────────────
# Each tuple: (url, source_label)
FINANCIAL_RSS_FEEDS: List[tuple] = [
    # Yahoo Finance — commodities + market moves
    ("https://finance.yahoo.com/news/rssindex", "Yahoo Finance"),
    # CNBC — energy, economy, markets
    ("https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=19836768", "CNBC Energy"),
    # Investing.com — commodities
    ("https://www.investing.com/rss/news_301.rss", "Investing.com Commodities"),
    # MarketWatch — energy headlines
    ("https://feeds.content.dowjones.io/public/rss/mw_realtimeheadlines", "MarketWatch"),
    # OilPrice.com — dedicated oil/energy news
    ("https://oilprice.com/rss/main", "OilPrice.com"),
]


class CommoditiesNewsClient:
    """Fetches commodities/macro news from multiple free sources in parallel."""

    def __init__(self, logger: Logger, config: "ConfigProtocol"):
        self.logger = logger
        self.config = config
        self._seen_hashes: Set[str] = set()

    # ── Public API ────────────────────────────────────────────────────────

    async def fetch_news(
        self,
        session: Optional[aiohttp.ClientSession] = None,
        symbol: Optional[str] = None,
        asset_class: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Fetch news from all configured sources in parallel.

        Args:
            session: Shared aiohttp session (created internally if None).
            symbol: Trading symbol (e.g. "CRUDOIL", "EURUSD", "XAUUSD").
            asset_class: Override asset class (crypto/forex/oil/metals/macro).
                         Falls back to SYMBOL_ASSET_MAP or "macro".

        Returns:
            List of normalised article dicts, newest first.
        """
        resolved_class = self._resolve_asset_class(symbol, asset_class)
        keywords = self._keywords_for_class(resolved_class)
        own_session = session is None
        if own_session:
            session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15),
                headers={"User-Agent": "LLM_Trader_v2/1.0"},
            )

        try:
            # ── RSS sources (no API key required) ─────────────────────────
            tasks = [
                self._fetch_google_rss(session, keywords),
                self._fetch_direct_rss_feeds(session),
                self._fetch_reuters_rss(session, keywords),
            ]

            # ── API sources (optional keys) ───────────────────────────────
            newsapi_key = self.config.get_env("NEWSAPI_KEY")
            if newsapi_key:
                tasks.append(self._fetch_newsapi(session, keywords, newsapi_key))

            gnews_key = self.config.get_env("GNEWS_API_KEY")
            if gnews_key:
                tasks.append(self._fetch_gnews(session, keywords, gnews_key))

            finnhub_key = self.config.get_env("FINNHUB_API_KEY")
            if finnhub_key:
                tasks.append(self._fetch_finnhub(session, keywords, finnhub_key))

            marketaux_key = self.config.get_env("MARKETAUX_API_KEY")
            if marketaux_key:
                tasks.append(self._fetch_marketaux(session, keywords, marketaux_key))

            thenewsapi_key = self.config.get_env("THENEWSAPI_KEY")
            if thenewsapi_key:
                tasks.append(self._fetch_thenewsapi(session, keywords, thenewsapi_key))

            alphavantage_key = self.config.get_env("ALPHAVANTAGE_API_KEY")
            if alphavantage_key:
                tasks.append(self._fetch_alphavantage(session, keywords, alphavantage_key))

            results = await asyncio.gather(*tasks, return_exceptions=True)

            articles: List[Dict[str, Any]] = []
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    self.logger.warning("Commodities news source %d failed: %s", i, result)
                    continue
                articles.extend(result)

            # Deduplicate, filter relevance, sort newest first
            unique = self._deduplicate(articles)
            relevant = self._filter_relevant(unique, resolved_class)
            relevant.sort(key=lambda a: a.get("published_on", 0), reverse=True)
            self.logger.info(
                "Commodities news: %d relevant / %d total from %d sources (symbol=%s, class=%s)",
                len(relevant), len(unique), len(tasks), symbol or "general", resolved_class,
            )
            return relevant
        finally:
            if own_session:
                await session.close()

    def filter_by_age(
        self,
        articles: List[Dict[str, Any]],
        max_age_hours: int = 48,
    ) -> List[Dict[str, Any]]:
        """Keep only articles younger than max_age_hours."""
        cutoff = time.time() - (max_age_hours * 3600)
        return [a for a in articles if a.get("published_on", 0) > cutoff]

    # ── Source: Google News RSS ────────────────────────────────────────────

    async def _fetch_google_rss(
        self,
        session: aiohttp.ClientSession,
        keywords: List[str],
    ) -> List[Dict[str, Any]]:
        """Fetch from Google News RSS — free, no API key, fast."""
        articles: List[Dict[str, Any]] = []

        # Query top 3 keyword groups to stay fast
        for query in keywords[:3]:
            url = (
                "https://news.google.com/rss/search?"
                f"q={aiohttp.helpers.quote(query, safe='')}"
                "&hl=en&gl=US&ceid=US:en"
            )
            try:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        self.logger.debug("Google RSS %d for query '%s'", resp.status, query)
                        continue
                    text = await resp.text()
                articles.extend(self._parse_rss(text, source="Google News"))
            except Exception as e:
                self.logger.debug("Google RSS error for '%s': %s", query, e)
        return articles

    # ── Source: NewsAPI.org ────────────────────────────────────────────────

    async def _fetch_newsapi(
        self,
        session: aiohttp.ClientSession,
        keywords: List[str],
        api_key: str,
    ) -> List[Dict[str, Any]]:
        """Fetch from NewsAPI.org — fast, 100 req/day free tier."""
        articles: List[Dict[str, Any]] = []
        # Combine top keywords into one OR query (fewer API calls)
        combined_q = " OR ".join(f'"{kw}"' for kw in keywords[:4])
        from_date = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%d")
        url = (
            "https://newsapi.org/v2/everything?"
            f"q={aiohttp.helpers.quote(combined_q, safe='')}"
            f"&from={from_date}"
            "&sortBy=publishedAt"
            "&pageSize=30"
            "&language=en"
        )
        headers = {"X-Api-Key": api_key}
        try:
            async with session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    self.logger.warning("NewsAPI returned %d", resp.status)
                    return []
                data = await resp.json()
            for item in data.get("articles", []):
                ts = self._parse_iso_timestamp(item.get("publishedAt", ""))
                articles.append({
                    "id": hashlib.md5(
                        (item.get("url", "") or item.get("title", "")).encode()
                    ).hexdigest(),
                    "title": item.get("title", ""),
                    "body": item.get("description", "") or item.get("content", "") or "",
                    "url": item.get("url", ""),
                    "source": item.get("source", {}).get("name", "NewsAPI"),
                    "published_on": ts,
                    "categories": "commodities|macro",
                    "tags": "newsapi",
                })
        except Exception as e:
            self.logger.warning("NewsAPI error: %s", e)
        return articles

    # ── Source: GNews.io ──────────────────────────────────────────────────

    async def _fetch_gnews(
        self,
        session: aiohttp.ClientSession,
        keywords: List[str],
        api_key: str,
    ) -> List[Dict[str, Any]]:
        """Fetch from GNews.io — fast, 100 req/day free tier."""
        articles: List[Dict[str, Any]] = []
        query = " OR ".join(keywords[:3])
        url = (
            "https://gnews.io/api/v4/search?"
            f"q={aiohttp.helpers.quote(query, safe='')}"
            f"&token={api_key}"
            "&lang=en&max=20&sortby=publishedAt"
        )
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    self.logger.warning("GNews returned %d", resp.status)
                    return []
                data = await resp.json()
            for item in data.get("articles", []):
                ts = self._parse_iso_timestamp(item.get("publishedAt", ""))
                articles.append({
                    "id": hashlib.md5(
                        (item.get("url", "") or item.get("title", "")).encode()
                    ).hexdigest(),
                    "title": item.get("title", ""),
                    "body": item.get("description", "") or item.get("content", "") or "",
                    "url": item.get("url", ""),
                    "source": item.get("source", {}).get("name", "GNews"),
                    "published_on": ts,
                    "categories": "commodities|macro",
                    "tags": "gnews",
                })
        except Exception as e:
            self.logger.warning("GNews error: %s", e)
        return articles

    # ── Source: Direct RSS Feeds (Yahoo, CNBC, Investing.com, MarketWatch) ─

    async def _fetch_direct_rss_feeds(
        self,
        session: aiohttp.ClientSession,
    ) -> List[Dict[str, Any]]:
        """Fetch from multiple financial RSS feeds in parallel — free, no API key."""
        async def _fetch_one(url: str, source: str) -> List[Dict[str, Any]]:
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        return []
                    text = await resp.text()
                return self._parse_rss(text, source=source)
            except Exception:
                return []

        feed_tasks = [_fetch_one(url, src) for url, src in FINANCIAL_RSS_FEEDS]
        results = await asyncio.gather(*feed_tasks, return_exceptions=True)

        articles: List[Dict[str, Any]] = []
        for result in results:
            if isinstance(result, list):
                articles.extend(result)
        return articles

    # ── Source: Reuters via Google News RSS (site:reuters.com) ─────────────

    async def _fetch_reuters_rss(
        self,
        session: aiohttp.ClientSession,
        keywords: List[str],
    ) -> List[Dict[str, Any]]:
        """Fetch Reuters articles via Google News RSS with site: filter."""
        articles: List[Dict[str, Any]] = []
        # Use top 2 keywords with site:reuters.com
        for query in keywords[:2]:
            url = (
                "https://news.google.com/rss/search?"
                f"q={aiohttp.helpers.quote(query + ' site:reuters.com', safe='')}"
                "&hl=en&gl=US&ceid=US:en"
            )
            try:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        continue
                    text = await resp.text()
                articles.extend(self._parse_rss(text, source="Reuters"))
            except Exception:
                pass
        return articles

    # ── Source: Finnhub ────────────────────────────────────────────────────

    async def _fetch_finnhub(
        self,
        session: aiohttp.ClientSession,
        keywords: List[str],
        api_key: str,
    ) -> List[Dict[str, Any]]:
        """Fetch from Finnhub — free 60 calls/min, market news with sentiment."""
        articles: List[Dict[str, Any]] = []
        # General news (category=general covers commodities/macro)
        url = f"https://finnhub.io/api/v1/news?category=general&token={api_key}"
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    self.logger.warning("Finnhub returned %d", resp.status)
                    return []
                data = await resp.json()
            for item in data if isinstance(data, list) else []:
                articles.append({
                    "id": hashlib.md5(
                        str(item.get("id", item.get("url", ""))).encode()
                    ).hexdigest(),
                    "title": item.get("headline", ""),
                    "body": item.get("summary", ""),
                    "url": item.get("url", ""),
                    "source": item.get("source", "Finnhub"),
                    "published_on": item.get("datetime", int(time.time())),
                    "categories": "commodities|macro",
                    "tags": "finnhub",
                })
        except Exception as e:
            self.logger.warning("Finnhub error: %s", e)
        return articles

    # ── Source: MarketAux ─────────────────────────────────────────────────

    async def _fetch_marketaux(
        self,
        session: aiohttp.ClientSession,
        keywords: List[str],
        api_key: str,
    ) -> List[Dict[str, Any]]:
        """Fetch from MarketAux — free 100 req/day, entity recognition."""
        articles: List[Dict[str, Any]] = []
        query = ",".join(keywords[:3])
        url = (
            "https://api.marketaux.com/v1/news/all?"
            f"api_token={api_key}"
            f"&search={aiohttp.helpers.quote(query, safe='')}"
            "&language=en&limit=20&sort=published_desc"
        )
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    self.logger.warning("MarketAux returned %d", resp.status)
                    return []
                data = await resp.json()
            for item in data.get("data", []):
                ts = self._parse_iso_timestamp(item.get("published_at", ""))
                entities = [e.get("name", "") for e in item.get("entities", [])[:5]]
                articles.append({
                    "id": hashlib.md5(
                        (item.get("url", "") or item.get("title", "")).encode()
                    ).hexdigest(),
                    "title": item.get("title", ""),
                    "body": item.get("description", "") or item.get("snippet", "") or "",
                    "url": item.get("url", ""),
                    "source": item.get("source", "MarketAux"),
                    "published_on": ts,
                    "categories": "commodities|macro",
                    "tags": "marketaux|" + "|".join(entities) if entities else "marketaux",
                })
        except Exception as e:
            self.logger.warning("MarketAux error: %s", e)
        return articles

    # ── Source: TheNewsAPI ─────────────────────────────────────────────────

    async def _fetch_thenewsapi(
        self,
        session: aiohttp.ClientSession,
        keywords: List[str],
        api_key: str,
    ) -> List[Dict[str, Any]]:
        """Fetch from TheNewsAPI — free 150 req/day, multi-category."""
        articles: List[Dict[str, Any]] = []
        query = "|".join(keywords[:3])
        url = (
            "https://api.thenewsapi.com/v1/news/all?"
            f"api_token={api_key}"
            f"&search={aiohttp.helpers.quote(query, safe='')}"
            "&language=en&limit=20&sort=published_at"
            "&categories=business,general"
        )
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    self.logger.warning("TheNewsAPI returned %d", resp.status)
                    return []
                data = await resp.json()
            for item in data.get("data", []):
                ts = self._parse_iso_timestamp(item.get("published_at", ""))
                articles.append({
                    "id": hashlib.md5(
                        (item.get("url", "") or item.get("title", "")).encode()
                    ).hexdigest(),
                    "title": item.get("title", ""),
                    "body": item.get("description", "") or item.get("snippet", "") or "",
                    "url": item.get("url", ""),
                    "source": item.get("source", "TheNewsAPI"),
                    "published_on": ts,
                    "categories": "commodities|macro",
                    "tags": "thenewsapi",
                })
        except Exception as e:
            self.logger.warning("TheNewsAPI error: %s", e)
        return articles

    # ── Source: Alpha Vantage News & Sentiments ───────────────────────────

    async def _fetch_alphavantage(
        self,
        session: aiohttp.ClientSession,
        keywords: List[str],
        api_key: str,
    ) -> List[Dict[str, Any]]:
        """Fetch from Alpha Vantage News — free 25 req/day, sentiment scored."""
        articles: List[Dict[str, Any]] = []
        # Alpha Vantage uses topics: economy_macro, energy_transportation, finance
        topics = "economy_macro,energy_transportation,finance"
        url = (
            "https://www.alphavantage.co/query?"
            "function=NEWS_SENTIMENT"
            f"&topics={topics}"
            "&sort=LATEST&limit=30"
            f"&apikey={api_key}"
        )
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    self.logger.warning("Alpha Vantage returned %d", resp.status)
                    return []
                data = await resp.json()
            for item in data.get("feed", []):
                # Parse "20260417T143000" format
                ts = self._parse_av_timestamp(item.get("time_published", ""))
                sentiment = item.get("overall_sentiment_label", "")
                score = item.get("overall_sentiment_score", 0)
                articles.append({
                    "id": hashlib.md5(
                        (item.get("url", "") or item.get("title", "")).encode()
                    ).hexdigest(),
                    "title": item.get("title", ""),
                    "body": item.get("summary", ""),
                    "url": item.get("url", ""),
                    "source": item.get("source", "Alpha Vantage"),
                    "published_on": ts,
                    "categories": "commodities|macro",
                    "tags": f"alphavantage|sentiment:{sentiment}|score:{score}",
                })
        except Exception as e:
            self.logger.warning("Alpha Vantage error: %s", e)
        return articles

    # ── RSS Parsing ───────────────────────────────────────────────────────

    def _parse_rss(self, xml_text: str, source: str = "RSS") -> List[Dict[str, Any]]:
        """Parse RSS/Atom XML into normalised article dicts."""
        articles: List[Dict[str, Any]] = []
        try:
            root = ElementTree.fromstring(xml_text)
        except ElementTree.ParseError:
            return articles

        for item in root.iter("item"):
            title = self._xml_text(item, "title")
            link = self._xml_text(item, "link")
            description = self._xml_text(item, "description")
            pub_date = self._xml_text(item, "pubDate")
            source_el = item.find("source")
            src_name = source_el.text if source_el is not None and source_el.text else source

            ts = self._parse_rfc2822_timestamp(pub_date) if pub_date else int(time.time())
            # Strip HTML from description
            clean_body = re.sub(r"<[^>]+>", "", html.unescape(description)) if description else ""

            articles.append({
                "id": hashlib.md5((link or title or "").encode()).hexdigest(),
                "title": html.unescape(title) if title else "",
                "body": clean_body[:3000],
                "url": link or "",
                "source": src_name,
                "published_on": ts,
                "categories": "commodities|macro",
                "tags": "rss",
            })
        return articles

    # ── Helpers ────────────────────────────────────────────────────────────

    @staticmethod
    def _resolve_asset_class(symbol: Optional[str], asset_class: Optional[str]) -> str:
        """Pick the effective asset class from explicit override or symbol map."""
        valid = {"crypto", "forex", "oil", "metals", "macro"}
        if asset_class and asset_class in valid:
            return asset_class
        if symbol:
            key = symbol.upper().replace("/", "").replace(" ", "")
            mapped = SYMBOL_ASSET_MAP.get(key)
            if mapped:
                return mapped
            # Heuristic: 6-letter alpha = forex pair (EURUSD, GBPJPY…)
            if len(key) == 6 and key.isalpha():
                return "forex"
        return "macro"

    def _keywords_for_class(self, asset_class: str) -> List[str]:
        """Build deduplicated keyword list for the given asset class."""
        groups = KEYWORD_GROUPS_BY_CLASS.get(asset_class, ["macro"])
        keywords: List[str] = []
        for group in groups:
            keywords.extend(ASSET_KEYWORDS.get(group, []))
        seen: Set[str] = set()
        unique: List[str] = []
        for kw in keywords:
            if kw not in seen:
                seen.add(kw)
                unique.append(kw)
        return unique

    @staticmethod
    def _filter_relevant(
        articles: List[Dict[str, Any]],
        asset_class: str = "oil",
    ) -> List[Dict[str, Any]]:
        """Keep only articles whose title/body contain terms relevant to the class."""
        terms = RELEVANCE_TERMS_BY_CLASS.get(
            asset_class, RELEVANCE_TERMS_BY_CLASS["oil"]
        )
        relevant: List[Dict[str, Any]] = []
        for article in articles:
            text = (
                article.get("title", "") + " " + article.get("body", "")
            ).lower()
            if any(term in text for term in terms):
                relevant.append(article)
        return relevant

    def _deduplicate(self, articles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Remove duplicates based on title hash."""
        unique: List[Dict[str, Any]] = []
        for article in articles:
            title = article.get("title", "").strip().lower()
            h = hashlib.md5(title.encode()).hexdigest()
            if h not in self._seen_hashes:
                self._seen_hashes.add(h)
                unique.append(article)
        return unique

    @staticmethod
    def _xml_text(element: ElementTree.Element, tag: str) -> str:
        """Get text content of a child element."""
        child = element.find(tag)
        return child.text.strip() if child is not None and child.text else ""

    @staticmethod
    def _parse_rfc2822_timestamp(date_str: str) -> int:
        """Parse RFC 2822 date (RSS pubDate) to Unix timestamp."""
        try:
            return int(parsedate_to_datetime(date_str).timestamp())
        except Exception:
            return int(time.time())

    @staticmethod
    def _parse_iso_timestamp(date_str: str) -> int:
        """Parse ISO 8601 date to Unix timestamp."""
        if not date_str:
            return int(time.time())
        try:
            # Handle 'Z' suffix
            clean = date_str.replace("Z", "+00:00")
            dt = datetime.fromisoformat(clean)
            return int(dt.timestamp())
        except Exception:
            return int(time.time())

    @staticmethod
    def _parse_av_timestamp(date_str: str) -> int:
        """Parse Alpha Vantage timestamp format '20260417T143000' to Unix."""
        if not date_str:
            return int(time.time())
        try:
            dt = datetime.strptime(date_str[:15], "%Y%m%dT%H%M%S")
            dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except Exception:
            return int(time.time())
