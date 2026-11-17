"""RAG Orchestration Layer & Facade Interface.

Coordinates the unified RAG execution lifecycle:
1. Validates request and trusted server-side identity (Zero Client Trust).
2. Performs Sanitizer Vault input validation and adversarial threat detection.
3. Invokes 3-Layer Security-Aware Retriever.
4. Enforces short-circuit refusal gating (retrieval refusal stops generation).
5. Invokes Grounded Answer Generator with claim-to-passage verification.
6. Performs Sanitizer Vault output scrubbing (PII and system tokens).
7. Emits structured RAGResolutionResult with provenance and correlation ID.
"""

from datetime import date, datetime, timezone
import logging
import re
import time
from typing import Any, Dict, List, Optional
import uuid

from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, settings as default_settings
from app.core.exceptions import (
    PlatformException,
    RAGAuthorizationError,
    RAGError,
    RAGPolicyConflictError,
    RAGSanitizationError,
    RAGSecurityQuarantineError,
)
from app.core.logging import configure_safe_logger
from app.modules.rag.citation import ClaimVerificationResult
from app.modules.rag.generator import (
    GroundedAnswerGenerator,
    RAGResolutionResult,
)
from app.modules.rag.retriever import (
    RetrievalResult,
    SecurityAwareRetriever,
)
from app.modules.rag.sanitizer import (
    RAGSanitizer,
    SanitizationResult,
    get_rag_sanitizer,
)
from app.modules.rag.security import (
    SecurityContext,
    SecurityContextResolver,
)

logger = configure_safe_logger(logging.getLogger(__name__))


class RAGInternalRequest(BaseModel):
    """Validated internal request payload for the RAG Knowledge Assistant.

    Enforces zero-trust: disallows client-specified role or access tiers.
    """

    model_config = {"extra": "forbid"}

    query: str = Field(..., description="Customer or agent natural language query")
    raw_customer_query: Optional[str] = Field(None, description="Original un-translated customer query text if available")
    preferred_language: Optional[str] = Field(None, description="Preferred dialogue language code")
    session_token: Optional[str] = Field(None, description="Authenticated customer session token")
    user_id_or_email: Optional[str] = Field(None, description="Authenticated staff user identifier")
    jwt_claims: Optional[Dict[str, Any]] = Field(None, description="Verified JWT claims")
    security_context: Optional[SecurityContext] = Field(
        None, description="Pre-resolved trusted server-side security context"
    )
    effective_as_of_date: Optional[date] = Field(
        None, description="Temporal evaluation date (defaults to current date)"
    )
    product_line: Optional[str] = Field(None, description="Product line scoping filter")
    document_code: Optional[str] = Field(None, description="Target document code filter")
    tone_directive: Optional[str] = Field(None, description="Optional controlled tone directive for grounded generation")
    sentiment_class: Optional[str] = Field(None, description="Detected customer sentiment category")
    conversation_history: List[Dict[str, str]] = Field(
        default_factory=list,
        description="Chronological recent turn history scoped strictly to the active conversation",
    )
    correlation_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Unique request correlation ID for end-to-end tracing",
    )

    @field_validator("security_context", mode="before")
    @classmethod
    def validate_trusted_context(cls, v: Any) -> Any:
        """Enforces zero-trust: clients cannot serialize forged security context dicts."""
        if isinstance(v, dict):
            raise ValueError(
                "Client cannot supply serialized security_context dictionary. Identity must be resolved server-side."
            )
        return v



CARETAKER_FALLBACK_SYSTEM_INSTRUCTION = """You are a senior customer support representative on our team.
A customer has reached out with an inquiry or issue. Speak naturally, warmly, and helpfully like a real human customer support specialist.

HUMAN SUPPORT DIRECTIVES:
1. Tone & Voice: Speak like an authentic, empathetic human support specialist. Never sound like a generic AI or canned bot.
2. Direct Context Relevance: Respond directly to what the customer actually asked. Never say "I am doing well, thank you for asking!" unless the customer explicitly asked how you are.
3. Order / Delivery / Damaged Goods: Sincere empathy first ("Oh no, I'm really sorry your order arrived damaged..."). Ask clearly for any details needed to look it up (Order ID, photos of damage, tracking number).
4. Concrete Help: Offer clear next steps, such as opening a priority support ticket or having a team specialist take ownership of the issue.
5. Responsible Care: Be reassuring and supportive without making unauthorized financial guarantees or policy commitments.
6. Seamless Language: Match the customer's exact language and dialect naturally (English, conversational Hindi/Hinglish, Spanish, French, German).
7. Flow: Write concise, fluid paragraphs with natural pacing. Avoid mechanical formatting.
"""


