"""LLM Gateway & Provider Abstraction Service.

Provides an extensible, resilient multi-provider LLM gateway supporting Google Gemini
(via modern google-genai SDK) as primary provider and Ollama as local fallback, with
transient retry policies, strict non-fallback authentication boundaries, and safe PII logging.
"""

from abc import ABC, abstractmethod
import asyncio
import logging
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

import httpx
from google import genai
from google.genai import types
from google.genai.errors import APIError, ClientError, ServerError

from app.core.config import Settings, settings as default_settings
from app.core.exceptions import (
    LLMAuthenticationError,
    LLMError,
    LLMProviderError,
    LLMTimeoutError,
)
from app.core.logging import configure_safe_logger
from app.services.pii_masker import PIIMasker, get_pii_masker

logger = configure_safe_logger(logging.getLogger(__name__))


class LLMResponse(BaseModel):
    """Normalized response returned by all LLM providers."""

    content: str
    provider: str
    model: str
    usage: Dict[str, Any] = Field(default_factory=dict)


class BaseLLMProvider(ABC):
    """Abstract base class for all LLM inference providers."""

    provider_name: str

    @abstractmethod
    async def generate(
        self,
        prompt: str,
        system_instruction: Optional[str] = None,
        temperature: float = 0.7,
        max_output_tokens: Optional[int] = None,
        timeout_seconds: Optional[float] = None,
        model_override: Optional[str] = None,
    ) -> LLMResponse:
        """Generates a text completion for the provided prompt."""
        pass


