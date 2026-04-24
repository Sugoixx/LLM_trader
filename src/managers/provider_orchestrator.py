"""Provider orchestration for AI model invocation with fallback logic."""
import io
from typing import Optional, Dict, Any, List, Union, cast, TYPE_CHECKING

from src.logger.logger import Logger
from src.platforms.ai_providers.response_models import ChatResponseModel
from src.managers.provider_types import ProviderMetadata, InvocationResult, ProviderClients

if TYPE_CHECKING:
    from src.config.protocol import ConfigProtocol


class ProviderOrchestrator:
    """
    Orchestrates AI provider invocation with fallback and retry logic.

    Responsibilities:
    - Provider metadata management
    - Single provider invocation
    - Multi-provider fallback chains
    - Google free/paid tier fallback
    - Response validation and rate limit detection
    """

    def __init__(
        self,
        logger: Logger,
        config: "ConfigProtocol",
        clients: ProviderClients
    ) -> None:
        """
        Initialize the provider orchestrator.

        Args:
            logger: Logger instance
            config: Configuration instance
            clients: Container with all AI provider clients
        """
        self.logger = logger
        self.config = config
        self.clients = clients
        self._providers = self._build_provider_metadata()

    def _build_provider_metadata(self) -> Dict[str, ProviderMetadata]:
        """Build provider metadata registry from clients and config."""
        return {
            'googleai': ProviderMetadata(
                name='Google AI Studio',
                client=self.clients.google,
                paid_client=self.clients.google_paid,
                default_model=self.config.GOOGLE_STUDIO_MODEL,
                config=self.config.get_model_config(self.config.GOOGLE_STUDIO_MODEL),
                supports_chart=True,
                has_rate_limits=True
            ),
            'openrouter': ProviderMetadata(
                name='OpenRouter',
                client=self.clients.openrouter,
                default_model=self.config.OPENROUTER_BASE_MODEL,
                config=self.config.get_model_config(self.config.OPENROUTER_BASE_MODEL),
                supports_chart=True,
                has_rate_limits=True
            ),
            'local': ProviderMetadata(
                name='LM Studio',
                client=self.clients.lmstudio,
                default_model=self.config.LM_STUDIO_MODEL,
                config=self.config.get_model_config(self.config.LM_STUDIO_MODEL),
                supports_chart=False,
                has_rate_limits=False
            ),
            'blockrun': ProviderMetadata(
                name='BlockRun.AI',
                client=self.clients.blockrun,
                default_model=self.config.BLOCKRUN_MODEL,
                config=self.config.get_model_config(self.config.BLOCKRUN_MODEL),
                supports_chart=True,
                has_rate_limits=False
            )
        }

    def get_metadata(self, provider: str) -> Optional[ProviderMetadata]:
        """Get metadata for a provider."""
        return self._providers.get(provider)

    def resolve_model(self, provider: str, model_override: Optional[str] = None) -> str:
        """Resolve effective model name for a provider."""
        if model_override:
            return model_override
        metadata = self._providers.get(provider)
        return metadata.default_model if metadata else "unknown-model"

    @staticmethod
    def _openrouter_model_supports_chart(model_name: str) -> bool:
        """Return whether an OpenRouter model can accept image input."""
        normalized = (model_name or "").strip().lower()
        if not normalized:
            return False
        # Current OpenRouter default profiles use gpt-oss for text-only analysis.
        return "gpt-oss" not in normalized

    def is_available(self, provider: str) -> bool:
        """Check if a provider is available."""
        metadata = self._providers.get(provider)
        return metadata.is_available() if metadata else False

    def _openrouter_best_chart_model(self, model_override: Optional[str] = None) -> Optional[str]:
        """Return the best available OpenRouter model for chart requests, or None."""
        # Explicit override takes priority
        if model_override and self._openrouter_model_supports_chart(model_override):
            return model_override
        # Base model
        base = self.resolve_model("openrouter")
        if self._openrouter_model_supports_chart(base):
            return base
        # Fallback model
        fallback = self.config.OPENROUTER_FALLBACK_MODEL
        if fallback and self._openrouter_model_supports_chart(fallback):
            return fallback
        return None

    def supports_chart(self, provider: str, model_override: Optional[str] = None) -> bool:
        """Check if provider and resolved model support chart analysis."""
        if provider == "all":
            return (
                self.supports_chart("googleai")
                or self.supports_chart("openrouter", model_override)
                or self.supports_chart("blockrun")
            )
        metadata = self._providers.get(provider)
        if not metadata or not metadata.is_available() or not metadata.supports_chart:
            return False
        if provider == "openrouter":
            return self._openrouter_best_chart_model(model_override) is not None
        return True

    async def invoke(
        self,
        provider: str,
        messages: List[Dict[str, str]],
        *,
        chart: bool = False,
        chart_image: Optional[Union[io.BytesIO, bytes, str]] = None,
        model: Optional[str] = None
    ) -> InvocationResult:
        """
        Invoke a single provider and return structured result.

        Args:
            provider: Provider key (googleai, openrouter, local)
            messages: Chat messages
            chart: Whether this is a chart analysis request
            chart_image: Optional chart image for analysis
            model: Optional model override

        Returns:
            InvocationResult with success status, response, and metadata
        """
        metadata = self._providers.get(provider)
        if not metadata or not metadata.is_available():
            return InvocationResult(
                success=False,
                response=ChatResponseModel.from_error(f"Provider '{provider}' is not available"),
                provider=provider,
                model=self.resolve_model(provider, model)
            )
        effective_model = self.resolve_model(provider, model)
        if provider == "googleai":
            return await self._invoke_google(metadata, messages, effective_model, chart, chart_image)
        if provider == "local":
            return await self._invoke_local(metadata, messages, effective_model, chart)
        if provider == "openrouter":
            return await self._invoke_openrouter(metadata, messages, effective_model, chart, chart_image)
        if provider == "blockrun":
            return await self._invoke_blockrun(metadata, messages, effective_model, chart, chart_image)
        return InvocationResult(
            success=False,
            response=ChatResponseModel.from_error(f"Unknown provider '{provider}'"),
            provider=provider,
            model=effective_model
        )

    async def invoke_with_fallback(
        self,
        providers: List[str],
        messages: List[Dict[str, str]],
        *,
        chart: bool = False,
        chart_image: Optional[Union[io.BytesIO, bytes, str]] = None,
        model: Optional[str] = None
    ) -> InvocationResult:
        """
        Try providers in order, returning first successful result.

        Args:
            providers: List of provider keys to try in order
            messages: Chat messages
            chart: Whether this is a chart analysis request
            chart_image: Optional chart image
            model: Optional model override

        Returns:
            InvocationResult from first successful provider, or last failure
        """
        last_result: Optional[InvocationResult] = None
        for provider in providers:
            if not self.is_available(provider):
                continue
            effective_model = self.resolve_model(provider, model)
            self._log_attempt(provider, effective_model, chart)
            result = await self.invoke(provider, messages, chart=chart, chart_image=chart_image, model=model)
            if result.success and not self._is_rate_limited(result.response):
                return result
            self._log_failure(provider)
            last_result = result
        return last_result or InvocationResult(
            success=False,
            response=ChatResponseModel.from_error("No providers available"),
            provider="none",
            model="none"
        )

    async def get_text_response(
        self,
        effective_provider: str,
        messages: List[Dict[str, str]],
        model: Optional[str] = None
    ) -> InvocationResult:
        """
        Get text response using single provider or fallback chain.

        Args:
            effective_provider: Provider key or 'all' for fallback chain
            messages: Chat messages
            model: Optional model override

        Returns:
            InvocationResult with response
        """
        if effective_provider == "all":
            order = self.config.ALL_PROVIDER_ORDER
            self.logger.info(
                "[all] Provider probe order: %s", " → ".join(order)
            )
            result = await self.invoke_with_fallback(order, messages, model=model)
            return result
        if self.is_available(effective_provider):
            effective_model = self.resolve_model(effective_provider, model)
            self.logger.info("Using %s model: %s", self._providers[effective_provider].name, effective_model)
            result = await self.invoke(effective_provider, messages, model=model)
            if result.success:
                return result
            # Hot fallback: if the primary provider had a transient error, try others
            if self._is_transient_failure(result):
                fallback_order = self._hot_fallback_order(effective_provider)
                if fallback_order:
                    self.logger.warning(
                        "%s transient failure (%s) — hot-switching to: %s",
                        effective_provider,
                        result.response.error if result.response else "unknown",
                        " → ".join(fallback_order),
                    )
                    return await self.invoke_with_fallback(fallback_order, messages, model=model)
            return result
        self._log_unavailable_guidance(effective_provider)
        return InvocationResult(
            success=False,
            response=ChatResponseModel.from_error(f"Provider '{effective_provider}' is not available"),
            provider=effective_provider,
            model=self.resolve_model(effective_provider, model)
        )

    async def get_chart_response(
        self,
        effective_provider: str,
        messages: List[Dict[str, str]],
        chart_image: Union[io.BytesIO, bytes, str],
        model: Optional[str] = None
    ) -> InvocationResult:
        """
        Get chart analysis response using single provider or fallback chain.

        Args:
            effective_provider: Provider key or 'all' for fallback chain
            messages: Chat messages
            chart_image: Chart image for analysis
            model: Optional model override

        Returns:
            InvocationResult with response
        """
        if effective_provider == "all":
            # Local has no vision, and OpenRouter depends on the resolved model.
            order = [
                p for p in self.config.ALL_PROVIDER_ORDER
                if p != "local" and self.supports_chart(p, model)
            ]
            self.logger.info(
                "[all/chart] Provider probe order (vision-capable): %s", " → ".join(order)
            )
            return await self.invoke_with_fallback(
                order, messages, chart=True, chart_image=chart_image, model=model
            )
        if effective_provider == "local":
            return InvocationResult(
                success=False,
                response=ChatResponseModel.from_error("Chart analysis unavailable - local models don't support images"),
                provider="local",
                model=self.resolve_model("local", model)
            )
        if self.is_available(effective_provider) and self.supports_chart(effective_provider, model):
            if effective_provider == "openrouter":
                effective_model = self._openrouter_best_chart_model(model)
                if effective_model != self.resolve_model(effective_provider, model):
                    self.logger.info(
                        "OpenRouter base model does not support charts — using fallback model for chart analysis: %s",
                        effective_model,
                    )
                else:
                    self.logger.info("Using OpenRouter for chart analysis: %s", effective_model)
            else:
                effective_model = self.resolve_model(effective_provider, model)
                self.logger.info("Using %s for chart analysis: %s", self._providers[effective_provider].name, effective_model)
            result = await self.invoke(effective_provider, messages, chart=True, chart_image=chart_image, model=effective_model)
            if result.success:
                return result
            # Hot fallback for chart: try other vision-capable providers on transient errors
            if self._is_transient_failure(result):
                fallback_order = [
                    p for p in self._hot_fallback_order(effective_provider)
                    if self.supports_chart(p, model)
                ]
                if fallback_order:
                    self.logger.warning(
                        "%s chart transient failure (%s) — hot-switching to: %s",
                        effective_provider,
                        result.response.error if result.response else "unknown",
                        " → ".join(fallback_order),
                    )
                    return await self.invoke_with_fallback(
                        fallback_order, messages, chart=True, chart_image=chart_image, model=model
                    )
            return result
        self._log_unavailable_guidance(effective_provider)
        return InvocationResult(
            success=False,
            response=ChatResponseModel.from_error(f"Provider '{effective_provider}' is not available for chart analysis"),
            provider=effective_provider,
            model=self.resolve_model(effective_provider, model)
        )

    async def _invoke_google(
        self,
        metadata: ProviderMetadata,
        messages: List[Dict[str, str]],
        effective_model: str,
        chart: bool,
        chart_image: Optional[Union[io.BytesIO, bytes, str]]
    ) -> InvocationResult:
        """Invoke Google AI with free/paid tier fallback logic."""
        is_free_tier_model = "flash" in effective_model.lower()
        tier_info = "free tier" if is_free_tier_model else "paid tier"
        self.logger.info("Attempting with Google AI %s API (model: %s)", tier_info, effective_model)
        if chart and chart_image:
            response = await metadata.client.chat_completion_with_chart_analysis(
                effective_model, messages, cast(Any, chart_image), metadata.config
            )
        else:
            response = await metadata.client.chat_completion(effective_model, messages, metadata.config)
        error_type = response.error if response else None
        if error_type and ("overloaded" in error_type or "rate_limit" in error_type) and metadata.paid_client:
            error_reason = "rate limited" if error_type == "rate_limit" else "overloaded"
            self.logger.warning("Google AI free tier %s, retrying with paid API key", error_reason)
            if chart and chart_image:
                response = await metadata.paid_client.chat_completion_with_chart_analysis(
                    effective_model, messages, cast(Any, chart_image), metadata.config
                )
            else:
                response = await metadata.paid_client.chat_completion(effective_model, messages, metadata.config)
            if self._is_valid_response(response):
                self.logger.info("Successfully used paid Google AI API after free tier %s", error_reason)
                return InvocationResult(
                    success=True,
                    response=response,
                    provider="google",
                    model=effective_model,
                    used_paid_tier=True
                )
            paid_error = response.error if response else "no response"
            self.logger.error("Paid Google AI API also failed: %s", paid_error)
            return InvocationResult(
                success=False,
                response=response,
                provider="google",
                model=effective_model,
                used_paid_tier=True
            )
        if self._is_valid_response(response):
            tier_success = "free tier" if is_free_tier_model else "paid tier"
            self.logger.info("Successfully used %s Google AI API", tier_success)
            return InvocationResult(
                success=True,
                response=response,
                provider="google",
                model=effective_model,
                used_paid_tier=not is_free_tier_model
            )
        return InvocationResult(
            success=False,
            response=response,
            provider="google",
            model=effective_model
        )

    async def _invoke_local(
        self,
        metadata: ProviderMetadata,
        messages: List[Dict[str, str]],
        effective_model: str,
        chart: bool
    ) -> InvocationResult:
        """Invoke LM Studio local provider."""
        if chart:
            return InvocationResult(
                success=False,
                response=ChatResponseModel.from_error("Chart analysis unavailable - local models don't support images"),
                provider="lmstudio",
                model=effective_model
            )
        try:
            response = await metadata.client.chat_completion(effective_model, messages, metadata.config)
            success = self._is_valid_response(response)
            return InvocationResult(
                success=success,
                response=response,
                provider="lmstudio",
                model=effective_model
            )
        except Exception as e:
            return InvocationResult(
                success=False,
                response=ChatResponseModel.from_error(f"LM Studio connection failed: {str(e)}"),
                provider="lmstudio",
                model=effective_model
            )

    async def _invoke_openrouter(
        self,
        metadata: ProviderMetadata,
        messages: List[Dict[str, str]],
        effective_model: str,
        chart: bool,
        chart_image: Optional[Union[io.BytesIO, bytes, str]]
    ) -> InvocationResult:
        """Invoke OpenRouter provider."""
        if chart and chart_image:
            response = await metadata.client.chat_completion_with_chart_analysis(
                effective_model, messages, cast(Any, chart_image), metadata.config
            )
        else:
            response = await metadata.client.chat_completion(effective_model, messages, metadata.config)
        success = self._is_valid_response(response) and not self._is_rate_limited(response)
        return InvocationResult(
            success=success,
            response=response,
            provider="openrouter",
            model=effective_model
        )

    def _is_valid_response(self, response: Optional[ChatResponseModel]) -> bool:
        """Check if response contains valid choices with content."""
        if not response:
            return False
        if not response.choices:
            return False
        first_choice = response.choices[0]
        if first_choice.error:
            error_detail = first_choice.error
            error_code = error_detail.get('code', 'unknown') if isinstance(error_detail, dict) else 'unknown'
            error_msg = error_detail.get('message', 'unknown') if isinstance(error_detail, dict) else str(error_detail)
            provider = error_detail.get('metadata', {}).get('provider_name', 'unknown') if isinstance(error_detail, dict) else 'unknown'
            self.logger.error("Error in API response choice from %s: [%s] %s", provider, error_code, error_msg)
            self.logger.debug("Full error details: %s", error_detail)
            return False
        content = first_choice.message.content if first_choice.message else ""
        if not content:
            self.logger.debug("Empty content in API response choice. Message: %s", first_choice.message)
            return False
        return True

    def _is_rate_limited(self, response: Optional[ChatResponseModel]) -> bool:
        """Check if response indicates rate limiting."""
        return bool(response and response.error and "rate_limit" in response.error)

    def _is_transient_failure(self, result: InvocationResult) -> bool:
        """Return True when a provider failed with a retriable/transient error.

        Transient errors (rate_limit, overload, timeout, connection) justify
        trying another provider at runtime instead of giving up immediately.
        """
        if result.success:
            return False
        error = (result.response.error if result.response else None) or ""
        TRANSIENT_PREFIXES = ("rate_limit", "overloaded", "timeout", "connection")
        return any(error.startswith(p) for p in TRANSIENT_PREFIXES)

    def _hot_fallback_order(self, failed_provider: str) -> List[str]:
        """Return available providers to try after *failed_provider* failed.

        Uses ALL_PROVIDER_ORDER as the priority list so the user's preferred
        order is respected, minus the provider that just failed.
        """
        return [
            p for p in self.config.ALL_PROVIDER_ORDER
            if p != failed_provider and self.is_available(p)
        ]

    def _log_attempt(self, provider: str, model: str, chart: bool) -> None:
        """Log provider attempt."""
        metadata = self._providers.get(provider)
        if not metadata:
            return
        noun = "chart analysis" if chart else "request"
        self.logger.info("Attempting %s with %s model: %s", noun, metadata.name, model)

    def _log_failure(self, provider: str) -> None:
        """Log provider failure with appropriate message."""
        if provider == "googleai":
            self.logger.warning("Google AI Studio model failed. Trying alternatives...")
        elif provider == "local":
            self.logger.warning("LM Studio failed. Falling back to next provider.")
        elif provider == "openrouter":
            self.logger.warning("OpenRouter failed or rate limited.")

    def _log_unavailable_guidance(self, provider: str) -> None:
        """Log guidance when provider is unavailable."""
        metadata = self._providers.get(provider)
        if not metadata or not metadata.client:
            if provider == "openrouter":
                self.logger.error("OpenRouter client not initialized. Check OPENROUTER_API_KEY in keys.env")
            elif provider == "googleai":
                self.logger.error("Google AI client not initialized. Check GOOGLE_STUDIO_API_KEY in keys.env")
            elif provider == "local":
                self.logger.error("LM Studio client not initialized. Check LM_STUDIO_BASE_URL in config.ini")
            elif provider == "blockrun":
                self.logger.error("BlockRun client not initialized. Check BLOCKRUN_WALLET_KEY in keys.env")
        elif provider == "local":
            self.logger.error("Local models don't support image analysis")

    async def _invoke_blockrun(
        self,
        metadata: ProviderMetadata,
        messages: List[Dict[str, str]],
        effective_model: str,
        chart: bool,
        chart_image: Optional[Union[io.BytesIO, bytes, str]]
    ) -> InvocationResult:
        """Invoke BlockRun provider."""
        if chart and chart_image:
            response = await metadata.client.chat_completion_with_chart_analysis(
                effective_model, messages, cast(Any, chart_image), metadata.config
            )
        else:
            response = await metadata.client.chat_completion(effective_model, messages, metadata.config)
        success = self._is_valid_response(response)
        return InvocationResult(
            success=success,
            response=response,
            provider="blockrun",
            model=effective_model
        )