class RAGOrchestrator:
    """Enterprise RAG Orchestrator coordinating retrieval, generation, and sanitization."""

    def __init__(
        self,
        retriever: Optional[SecurityAwareRetriever] = None,
        generator: Optional[GroundedAnswerGenerator] = None,
        sanitizer: Optional[RAGSanitizer] = None,
        settings: Optional[Settings] = None,
    ) -> None:
        self.settings = settings or default_settings
        self.retriever = retriever or SecurityAwareRetriever(settings=self.settings)
        self.generator = generator or GroundedAnswerGenerator(settings=self.settings)
        self.sanitizer = sanitizer or get_rag_sanitizer()

    def _build_refusal(
        self,
        reason: str,
        message: str,
        correlation_id: str,
        start_time: float,
        retrieval_result: Optional[RetrievalResult] = None,
        effective_date: Optional[date] = None,
    ) -> RAGResolutionResult:
        """Constructs a standardized structured refusal result."""
        ret_lat = retrieval_result.latency_ms if retrieval_result else 0.0
        total_lat = (time.perf_counter() - start_time) * 1000
        eff_date_str = (
            retrieval_result.effective_as_of_date_applied.isoformat()
            if retrieval_result
            else (effective_date or date.today()).isoformat()
        )
        conf = retrieval_result.top_similarity if retrieval_result else 0.0

        # Suggested ticket escalation category
        cat = "GENERAL_SUPPORT"
        if reason == "UNKNOWN_REGION_POLICY_CLARIFICATION":
            cat = "POLICY_REGION_CLARIFICATION"
        elif reason == "CONFLICTING_POLICY_EVIDENCE":
            cat = "POLICY_DISPUTE_ESCALATION"
        elif reason == "UNSUPPORTED_CLAIM_VERIFICATION_FAILED":
            cat = "COMPLIANCE_REVIEW_ESCALATION"
        elif reason in ("AUTHORIZATION_DENIED", "UNAUTHORIZED_ACCESS"):
            cat = "SECURITY_ACCESS_REVIEW"
        elif "INJECTION" in reason or "QUARANTINE" in reason:
            cat = "SECURITY_INCIDENT_REVIEW"
        elif reason in ("INCONCLUSIVE_RETRIEVAL_CONFIDENCE", "INSUFFICIENT_EVIDENCE", "LOW_SIMILARITY_OUT_OF_DOMAIN"):
            cat = "GENERAL_SUPPORT_ESCALATION"

        return RAGResolutionResult(
            is_resolved=False,
            refusal_triggered=True,
            refusal_reason=reason,
            generated_response=message,
            citations=[],
            confidence_score=conf,
            effective_as_of_date_applied=eff_date_str,
            suggested_ticket_category=cat,
            claim_verification=ClaimVerificationResult(
                total_claims=0,
                verified_claims=0,
                all_claims_verified=True,
            ),
            retrieval_latency_ms=ret_lat,
            generation_latency_ms=0.0,
            total_latency_ms=total_lat,
            model_used=getattr(self.settings, "GEMINI_MODEL", "gemini"),
            correlation_id=correlation_id,
        )

    def _resolve_retrieval_refusal_message(self, reason: str) -> str:
        """Provides user-safe explanatory messaging for retrieval refusal triggers."""
        if reason == "UNKNOWN_REGION_POLICY_CLARIFICATION":
            return (
                "Return and warranty policies vary by country and region. To provide you with the "
                "exact policy covering your purchase, please specify your country or region."
            )
        if reason == "CONFLICTING_POLICY_EVIDENCE":
            return (
                "I have detected conflicting policy terms regarding your request across our authoritative "
                "documentation. Because these policies provide contradictory terms, I cannot confirm the "
                "applicable rule. I am escalating this directly to a support specialist to verify your exact eligibility."
            )
        if reason == "INCONCLUSIVE_RETRIEVAL_CONFIDENCE":
            return (
                "I do not have sufficient confidence in our authoritative policy documentation to answer your "
                "question definitively. To ensure you receive accurate guidance, I am connecting you with a "
                "customer support specialist."
            )
        if reason == "LOW_SIMILARITY_OUT_OF_DOMAIN":
            return (
                "I could not find authoritative documentation related to your question in our knowledge base. "
                "Please contact our customer support team for assistance."
            )
        if reason == "INSUFFICIENT_EVIDENCE":
            return "I could not find sufficient authoritative information in our knowledge base to answer your question."
        return "I could not locate authorized knowledge base records matching your request."

    async def _generate_caretaker_fallback(
        self,
        query: str,
        reason: str,
        conversation_history: Optional[List[Dict[str, str]]] = None,
        tone_directive: Optional[str] = None,
        preferred_language: Optional[str] = None,
    ) -> Optional[str]:
        """Generates an empathetic AI customer care response via LLM Gateway when specific KB docs are missing."""
        if not getattr(self.settings, "ENABLE_CARETAKER_FALLBACK", True):
            return None
        try:
            if not self.generator or not getattr(self.generator, "llm_gateway", None):
                return None

            history_lines: List[str] = []
            if conversation_history:
                for entry in conversation_history[-6:]:
                    role = str(entry.get("role") or "CUSTOMER").upper()
                    c = str(entry.get("content") or "").strip()
                    if c:
                        history_lines.append(f"{role}: {c}")
            hist_str = "\n".join(history_lines) if history_lines else "None"

            tone_str = f"Tone guidance: {tone_directive}\n" if tone_directive else ""
            lang_str = f"Customer preferred language: {preferred_language}\n" if preferred_language else ""

            prompt = (
                f"<customer_inquiry>\n{query}\n</customer_inquiry>\n\n"
                f"<recent_conversation>\n{hist_str}\n</recent_conversation>\n\n"
                f"{lang_str}"
                f"{tone_str}"
                "Please respond directly to the customer as their dedicated AI Customer Support Assistant."
            )

            resp = await self.generator.llm_gateway.generate(
                prompt=prompt,
                system_instruction=CARETAKER_FALLBACK_SYSTEM_INSTRUCTION,
                temperature=0.3,
            )
            if resp and resp.content and resp.content.strip():
                clean_reply = resp.content.strip()
                if self.sanitizer:
                    clean_reply = self.sanitizer.sanitize_output(clean_reply)
                return clean_reply
        except Exception as exc:
            logger.warning(
                "Caretaker LLM fallback generation encountered an issue: %s; falling back to default message.",
                exc,
            )
        return None

    def _sanitize_history(self, history: List[Dict[str, str]]) -> List[Dict[str, str]]:
        """Sanitizes up to 10 prior conversation turns and strips any injected turns."""
        if not history:
            return []
        cleaned: List[Dict[str, str]] = []
        for entry in history[-10:]:
            content = str(entry.get("content") or "").strip()
            if not content:
                continue
            san = self.sanitizer.sanitize_input(content)
            if san.risk_detected and san.risk_reason == "PROMPT_INJECTION_DETECTED":
                continue
            role = str(entry.get("role") or "CUSTOMER").upper()
            cleaned.append({"role": role, "content": san.clean_query})
        return cleaned

    def _build_retrieval_query(
        self,
        clean_query: str,
        sanitized_history: List[Dict[str, str]],
    ) -> str:
        """Enriches short or anaphoric follow-up queries with the prior customer turn for retrieval."""
        if not sanitized_history:
            return clean_query
        words = clean_query.split()
        is_short_followup = len(words) <= 6
        has_reference = bool(
            re.search(
                r"\b(?:it|that|this|those|these|them|they|what about|how about|and for|else)\b",
                clean_query,
                re.IGNORECASE,
            )
        )
        if not (is_short_followup or has_reference):
            return clean_query
        for entry in reversed(sanitized_history):
            if entry.get("role") == "CUSTOMER" and entry.get("content") and entry["content"] != clean_query:
                combined = f"{entry['content']} {clean_query}".strip()
                return combined[:1500]
        return clean_query

    async def process_query(
        self,
        request: RAGInternalRequest,
        session: AsyncSession,
    ) -> RAGResolutionResult:
        """Processes an incoming customer or staff query through the complete RAG lifecycle."""
        start_time = time.perf_counter()
        correlation_id = request.correlation_id

        try:
            # 1. Validate query presence
            if not request.query or not request.query.strip():
                return self._build_refusal(
                    reason="EMPTY_QUERY",
                    message="Please provide a valid question.",
                    correlation_id=correlation_id,
                    start_time=start_time,
                    effective_date=request.effective_as_of_date,
                )

            # 2. Sanitizer Vault input validation and threat detection
            try:
                san_result: SanitizationResult = self.sanitizer.sanitize_input(request.query)
            except Exception as exc:
                logger.error("Sanitizer error on correlation %s: %s", correlation_id, exc)
                return self._build_refusal(
                    reason="SANITIZER_FAILURE",
                    message="Your request could not be processed due to a security sanitization error.",
                    correlation_id=correlation_id,
                    start_time=start_time,
                    effective_date=request.effective_as_of_date,
                )

            if san_result.risk_detected:
                if san_result.risk_reason == "QUERY_OVERSIZED":
                    return self._build_refusal(
                        reason="QUERY_OVERSIZED",
                        message="Your query exceeds the maximum allowed length. Please shorten your question.",
                        correlation_id=correlation_id,
                        start_time=start_time,
                        effective_date=request.effective_as_of_date,
                    )
                if san_result.risk_reason == "EMPTY_QUERY":
                    return self._build_refusal(
                        reason="EMPTY_QUERY",
                        message="Please provide a valid question.",
                        correlation_id=correlation_id,
                        start_time=start_time,
                        effective_date=request.effective_as_of_date,
                    )
                if san_result.risk_reason == "PROMPT_INJECTION_DETECTED":
                    logger.warning("Prompt injection blocked for correlation %s.", correlation_id)
                    return self._build_refusal(
                        reason="SECURITY_INJECTION_DETECTED",
                        message="Your request could not be processed due to invalid or disallowed instructions.",
                        correlation_id=correlation_id,
                        start_time=start_time,
                        effective_date=request.effective_as_of_date,
                    )

            # 3. Server-side identity resolution (Zero Client Trust)
            sec_ctx: SecurityContext
            # Server-side authoritative resolution takes strict precedence when credentials/tokens are provided
            if request.session_token is not None or request.user_id_or_email or request.jwt_claims:
                try:
                    sec_ctx = await SecurityContextResolver.resolve(
                        session=session,
                        session_token=request.session_token,
                        jwt_claims=request.jwt_claims,
                        user_id_or_email=request.user_id_or_email,
                    )
                except RAGAuthorizationError as exc:
                    logger.warning("Authorization denied for correlation %s: %s", correlation_id, exc)
                    return self._build_refusal(
                        reason="AUTHORIZATION_DENIED",
                        message="Access to requested knowledge resources is denied.",
                        correlation_id=correlation_id,
                        start_time=start_time,
                        effective_date=request.effective_as_of_date,
                    )
                except Exception as exc:
                    logger.error("Session resolution error for correlation %s: %s", correlation_id, exc)
                    return self._build_refusal(
                        reason="AUTHORIZATION_DENIED",
                        message="Access to requested knowledge resources is denied.",
                        correlation_id=correlation_id,
                        start_time=start_time,
                        effective_date=request.effective_as_of_date,
                    )
            elif request.security_context:
                # Disallow unauthenticated claims of privileged staff roles
                if (
                    request.security_context.role in ("ADMIN", "SUPERVISOR", "AGENT")
                    and request.security_context.auth_type not in ("JWT", "SESSION_TOKEN")
                ):
                    logger.warning("Unauthenticated privileged security_context rejected for correlation %s", correlation_id)
                    return self._build_refusal(
                        reason="AUTHORIZATION_DENIED",
                        message="Access to requested knowledge resources is denied.",
                        correlation_id=correlation_id,
                        start_time=start_time,
                        effective_date=request.effective_as_of_date,
                    )
                sec_ctx = request.security_context
            else:
                sec_ctx = SecurityContextResolver.resolve_anonymous()



            # 4. 3-Layer Security-Aware Retrieval
            sanitized_history = self._sanitize_history(request.conversation_history)
            retrieval_query = self._build_retrieval_query(san_result.clean_query, sanitized_history)
            try:
                retrieval_result: RetrievalResult = await self.retriever.retrieve(
                    query=retrieval_query,
                    session=session,
                    security_context=sec_ctx,
                    effective_as_of_date=request.effective_as_of_date,
                    product_line=request.product_line,
                    document_code=request.document_code,
                )
            except RAGSecurityQuarantineError as exc:
                logger.warning("Retrieval quarantined for correlation %s: %s", correlation_id, exc)
                return self._build_refusal(
                    reason="SECURITY_INJECTION_QUARANTINED",
                    message="Knowledge content has been quarantined due to security policy.",
                    correlation_id=correlation_id,
                    start_time=start_time,
                    effective_date=request.effective_as_of_date,
                )
            except RAGPolicyConflictError as exc:
                logger.warning("Policy conflict detected for correlation %s: %s", correlation_id, exc)
                return self._build_refusal(
                    reason="CONFLICTING_POLICY_EVIDENCE",
                    message=self._resolve_retrieval_refusal_message("CONFLICTING_POLICY_EVIDENCE"),
                    correlation_id=correlation_id,
                    start_time=start_time,
                    effective_date=request.effective_as_of_date,
                )
            except RAGAuthorizationError as exc:
                logger.warning("Retrieval authorization denied for correlation %s: %s", correlation_id, exc)
                return self._build_refusal(
                    reason="AUTHORIZATION_DENIED",
                    message="Access to requested knowledge resources is denied.",
                    correlation_id=correlation_id,
                    start_time=start_time,
                    effective_date=request.effective_as_of_date,
                )

            # 5. Retrieval Refusal Short-Circuit Gate
            # Strict rule: Retrieval refusal MUST stop generation!
            if retrieval_result.is_refusal:
                reason = retrieval_result.refusal_reason or "RETRIEVAL_REFUSAL"
                msg = self._resolve_retrieval_refusal_message(reason)
                if reason in (
                    "NO_AUTHORIZED_CANDIDATES",
                    "LOW_SIMILARITY_OUT_OF_DOMAIN",
                    "INSUFFICIENT_EVIDENCE",
                    "INCONCLUSIVE_RETRIEVAL_CONFIDENCE",
                    "RETRIEVAL_REFUSAL",
                ):
                    caretaker_msg = await self._generate_caretaker_fallback(
                        query=request.raw_customer_query or san_result.clean_query,
                        reason=reason,
                        conversation_history=sanitized_history,
                        tone_directive=request.tone_directive,
                        preferred_language=request.preferred_language,
                    )
                    if caretaker_msg:
                        msg = caretaker_msg
                return self._build_refusal(
                    reason=reason,
                    message=msg,
                    correlation_id=correlation_id,
                    start_time=start_time,
                    retrieval_result=retrieval_result,
                )

            # 6. Empty Evidence Gate
            if not retrieval_result.chunks:
                reason = "INSUFFICIENT_EVIDENCE"
                msg = self._resolve_retrieval_refusal_message(reason)
                caretaker_msg = await self._generate_caretaker_fallback(
                    query=request.raw_customer_query or san_result.clean_query,
                    reason=reason,
                    conversation_history=sanitized_history,
                    tone_directive=request.tone_directive,
                    preferred_language=request.preferred_language,
                )
                if caretaker_msg:
                    msg = caretaker_msg
                return self._build_refusal(
                    reason=reason,
                    message=msg,
                    correlation_id=correlation_id,
                    start_time=start_time,
                    retrieval_result=retrieval_result,
                )

            # 7. Grounded Answer Generation & Claim Verification
            gen_result = await self.generator.generate_answer(
                retrieval_result=retrieval_result,
                query_override=san_result.clean_query,
                tone_directive=request.tone_directive,
                conversation_history=sanitized_history,
            )

            # 8. Output Sanitization (PII and system tokens)
            if gen_result.generated_response:
                clean_reply = self.sanitizer.sanitize_output(gen_result.generated_response)
                gen_result.generated_response = clean_reply

            # 9. Final Correlation & Latency Mapping
            gen_result.correlation_id = correlation_id
            gen_result.total_latency_ms = (time.perf_counter() - start_time) * 1000

            return gen_result

        except Exception as exc:
            logger.error("Internal orchestration error for correlation %s: %s", correlation_id, exc, exc_info=False)
            return self._build_refusal(
                reason="INTERNAL_ORCHESTRATION_ERROR",
                message="An unexpected error occurred while processing your request. Please try again later.",
                correlation_id=correlation_id,
                start_time=start_time,
                effective_date=request.effective_as_of_date,
            )

    async def generate_grounded_answer(
        self,
        request: RAGInternalRequest,
        session: AsyncSession,
    ) -> RAGResolutionResult:
        """Alias conforming to interservice Module 4 facade interface."""
        return await self.process_query(request=request, session=session)


# Facade alias for Module 4 integration
RAGKnowledgeAssistant = RAGOrchestrator

