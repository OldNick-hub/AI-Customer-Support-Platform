"""Bidirectional Neural Translation Service with PII Masking & Ephemeral Redis Caching.

Translates between English and supported customer languages (Spanish, French, German, Hindi)
using the existing LLMGateway at temperature 0.0 with bounded latency and strict fail-closed
degradation (Zero Silent English).

Security & Privacy Guarantees:
- Raw PII is masked via PIIMasker BEFORE any external LLM invocation.
- Business identifiers (orders, SKUs, dates, amounts) and citations are masked via
  EntityPreservationVault using 128-bit cryptographic tokens.
- Shared Redis translation cache stores ONLY normalized, PII-free text templates with 24h TTL.
- Redis outages degrade gracefully to direct LLM execution without failing requests.
- Translation failures return pre-rendered localized customer notices.
"""

import hashlib
import logging
import re
import time
from typing import Dict, List, Optional, Set, Tuple
from pydantic import BaseModel, Field

from app.core.exceptions import LLMAuthenticationError, LLMProviderError, LLMTimeoutError
from app.core.logging import configure_safe_logger
from app.modules.multilingual.config import (
    LOCALIZED_SERVICE_NOTICES,
    REDIS_TRANSLATION_TTL_SECONDS,
    SUPPORTED_LANGUAGES,
)
from app.modules.multilingual.entity_preserver import (
    ANY_PLACEHOLDER_PATTERN,
    EntityPreservationVault,
)
from app.services.llm_gateway import LLMGateway, get_llm_gateway
from app.services.pii_masker import PIIMasker, get_pii_masker
from app.services.redis_service import RedisService, get_redis_service

logger = configure_safe_logger(logging.getLogger(__name__))

# Mapping of ISO codes to human-readable language names for prompt construction
LANGUAGE_NAMES: Dict[str, str] = {
    "en": "English",
    "es": "Spanish",
    "fr": "French",
    "de": "German",
    "hi": "Hindi",
}

# Regex to detect normalized cache placeholders: __NORM_{TYPE}_{INDEX}__
NORM_PLACEHOLDER_PATTERN = re.compile(r"__NORM_([A-Za-z0-9_]+)_(\d+)__")


class TranslationResult(BaseModel):
    """Structured outcome of bidirectional translation."""

    translated_text: str
    is_successful: bool = True
    source_language: str
    target_language: str
    cached: bool = False
    error_reason: Optional[str] = None
    latency_ms: float = 0.0
    model_used: str = ""


