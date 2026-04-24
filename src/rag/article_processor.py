"""
Shared article processing utilities for RAG components.
Eliminates code duplication between news_manager and context_builder.
"""

from typing import Dict, Any, Set
import logging


class ArticleProcessor:
    """Utility class for common article processing operations."""

    def __init__(
        self,
        logger: logging.Logger,
        format_utils=None,
        unified_parser=None,
        sentiment_analyzer=None,
    ):

        self.logger = logger
        self.parser = unified_parser
        self.format_utils = format_utils
        self.sentiment_analyzer = sentiment_analyzer

    def detect_coins_in_article(
        self, article: Dict[str, Any], known_crypto_tickers: Set[str]
    ) -> Set[str]:
        """Detect cryptocurrency mentions in article content."""
        # Check categories first
        coins_mentioned = set()
        categories = article.get("categories", "").split("|")
        for category in categories:
            cat_upper = category.upper()
            if cat_upper in known_crypto_tickers:
                coins_mentioned.add(cat_upper)

        # Check title and body for coin mentions
        title = article.get("title", "")
        body = (
            article.get("body", "")[:10000]
            if len(article.get("body", "")) >= 10000
            else article.get("body", "")
        )

        title_coins = self.parser.detect_coins_in_text(title, known_crypto_tickers)
        body_coins = self.parser.detect_coins_in_text(body, known_crypto_tickers)

        coins_mentioned.update(title_coins)
        coins_mentioned.update(body_coins)

        return coins_mentioned

    def get_article_timestamp(self, article: Dict[str, Any]) -> float:
        """Extract timestamp from article in a consistent format."""
        published_on = article.get("published_on", 0)
        return self.format_utils.parse_timestamp(published_on)

    def extract_base_coin(self, symbol: str) -> str:
        """Extract base coin from trading pair symbol."""

        return self.parser.extract_base_coin(symbol)

    def analyze_article_sentiment(self, article: Dict[str, Any]) -> Dict[str, Any]:
        """Analyze sentiment of an article and add results to it.

        Args:
            article: Article dict with title and body

        Returns:
            Updated article dict with sentiment scores
        """
        if not self.sentiment_analyzer:
            return article

        try:
            # Combine title and body for analysis
            text_to_analyze = (
                f"{article.get('title', '')}. {article.get('body', '')[:1000]}"
            )

            sentiment_scores = self.sentiment_analyzer.analyze_text(text_to_analyze)
            overall_sentiment = self.sentiment_analyzer.get_overall_sentiment(
                sentiment_scores
            )

            article["sentiment_scores"] = sentiment_scores
            article["overall_sentiment"] = overall_sentiment

            self.logger.debug(
                "Analyzed sentiment for article: %s -> %s",
                article.get("title", "")[:50],
                overall_sentiment,
            )

        except Exception as e:
            self.logger.error("Error analyzing sentiment for article: %s", e)
            article["sentiment_scores"] = {
                "positive": 0.0,
                "negative": 0.0,
                "neutral": 0.0,
            }
            article["overall_sentiment"] = "neutral"

        return article
