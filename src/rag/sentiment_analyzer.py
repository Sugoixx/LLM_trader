"""
Sentiment analysis module using HuggingFace transformers.
Provides financial sentiment analysis for news articles.
"""

from typing import Dict, Any, Optional
import logging
from transformers import pipeline


class SentimentAnalyzer:
    """Analyzes sentiment of financial text using pre-trained models."""

    def __init__(self, logger: logging.Logger, model_name: str = "ProsusAI/finbert"):
        """Initialize sentiment analyzer.

        Args:
            logger: Logger instance
            model_name: HuggingFace model name for sentiment analysis
        """
        self.logger = logger
        self.model_name = model_name
        self._pipeline: Optional[Any] = None

    def _ensure_pipeline(self) -> bool:
        """Lazy load the sentiment analysis pipeline."""
        if self._pipeline is not None:
            return True

        try:
            self.logger.info("Loading sentiment analysis model: %s", self.model_name)
            self._pipeline = pipeline(
                "sentiment-analysis",
                model=self.model_name,
                tokenizer=self.model_name,
                top_k=None,  # replaces deprecated return_all_scores=True
            )
            self.logger.info("Sentiment analysis model loaded successfully")
            return True
        except Exception as e:
            self.logger.error(
                "Failed to load sentiment model %s: %s", self.model_name, e
            )
            return False

    def analyze_text(self, text: str) -> Dict[str, Any]:
        """Analyze sentiment of the given text.

        Args:
            text: Text to analyze

        Returns:
            Dict with sentiment scores: {'positive': float, 'negative': float, 'neutral': float}
        """
        if not self._ensure_pipeline():
            return {"positive": 0.0, "negative": 0.0, "neutral": 0.0}

        if not text or not text.strip():
            return {"positive": 0.0, "negative": 0.0, "neutral": 0.0}

        try:
            # Limit text length to avoid model limits
            truncated_text = text[:512] if len(text) > 512 else text

            results = self._pipeline(truncated_text)

            # Convert results to our format
            # Handle both [[{…}]] (top_k=None, batch) and [{…}] (single result)
            scores = {"positive": 0.0, "negative": 0.0, "neutral": 0.0}
            result_list = results[0] if results else []
            if isinstance(result_list, dict):
                result_list = [result_list]
            for result in result_list:
                label = result["label"].lower()
                score = result["score"]
                if "positive" in label:
                    scores["positive"] = score
                elif "negative" in label:
                    scores["negative"] = score
                elif "neutral" in label:
                    scores["neutral"] = score

            return scores

        except Exception as e:
            self.logger.error("Error analyzing sentiment for text: %s", e)
            return {"positive": 0.0, "negative": 0.0, "neutral": 0.0}

    def get_overall_sentiment(self, scores: Dict[str, float]) -> str:
        """Get overall sentiment label from scores.

        Args:
            scores: Sentiment scores dict

        Returns:
            'positive', 'negative', or 'neutral'
        """
        pos = scores.get("positive", 0.0)
        neg = scores.get("negative", 0.0)
        neu = scores.get("neutral", 0.0)

        if pos > neg and pos > neu:
            return "positive"
        elif neg > pos and neg > neu:
            return "negative"
        else:
            return "neutral"