class GeminiProvider(BaseLLMProvider):
    """Primary Cloud LLM Provider using the modern Google GenAI SDK."""

    provider_name: str = "gemini"

    def __init__(
        self,
        api_key: Optional[str] = None,
        default_model: Optional[str] = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.api_key = api_key
        self.default_model = default_model or default_settings.GEMINI_MODEL
        self.timeout_seconds = timeout_seconds
        self._client: Optional[genai.Client] = None

    def _get_client(self) -> genai.Client:
        """Initializes or returns the Google GenAI client."""
        if not self.api_key:
            raise LLMAuthenticationError(
                message="Gemini API key is not configured or missing.",
                provider="gemini",
            )
        if self._client is None:
            self._client = genai.Client(api_key=self.api_key)
        return self._client

    async def generate(
        self,
        prompt: str,
        system_instruction: Optional[str] = None,
        temperature: float = 0.7,
        max_output_tokens: Optional[int] = None,
        timeout_seconds: Optional[float] = None,
        model_override: Optional[str] = None,
    ) -> LLMResponse:
        """Executes inference via the Google GenAI SDK."""
        client = self._get_client()
        target_model = model_override or self.default_model
        timeout = timeout_seconds or self.timeout_seconds

        config = types.GenerateContentConfig(
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            system_instruction=system_instruction,
        )

        try:
            # Enforce timeout via asyncio.wait_for
            coro = client.aio.models.generate_content(
                model=target_model,
                contents=prompt,
                config=config,
            )
            response = await asyncio.wait_for(coro, timeout=timeout)

            content = response.text or ""
            usage: Dict[str, Any] = {}
            if getattr(response, "usage_metadata", None):
                meta = response.usage_metadata
                usage = {
                    "prompt_tokens": getattr(meta, "prompt_token_count", 0),
                    "candidates_tokens": getattr(meta, "candidates_token_count", 0),
                    "total_tokens": getattr(meta, "total_token_count", 0),
                }

            return LLMResponse(
                content=content,
                provider=self.provider_name,
                model=target_model,
                usage=usage,
            )

        except asyncio.TimeoutError as exc:
            logger.warning("Gemini generation timed out after %ss for model '%s'.", timeout, target_model)
            raise LLMTimeoutError(
                message=f"Gemini request timed out after {timeout}s.",
                provider=self.provider_name,
                timeout_seconds=timeout,
            ) from exc

        except APIError as exc:
            code = getattr(exc, "code", None)
            message = getattr(exc, "message", str(exc))

            # Authentication / Credential Errors -> 401 / 403
            if code in (401, 403):
                logger.error("Gemini authentication failure (HTTP %s): %s", code, message)
                raise LLMAuthenticationError(
                    message=f"Gemini authentication failed (HTTP {code}): {message}",
                    provider=self.provider_name,
                ) from exc

            # Client / Invalid Request Errors -> 400
            if code == 400:
                logger.error("Gemini invalid request error (HTTP 400): %s", message)
                raise LLMProviderError(
                    message=f"Gemini rejected invalid request: {message}",
                    provider=self.provider_name,
                    detail={"status_code": 400, "error": message},
                ) from exc

            # Transient errors -> 429, 500, 503, 504
            logger.warning("Gemini API error (HTTP %s): %s", code, message)
            raise LLMProviderError(
                message=f"Gemini API failure (HTTP {code}): {message}",
                provider=self.provider_name,
                detail={"status_code": code, "error": message, "is_transient": True},
            ) from exc

        except Exception as exc:
            logger.error("Unexpected error in Gemini generation: %s", str(exc))
            raise LLMProviderError(
                message=f"Gemini generation failed: {exc}",
                provider=self.provider_name,
            ) from exc


class OllamaProvider(BaseLLMProvider):
    """Local inference fallback provider using Ollama HTTP API."""

    provider_name: str = "ollama"

    def __init__(
        self,
        base_url: Optional[str] = None,
        default_model: Optional[str] = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.base_url = (base_url or default_settings.OLLAMA_BASE_URL).rstrip("/")
        self.default_model = default_model or default_settings.OLLAMA_MODEL
        self.timeout_seconds = timeout_seconds

    async def generate(
        self,
        prompt: str,
        system_instruction: Optional[str] = None,
        temperature: float = 0.7,
        max_output_tokens: Optional[int] = None,
        timeout_seconds: Optional[float] = None,
        model_override: Optional[str] = None,
    ) -> LLMResponse:
        """Executes inference via the Ollama generate endpoint."""
        target_model = model_override or self.default_model
        timeout = timeout_seconds or self.timeout_seconds

        payload: Dict[str, Any] = {
            "model": target_model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": temperature,
            },
        }
        if system_instruction:
            payload["system"] = system_instruction
        if max_output_tokens:
            payload["options"]["num_predict"] = max_output_tokens

        url = f"{self.base_url}/api/generate"

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(url, json=payload)

            if resp.status_code == 404:
                raise LLMProviderError(
                    message=f"Ollama model '{target_model}' not found at {self.base_url}.",
                    provider=self.provider_name,
                    detail={"status_code": 404},
                )
            if resp.status_code == 400:
                raise LLMProviderError(
                    message=f"Invalid request to Ollama: {resp.text}",
                    provider=self.provider_name,
                    detail={"status_code": 400},
                )
            resp.raise_for_status()

            data = resp.json()
            content = data.get("response", "")
            usage = {
                "prompt_tokens": data.get("prompt_eval_count", 0),
                "completion_tokens": data.get("eval_count", 0),
                "total_tokens": data.get("prompt_eval_count", 0) + data.get("eval_count", 0),
            }

            return LLMResponse(
                content=content,
                provider=self.provider_name,
                model=target_model,
                usage=usage,
            )

        except httpx.TimeoutException as exc:
            logger.warning("Ollama inference timed out after %ss for model '%s'.", timeout, target_model)
            raise LLMTimeoutError(
                message=f"Ollama request timed out after {timeout}s.",
                provider=self.provider_name,
                timeout_seconds=timeout,
            ) from exc

        except httpx.ConnectError as exc:
            logger.warning("Could not connect to Ollama at %s: %s", self.base_url, exc)
            raise LLMProviderError(
                message=f"Could not connect to Ollama service at {self.base_url}: {exc}",
                provider=self.provider_name,
                detail={"is_transient": True},
            ) from exc

        except (LLMProviderError, LLMTimeoutError):
            raise

        except Exception as exc:
            logger.error("Ollama inference failure: %s", str(exc))
            raise LLMProviderError(
                message=f"Ollama inference failed: {exc}",
                provider=self.provider_name,
            ) from exc


class LLMGateway:
    """Enterprise LLM Gateway with retry policies, fallback orchestration, and safe logging."""

    def __init__(
        self,
        settings: Optional[Settings] = None,
        gemini_provider: Optional[BaseLLMProvider] = None,
        ollama_provider: Optional[BaseLLMProvider] = None,
        pii_masker: Optional[PIIMasker] = None,
    ) -> None:
        self.settings = settings or default_settings
        self.pii_masker = pii_masker or get_pii_masker()

        # Initialize providers
        self.gemini = gemini_provider or GeminiProvider(
            api_key=self.settings.GEMINI_API_KEY,
            default_model=self.settings.GEMINI_MODEL,
            timeout_seconds=self.settings.LLM_TIMEOUT_SECONDS,
        )
        self.ollama = ollama_provider or OllamaProvider(
            base_url=self.settings.OLLAMA_BASE_URL,
            default_model=self.settings.OLLAMA_MODEL,
            timeout_seconds=self.settings.LLM_TIMEOUT_SECONDS,
        )

        self._providers: Dict[str, BaseLLMProvider] = {
            "gemini": self.gemini,
            "ollama": self.ollama,
        }

    def register_provider(self, name: str, provider: BaseLLMProvider) -> None:
        """Allows registering custom or mock providers for extensibility and testing."""
        self._providers[name.lower()] = provider

    def get_provider(self, name: str) -> BaseLLMProvider:
        """Retrieves provider instance by name."""
        name_clean = name.strip().lower()
        if name_clean not in self._providers:
            raise LLMProviderError(
                message=f"Unknown LLM provider '{name}'. Available: {list(self._providers.keys())}",
                provider=name,
            )
        return self._providers[name_clean]

    def _is_transient_error(self, exc: Exception) -> bool:
        """Identifies transient errors eligible for retry and fallback."""
        if isinstance(exc, (LLMTimeoutError, asyncio.TimeoutError, httpx.TimeoutException)):
            return True
        if isinstance(exc, (httpx.ConnectError, httpx.NetworkError)):
            return True
        if isinstance(exc, LLMProviderError):
            detail = exc.detail
            if isinstance(detail, dict):
                if detail.get("is_transient"):
                    return True
                status_code = detail.get("status_code")
                if status_code in (429, 500, 502, 503, 504):
                    return True
        return False

    async def generate(
        self,
        prompt: str,
        system_instruction: Optional[str] = None,
        temperature: float = 0.7,
        max_output_tokens: Optional[int] = None,
        provider_override: Optional[str] = None,
        model_override: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
    ) -> LLMResponse:
        """Orchestrates generation through primary provider, retrying transient errors

        and selectively failing over to Ollama when appropriate.
        """
        if prompt is None or not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("Prompt cannot be empty, None, or whitespace-only.")

        primary_name = (provider_override or self.settings.PRIMARY_LLM_PROVIDER).strip().lower()
        primary_provider = self.get_provider(primary_name)
        max_retries = self.settings.LLM_MAX_RETRIES

        # Safe PII logging of incoming prompt preview
        masked_prompt_preview = self.pii_masker.mask_text(prompt[:120])
        logger.info(
            "Dispatching LLM prompt to provider '%s' (preview: %s...)",
            primary_name,
            masked_prompt_preview,
        )

        last_primary_error: Optional[Exception] = None

        # Execute primary with transient retries
        for attempt in range(max_retries + 1):
            try:
                response = await primary_provider.generate(
                    prompt=prompt,
                    system_instruction=system_instruction,
                    temperature=temperature,
                    max_output_tokens=max_output_tokens,
                    timeout_seconds=timeout_seconds,
                    model_override=model_override,
                )
                logger.info(
                    "LLM generation succeeded via provider '%s' (model: %s).",
                    response.provider,
                    response.model,
                )
                return response

            except LLMAuthenticationError:
                # STRICT RULE: Never retry and never fallback for authentication errors
                logger.error("Provider '%s' authentication failed. Aborting without fallback.", primary_name)
                raise

            except LLMProviderError as exc:
                # STRICT RULE: Never retry or fallback for client 400 invalid requests
                if isinstance(exc.detail, dict) and exc.detail.get("status_code") == 400:
                    logger.error("Provider '%s' rejected invalid request (400). Aborting without fallback.", primary_name)
                    raise

                last_primary_error = exc
                if not self._is_transient_error(exc) or attempt == max_retries:
                    break
                await asyncio.sleep(0.05 * (2 ** attempt))

            except (LLMTimeoutError, asyncio.TimeoutError) as exc:
                last_primary_error = exc
                if attempt == max_retries:
                    break
                await asyncio.sleep(0.05 * (2 ** attempt))

            except Exception as exc:
                last_primary_error = exc
                if not self._is_transient_error(exc) or attempt == max_retries:
                    break
                await asyncio.sleep(0.05 * (2 ** attempt))

        # Check fallback eligibility
        fallback_provider_name = (getattr(self.settings, "LLM_FALLBACK_PROVIDER", "ollama") or "").strip().lower()
        fallback_eligible = (
            self.settings.ENABLE_OLLAMA_FALLBACK
            and fallback_provider_name not in ("none", "false", "")
            and primary_name == "gemini"
            and fallback_provider_name in self._providers
            and self._is_transient_error(last_primary_error)
        )

        if fallback_eligible:
            logger.warning(
                "Primary provider 'gemini' exhausted retries (%s). Initiating fallback to '%s'.",
                str(last_primary_error),
                fallback_provider_name,
            )
            try:
                fallback_provider = self.get_provider(fallback_provider_name)
                fallback_resp = await fallback_provider.generate(
                    prompt=prompt,
                    system_instruction=system_instruction,
                    temperature=temperature,
                    max_output_tokens=max_output_tokens,
                    timeout_seconds=timeout_seconds,
                )
                logger.info("Fallback generation succeeded via Ollama (model: %s).", fallback_resp.model)
                return fallback_resp

            except Exception as fallback_exc:
                logger.error(
                    "Both primary ('gemini') and fallback ('ollama') providers failed. "
                    "Primary error: %s; Fallback error: %s",
                    str(last_primary_error),
                    str(fallback_exc),
                )
                raise LLMProviderError(
                    message=(
                        f"All LLM providers failed. Primary ('gemini') failed with: {last_primary_error}. "
                        f"Fallback ('ollama') failed with: {fallback_exc}."
                    ),
                    provider="gateway",
                    detail={"primary_error": str(last_primary_error), "fallback_error": str(fallback_exc)},
                ) from fallback_exc

        # If not fallback eligible, re-raise primary error
        if isinstance(last_primary_error, LLMError):
            raise last_primary_error
        raise LLMProviderError(
            message=f"LLM provider '{primary_name}' failed: {last_primary_error}",
            provider=primary_name,
        ) from last_primary_error


_default_llm_gateway: Optional[LLMGateway] = None


def get_llm_gateway() -> LLMGateway:
    """Returns a singleton instance of the LLMGateway."""
    global _default_llm_gateway
    if _default_llm_gateway is None:
        _default_llm_gateway = LLMGateway()
    return _default_llm_gateway