class Translator:
    """Enterprise Multilingual Translation Engine.

    Coordinates pre-translation PII masking, 128-bit entity preservation,
    PII-free Redis caching, and resilient neural translation via LLMGateway.
    """

    PROMPT_VERSION: str = "v1.0"

    def __init__(
        self,
        llm_gateway: Optional[LLMGateway] = None,
        redis_service: Optional[RedisService] = None,
        pii_masker: Optional[PIIMasker] = None,
        cache_ttl_seconds: int = REDIS_TRANSLATION_TTL_SECONDS,
        timeout_seconds: float = 10.0,
        max_output_tokens: int = 2000,
        prompt_version: str = "v1.0",
        model_name: Optional[str] = None,
    ) -> None:
        self.llm_gateway = llm_gateway
        self.redis_service = redis_service
        self.pii_masker = pii_masker
        self.cache_ttl_seconds = cache_ttl_seconds
        self.timeout_seconds = timeout_seconds
        self.max_output_tokens = max_output_tokens
        self.prompt_version = prompt_version
        self.model_name = model_name

    def _get_llm_gateway(self) -> LLMGateway:
        if self.llm_gateway is None:
            self.llm_gateway = get_llm_gateway()
        return self.llm_gateway

    def _get_redis_service(self) -> RedisService:
        if self.redis_service is None:
            self.redis_service = get_redis_service()
        return self.redis_service

    def _get_pii_masker(self) -> PIIMasker:
        if self.pii_masker is None:
            self.pii_masker = get_pii_masker()
        return self.pii_masker

    def _build_system_instruction(self, source_lang: str, target_lang: str) -> str:
        """Constructs a strict, constrained system prompt for deterministic translation."""
        source_name = LANGUAGE_NAMES.get(source_lang, source_lang)
        target_name = LANGUAGE_NAMES.get(target_lang, target_lang)

        instruction = (
            f"You are a professional, accurate translation engine for an enterprise customer support platform. "
            f"Translate the provided text from {source_name} to {target_name}.\n"
            f"STRICT RULES:\n"
            f"1. Accurately preserve customer support meaning, polite tone, and terminology.\n"
            f"2. CRITICAL PRESERVATION: Any placeholder tokens matching __NORM_...__ or __PRESERVED_...__ or "
            f"[SSN], [CREDIT_CARD], [EMAIL_ADDRESS], [PHONE_NUMBER], [CVV] MUST BE PRESERVED VERBATIM AND UNCHANGED. "
            f"Never translate, renumber, omit, or alter any placeholder tokens.\n"
        )
        if target_lang == "hi":
            instruction += "3. For Hindi, use natural, polite customer support phrasing.\n"
        elif source_lang == "hi" and target_lang == "en":
            instruction += "3. Understand colloquial and Romanized Hindi (Hinglish) and translate to formal English.\n"

        instruction += "4. Return ONLY the translated text without commentary, quotes, notes, or prefixes."
        return instruction

    def _normalize_placeholders_for_cache(
        self, text: str
    ) -> Tuple[str, Dict[str, str], Dict[str, str]]:
        """Replaces request-specific 128-bit placeholders with deterministic normalized tags.

        This allows identical phrasing from different requests/customers to share
        cached translation templates without leaking unique request tokens.

        Returns:
            (normalized_text, norm_to_vault_map, vault_to_norm_map)
        """
        vault_tokens = ANY_PLACEHOLDER_PATTERN.findall(text)
        if not vault_tokens:
            return text, {}, {}

        norm_to_vault: Dict[str, str] = {}
        vault_to_norm: Dict[str, str] = {}
        norm_text = text

        for idx, token in enumerate(vault_tokens):
            if token in vault_to_norm:
                continue
            # Extract type segment: __PRESERVED_{TYPE}_{HEX32}__
            parts = token.strip("_").split("_")
            type_tag = parts[1] if len(parts) >= 2 else "ENTITY"
            if len(parts) >= 4 and parts[1] == "CIT":
                type_tag = f"CIT_{parts[2]}"

            norm_tag = f"__NORM_{type_tag}_{idx}__"
            norm_to_vault[norm_tag] = token
            vault_to_norm[token] = norm_tag

        for token, norm_tag in vault_to_norm.items():
            norm_text = norm_text.replace(token, norm_tag)

        return norm_text, norm_to_vault, vault_to_norm

    def _denormalize_placeholders(
        self, text: str, norm_to_vault: Dict[str, str]
    ) -> str:
        """Restores 128-bit request-scoped placeholders from normalized tags."""
        result = text
        for norm_tag, vault_token in norm_to_vault.items():
            result = result.replace(norm_tag, vault_token)
        return result

    def _generate_cache_key(
        self, source_lang: str, target_lang: str, normalized_text: str
    ) -> str:
        """Generates language-isolated, model/version-bound, PII-free SHA-256 cache key.

        Format: trans:{prompt_version}:{model_tag}:{src}:{tgt}:{sha256(canonical_pii_free_text)}
        """
        model_tag = self.model_name or "default"
        content_hash = hashlib.sha256(normalized_text.strip().encode("utf-8")).hexdigest()
        return f"trans:{self.prompt_version}:{model_tag}:{source_lang}:{target_lang}:{content_hash}"

    def _validate_cached_placeholders(
        self, cached_text: str, expected_norm_tags: Set[str]
    ) -> Tuple[bool, Optional[str]]:
        """Validates placeholder integrity in cached translation BEFORE denormalization.

        Enforces that cached template contains exactly the expected normalized tags
        with zero duplicates, zero missing tags, and zero unexpected tags.
        """
        found_matches = NORM_PLACEHOLDER_PATTERN.findall(cached_text)
        found_tags = [f"__NORM_{m[0]}_{m[1]}__" for m in found_matches]

        if len(found_tags) != len(expected_norm_tags):
            return False, f"CACHE_NORM_COUNT_MISMATCH: expected {len(expected_norm_tags)}, found {len(found_tags)}"

        found_set = set(found_tags)
        if len(found_set) != len(found_tags):
            return False, "CACHE_DUPLICATE_NORM_TAGS"

        if found_set != expected_norm_tags:
            return False, f"CACHE_NORM_TAG_MISMATCH: expected {expected_norm_tags}, found {found_set}"

        return True, None

    async def _get_cached_translation(self, cache_key: str) -> Optional[str]:
        """Queries Redis cache with graceful circuit breaker on Redis outage.

        Ensures that Redis failure never logs raw customer text or sensitive data.
        """
        try:
            redis_svc = self._get_redis_service()
            return await redis_svc.get(cache_key)
        except Exception as exc:
            logger.warning("Redis cache read failed (%s). Continuing without cache.", type(exc).__name__)
            return None

    async def _set_cached_translation(self, cache_key: str, translated_norm_text: str) -> None:
        """Stores translated template in Redis with graceful circuit breaker on Redis outage.

        Ensures that Redis failure never logs raw customer text or sensitive data.
        """
        try:
            redis_svc = self._get_redis_service()
            await redis_svc.set(cache_key, translated_norm_text, ttl_seconds=self.cache_ttl_seconds)
        except Exception as exc:
            logger.warning("Redis cache write failed (%s). Bypassing cache write.", type(exc).__name__)

    async def translate_inbound(
        self,
        text: Optional[str],
        source_language: str,
        vault: Optional[EntityPreservationVault] = None,
    ) -> TranslationResult:
        """Translates non-English customer query into English."""
        return await self.translate(
            text=text,
            source_language=source_language,
            target_language="en",
            vault=vault,
            is_outbound_response=False,
        )

    async def translate_outbound(
        self,
        text: Optional[str],
        target_language: str,
        vault: Optional[EntityPreservationVault] = None,
    ) -> TranslationResult:
        """Translates English agent response into customer's preferred language."""
        return await self.translate(
            text=text,
            source_language="en",
            target_language=target_language,
            vault=vault,
            is_outbound_response=True,
        )

    async def translate(
        self,
        text: Optional[str],
        source_language: str,
        target_language: str,
        vault: Optional[EntityPreservationVault] = None,
        mask_pii: bool = True,
        mask_entities: bool = True,
        is_outbound_response: bool = False,
    ) -> TranslationResult:
        """Executes secure bidirectional translation with PII scrubbing, 128-bit entity preservation, and caching."""
        start_time = time.perf_counter()

        # 1. Empty / Whitespace Input Handling
        if text is None or not str(text).strip():
            return TranslationResult(
                translated_text="",
                is_successful=True,
                source_language=source_language,
                target_language=target_language,
                latency_ms=0.0,
            )

        src = source_language.lower().strip()
        tgt = target_language.lower().strip()

        # 2. Identity / Same-Language Passthrough
        if src == tgt:
            return TranslationResult(
                translated_text=str(text),
                is_successful=True,
                source_language=src,
                target_language=tgt,
                latency_ms=0.0,
            )

        # 3. Validate Supported Language Pair
        if src not in SUPPORTED_LANGUAGES or tgt not in SUPPORTED_LANGUAGES:
            logger.error("Unsupported translation language pair: %s -> %s", src, tgt)
            fallback = LOCALIZED_SERVICE_NOTICES.get(tgt, LOCALIZED_SERVICE_NOTICES.get(src, LOCALIZED_SERVICE_NOTICES["en"]))
            return TranslationResult(
                translated_text=fallback,
                is_successful=False,
                source_language=src,
                target_language=tgt,
                error_reason="UNSUPPORTED_LANGUAGE_PAIR",
                latency_ms=(time.perf_counter() - start_time) * 1000,
            )

        # 4. Check for normalized marker injection in raw text (defense-in-depth)
        if NORM_PLACEHOLDER_PATTERN.search(str(text)):
            logger.warning("Normalized placeholder marker detected in input text. Rejecting injection.")
            fallback = LOCALIZED_SERVICE_NOTICES.get(tgt, LOCALIZED_SERVICE_NOTICES.get(src, LOCALIZED_SERVICE_NOTICES["en"]))
            return TranslationResult(
                translated_text=fallback,
                is_successful=False,
                source_language=src,
                target_language=tgt,
                error_reason="NORMALIZED_PLACEHOLDER_INJECTION_DETECTED",
                latency_ms=(time.perf_counter() - start_time) * 1000,
            )

        # 5. Request-Scoped Vault & PII Scrubbing
        current_vault = vault if vault is not None else EntityPreservationVault()
        processed_text = str(text)

        # Step 5A: Pre-translation PII Masking (Zero Raw PII to LLM)
        if mask_pii:
            masker = self._get_pii_masker()
            pii_result = masker.mask(processed_text)
            processed_text = pii_result.masked_text

        # Step 5B: 128-Bit Entity & Citation Masking
        if mask_entities:
            processed_text = current_vault.mask_entities(processed_text)
            if is_outbound_response:
                processed_text = current_vault.mask_citations(processed_text)

        # 6. Placeholder Normalization for Shared Redis Caching
        norm_text, norm_to_vault, _ = self._normalize_placeholders_for_cache(processed_text)
        cache_key = self._generate_cache_key(src, tgt, norm_text)

        # 7. Redis Cache Inspection (PII-Free Template Lookup)
        cached_val = await self._get_cached_translation(cache_key)
        if cached_val is not None:
            # Step 7A: Validate cached template placeholders BEFORE denormalization
            expected_norm_tags = set(norm_to_vault.keys())
            is_cache_valid, cache_reason = self._validate_cached_placeholders(cached_val, expected_norm_tags)
            if not is_cache_valid:
                logger.warning(
                    "Cached translation placeholder integrity check failed (%s). Rejecting corrupted cached value.",
                    cache_reason,
                )
            else:
                # Step 7B: Denormalize to request's 128-bit placeholders
                denorm_text = self._denormalize_placeholders(cached_val, norm_to_vault)

                # Step 7C: Check that zero residual __NORM_ tags remain
                if NORM_PLACEHOLDER_PATTERN.search(denorm_text):
                    logger.warning("Residual normalized tags after denormalization of cached value. Rejecting.")
                else:
                    # Step 7D: Validate 128-bit placeholder integrity in vault
                    is_valid, reason = current_vault.validate_placeholders(denorm_text)
                    if is_valid:
                        restored_text = current_vault.restore_entities(denorm_text)
                        elapsed = (time.perf_counter() - start_time) * 1000
                        return TranslationResult(
                            translated_text=restored_text,
                            is_successful=True,
                            source_language=src,
                            target_language=tgt,
                            cached=True,
                            latency_ms=elapsed,
                            model_used="redis-cache",
                        )
                    else:
                        logger.warning("Vault validation failed on cached value (%s). Falling back.", reason)

        # 8. Neural Translation via LLMGateway (temperature=0.0)
        gateway = self._get_llm_gateway()
        system_instruction = self._build_system_instruction(src, tgt)

        try:
            llm_resp = await gateway.generate(
                prompt=norm_text,
                system_instruction=system_instruction,
                temperature=0.0,
                max_output_tokens=self.max_output_tokens,
                timeout_seconds=self.timeout_seconds,
            )
            raw_translated = llm_resp.content.strip()
            model_used = llm_resp.model

        except (LLMProviderError, LLMTimeoutError, LLMAuthenticationError, Exception) as exc:
            logger.error("LLM translation failed for pair %s -> %s: %s", src, tgt, exc)
            fallback = LOCALIZED_SERVICE_NOTICES.get(tgt, LOCALIZED_SERVICE_NOTICES.get(src, LOCALIZED_SERVICE_NOTICES["en"]))
            elapsed = (time.perf_counter() - start_time) * 1000
            error_code = "PROVIDER_TIMEOUT" if isinstance(exc, LLMTimeoutError) else "TRANSLATION_PROVIDER_ERROR"
            return TranslationResult(
                translated_text=fallback,
                is_successful=False,
                source_language=src,
                target_language=tgt,
                error_reason=error_code,
                latency_ms=elapsed,
                model_used="fallback-notice",
            )

        if not raw_translated:
            logger.warning("LLM translation returned empty content for pair %s -> %s", src, tgt)
            fallback = LOCALIZED_SERVICE_NOTICES.get(tgt, LOCALIZED_SERVICE_NOTICES.get(src, LOCALIZED_SERVICE_NOTICES["en"]))
            elapsed = (time.perf_counter() - start_time) * 1000
            return TranslationResult(
                translated_text=fallback,
                is_successful=False,
                source_language=src,
                target_language=tgt,
                error_reason="EMPTY_PROVIDER_OUTPUT",
                latency_ms=elapsed,
                model_used=model_used,
            )

        # Step 9A: Validate normalized placeholders in LLM output BEFORE denormalization
        expected_norm_tags = set(norm_to_vault.keys())
        is_llm_valid, llm_reason = self._validate_cached_placeholders(raw_translated, expected_norm_tags)
        if not is_llm_valid:
            logger.warning(
                "Post-translation normalized placeholder check failed (%s). Refusing corrupted text.",
                llm_reason,
            )
            fallback = LOCALIZED_SERVICE_NOTICES.get(tgt, LOCALIZED_SERVICE_NOTICES.get(src, LOCALIZED_SERVICE_NOTICES["en"]))
            elapsed = (time.perf_counter() - start_time) * 1000
            return TranslationResult(
                translated_text=fallback,
                is_successful=False,
                source_language=src,
                target_language=tgt,
                error_reason=f"PLACEHOLDER_INTEGRITY_VIOLATION: {llm_reason}",
                latency_ms=elapsed,
                model_used=model_used,
            )

        # Step 9B: Denormalize Back to 128-Bit Request Vault Tokens
        denorm_translated = self._denormalize_placeholders(raw_translated, norm_to_vault)

        # Step 9C: Ensure zero residual normalized tags remain
        if NORM_PLACEHOLDER_PATTERN.search(denorm_translated):
            logger.warning("Residual normalized tags after denormalization of LLM output. Refusing.")
            fallback = LOCALIZED_SERVICE_NOTICES.get(tgt, LOCALIZED_SERVICE_NOTICES.get(src, LOCALIZED_SERVICE_NOTICES["en"]))
            elapsed = (time.perf_counter() - start_time) * 1000
            return TranslationResult(
                translated_text=fallback,
                is_successful=False,
                source_language=src,
                target_language=tgt,
                error_reason="PLACEHOLDER_INTEGRITY_VIOLATION: RESIDUAL_NORM_TAGS",
                latency_ms=elapsed,
                model_used=model_used,
            )

        # Step 10: Bi-Directional Placeholder Integrity Validation in Vault
        is_valid, reason = current_vault.validate_placeholders(denorm_translated)
        if not is_valid:
            logger.warning(
                "Post-translation placeholder integrity check failed (%s). Refusing corrupted text.",
                reason,
            )
            fallback = LOCALIZED_SERVICE_NOTICES.get(tgt, LOCALIZED_SERVICE_NOTICES.get(src, LOCALIZED_SERVICE_NOTICES["en"]))
            elapsed = (time.perf_counter() - start_time) * 1000
            return TranslationResult(
                translated_text=fallback,
                is_successful=False,
                source_language=src,
                target_language=tgt,
                error_reason=f"PLACEHOLDER_INTEGRITY_VIOLATION: {reason}",
                latency_ms=elapsed,
                model_used=model_used,
            )

        # 11. Update Redis Cache (Store Normalized PII-Free Template)
        await self._set_cached_translation(cache_key, raw_translated)

        # 12. Safe Value Restoration
        final_restored = current_vault.restore_entities(denorm_translated)
        elapsed = (time.perf_counter() - start_time) * 1000

        return TranslationResult(
            translated_text=final_restored,
            is_successful=True,
            source_language=src,
            target_language=tgt,
            cached=False,
            latency_ms=elapsed,
            model_used=model_used,
        )

