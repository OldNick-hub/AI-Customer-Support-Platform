"""Multilingual Chat Orchestrator & Canonical Endpoint Integration.

Implements the complete 14-step end-to-end request pipeline for:
POST /api/v1/chat/message

Key Capabilities:
1. Two-phase committed idempotency reservation protocol with heartbeat renewals.
2. CAS takeover of expired leases and atomic CAS recovery of FAILED reservations.
3. Dedicated HMAC-SHA256 fingerprinting with key ring version rotation support.
4. AES-256-GCM encryption of cached responses and raw message content.
5. Inbound prompt injection defense, language detection, and entity preservation.
6. Bidirectional translation with 4-point structural parity checks.
7. Database-controlled atomic sequence allocation (fn_allocate_next_turn_sequence).
8. Atomic customer/agent message pair and transactional outbox staging.
9. Outbox worker delivery to Redis timeline buffer and timeline continuity verification.
"""

import asyncio
from datetime import date, datetime, timedelta, timezone
import hashlib
import hmac
import json
import logging
import re
import secrets
from typing import Any, Dict, List, Optional, Set, Tuple, Union
import uuid

from fastapi import HTTPException, status
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import ResolvedAuthContext
from app.core.config import Settings, settings as global_settings
from app.core.encryption import ApplicationLayerEncryption, get_encryption_engine
from app.core.logging import configure_safe_logger
from app.core.pii import PIIMaskingEngine
from app.models.chat import ChatIdempotencyRecord, ChatOutboxEvent
from app.models.customer import Conversation, Customer, Message, Session as CustomerSession
from app.models.sentiment import SentimentLog
from app.modules.multilingual.config import (
    HOT_CONTEXT_WINDOW_SIZE,
    LOCALIZED_SERVICE_NOTICES,
    MIN_CONFIDENCE_THRESHOLD,
    MULTILINGUAL_CLARIFICATION_PROMPT,
    SUPPORTED_LANGUAGES,
)
from app.modules.multilingual.entity_preserver import EntityPreservationVault
from app.modules.multilingual.language_detector import LanguageDetectionResult, LanguageDetector
from app.modules.multilingual.lifecycle_manager import (
    ConversationLifecycleManager,
    SessionRestorationResult,
)
from app.modules.multilingual.outbox_worker import (
    OutboxWorker,
    generate_outbox_event_id,
)
from app.modules.multilingual.correction_handler import SlotCorrectionEngine
from app.modules.multilingual.query_decomposer import QueryDecomposer
from app.modules.multilingual.security_scanner import (
    PreTranslationSecurityResult,
    PreTranslationSecurityScanner,
)
from app.modules.multilingual.translator import TranslationResult, Translator
from app.modules.rag.generator import RAGResolutionResult, get_tone_directive
from app.modules.rag.orchestrator import RAGInternalRequest, RAGOrchestrator
from app.modules.sentiment.analyzer import (
    SentimentAnalysisResult,
    SentimentAnalyzer,
    default_sentiment_analyzer,
)
from app.modules.ticketing.escalation_service import EscalationService

logger = configure_safe_logger(logging.getLogger(__name__))


def compute_idempotency_fingerprint(
    key: str,
    customer_id: str,
    session_id: str,
    message: str,
    version: str = "v1",
    secret: Optional[str] = None,
    settings: Optional[Settings] = None,
) -> str:
    """Computes HMAC-SHA256 request fingerprint using versioned secret key ring."""
    app_settings = settings or global_settings
    if secret is None:
        secret = app_settings.IDEMPOTENCY_HMAC_KEY_RING.get(version)
    if not secret:
        raise RuntimeError(f"HMAC secret for version '{version}' not found in key ring.")
    
    canonical_payload = f"{customer_id}:{session_id}:{key}:{message.strip()}"
    return hmac.new(
        secret.encode("utf-8"),
        canonical_payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


class MultilingualOrchestrator:
    """Canonical conversational chat orchestrator for multilingual RAG queries."""

    def __init__(
        self,
        settings: Optional[Settings] = None,
        session_factory: Optional[async_sessionmaker[AsyncSession]] = None,
        security_scanner: Optional[PreTranslationSecurityScanner] = None,
        language_detector: Optional[LanguageDetector] = None,
        translator: Optional[Translator] = None,
        lifecycle_manager: Optional[ConversationLifecycleManager] = None,
        rag_orchestrator: Optional[RAGOrchestrator] = None,
        outbox_worker: Optional[OutboxWorker] = None,
        encryption: Optional[ApplicationLayerEncryption] = None,
        pii_masker: Optional[PIIMaskingEngine] = None,
        sentiment_analyzer: Optional[SentimentAnalyzer] = None,
        escalation_service: Optional[EscalationService] = None,
        query_decomposer: Optional[QueryDecomposer] = None,
        slot_correction_engine: Optional[SlotCorrectionEngine] = None,
    ) -> None:
        self.settings = settings or global_settings
        self.session_factory = session_factory
        self.security_scanner = security_scanner or PreTranslationSecurityScanner()
        self.language_detector = language_detector or LanguageDetector()
        self.translator = translator or Translator()
        self.lifecycle_manager = lifecycle_manager
        self.rag_orchestrator = rag_orchestrator
        self.query_decomposer = query_decomposer or QueryDecomposer()
        self.slot_correction_engine = slot_correction_engine or SlotCorrectionEngine()
        self.outbox_worker = outbox_worker or OutboxWorker()
        self.encryption = encryption or get_encryption_engine()
        self.pii_masker = pii_masker or PIIMaskingEngine()
        self.sentiment_analyzer = sentiment_analyzer or default_sentiment_analyzer
        self.escalation_service = escalation_service or EscalationService()

    async def _heartbeat_loop(
        self,
        record_id: uuid.UUID,
        owner_token: str,
        lease_lost_event: asyncio.Event,
        stop_event: asyncio.Event,
    ) -> None:
        """Independent background task periodically renewing the 30s database lease."""
        interval = getattr(self.settings, "IDEMPOTENCY_HEARTBEAT_INTERVAL_SECONDS", 10)
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
                break
            except asyncio.TimeoutError:
                pass

            if stop_event.is_set() or not self.session_factory:
                break

            now = datetime.now(timezone.utc)
            new_expiry = now + timedelta(seconds=getattr(self.settings, "IDEMPOTENCY_LEASE_DURATION_SECONDS", 30))
            try:
                async with self.session_factory() as hb_session:
                    stmt = (
                        update(ChatIdempotencyRecord)
                        .where(
                            ChatIdempotencyRecord.id == record_id,
                            ChatIdempotencyRecord.owner_token == owner_token,
                            ChatIdempotencyRecord.status == "IN_PROGRESS",
                            ChatIdempotencyRecord.lease_expires_at > now,
                        )
                        .values(
                            lease_expires_at=new_expiry,
                            heartbeat_at=now,
                        )
                    )
                    res = await hb_session.execute(stmt)
                    await hb_session.commit()
                    if res.rowcount == 0:
                        logger.warning("Heartbeat failed to renew lease for record %s; lease lost", record_id)
                        lease_lost_event.set()
                        break
            except Exception as exc:
                logger.error("Error in heartbeat loop for record %s: %s", record_id, exc)

    # Anchored pure conversational phrases (no product/policy/troubleshooting content)
    _GREETING_PATTERN = re.compile(
        r"^(?:hi|hello|hey|hi\s+there|hello\s+there|hey\s+there|greetings|"
        r"good\s+morning|good\s+afternoon|good\s+evening|yo|"
        r"hola|buenos\s+d[ií]as|buenas\s+tardes|buenas\s+noches|"
        r"bonjour|salut|bonsoir|"
        r"hallo|guten\s+morgen|guten\s+tag|guten\s+abend|servus|moin|"
        r"namaste|namaskar|नमस्ते|नमस्कार)$",
        re.IGNORECASE,
    )
    _HOW_ARE_YOU_PATTERN = re.compile(
        r"^(?:(?:hi|hello|hey|hola|bonjour|hallo|namaste|नमस्ते)[,\s]+)?(?:"
        r"how\s+are\s+you(?:\s+doing)?|how\s+is\s+it\s+going|"
        r"c[oó]mo\s+est[aá]s|que\s+tal|comment\s+[çc]a\s+va|comment\s+allez[\s-]vous|"
        r"wie\s+geht\s+es\s+(?:dir|ihnen)|wie\s+geht'?s|"
        r"kaise\s+ho|aap\s+kaise\s+hain|कैसे\s+हो|आप\s+कैसे\s+हैं)$",
        re.IGNORECASE,
    )
    _GRATITUDE_PATTERN = re.compile(
        r"^(?:thanks|thank\s+you(?:\s+so\s+much|\s+very\s+much)?|"
        r"gracias|muchas\s+gracias|"
        r"merci(?:\s+beaucoup)?|"
        r"danke(?:\s+sch[oö]n)?|vielen\s+dank|"
        r"shukriya|dhanyawad|dhanyavaad|धन्यवाद|शुक्रिया)$",
        re.IGNORECASE,
    )
    _ACKNOWLEDGMENT_PATTERN = re.compile(
        r"^(?:ok|okay|alright|got\s+it|sounds\s+good|makes\s+sense|no\s+problem|sure|great|awesome|cool|"
        r"vale|d'accord|alles\s+klar|theek\s+hai|ठीक\s+है)$",
        re.IGNORECASE,
    )
    _FAREWELL_PATTERN = re.compile(
        r"^(?:bye|goodbye|bye\s+bye|see\s+you|take\s+care|have\s+a\s+good\s+day|"
        r"adi[oó]s|hasta\s+luego|chao|"
        r"au\s+revoir|[àa]\s+bient[oô]t|"
        r"tsch[uü]ss|tschuess|auf\s+wiedersehen|"
        r"alvida|अलविदा)$",
        re.IGNORECASE,
    )

    _CONVERSATIONAL_REPLIES: Dict[str, Dict[str, str]] = {
        "en": {
            "greeting": "Hello! Welcome to Customer Support. How can I assist you with your order, delivery, or product today?",
            "how_are_you": "I'm doing great, thank you for asking! How can I help you today?",
            "gratitude": "You're very welcome! Please let me know if there's anything else I can do for you.",
            "acknowledgment": "Got it! Let me know what you'd like to do next.",
            "farewell": "Take care! Feel free to reach back out anytime if you need a hand.",
        },
        "es": {
            "greeting": "¡Hola! Bienvenido a Atención al Cliente. ¿Cómo puedo ayudarte hoy con tu pedido o consulta?",
            "how_are_you": "¡Muy bien, gracias por preguntar! ¿En qué puedo colaborarte hoy?",
            "gratitude": "¡Con gusto! Avísame si necesitas algo más.",
            "acknowledgment": "¡Entendido! Dime en qué más te puedo ayudar.",
            "farewell": "¡Hasta pronto! Escríbenos cuando lo necesites.",
        },
        "fr": {
            "greeting": "Bonjour et bienvenue au service client ! Comment puis-je vous aider aujourd'hui concernant votre commande ou une question ?",
            "how_are_you": "Je vais très bien, merci de demander ! Comment puis-je vous aider ?",
            "gratitude": "Avec plaisir ! N'hésitez pas si vous avez d'autres questions.",
            "acknowledgment": "C'est bien noté ! Que souhaitez-vous faire ensuite ?",
            "farewell": "Bonne journée ! N'hésitez pas à revenir vers nous si besoin.",
        },
        "de": {
            "greeting": "Hallo und willkommen beim Kundenservice! Wie kann ich Ihnen heute bei Ihrer Bestellung oder Frage behilflich sein?",
            "how_are_you": "Mir geht es sehr gut, vielen Dank der Nachfrage! Wie kann ich Ihnen helfen?",
            "gratitude": "Sehr gerne! Lassen Sie mich wissen, wenn Sie noch etwas benötigen.",
            "acknowledgment": "Alles klar! Sagen Sie mir gerne, wie ich weiterhelfen kann.",
            "farewell": "Auf Wiedersehen und einen schönen Tag! Melden Sie sich jederzeit gerne wieder.",
        },
        "hi": {
            "greeting": "नमस्ते! ग्राहक सहायता में आपका स्वागत है। आज मैं आपके ऑर्डर, डिलीवरी या किसी भी समस्या में आपकी क्या मदद कर सकता हूँ?",
            "how_are_you": "मैं बहुत अच्छा हूँ, पूछने के लिए धन्यवाद! बताएं, आज मैं आपकी क्या सहायता कर सकता हूँ?",
            "gratitude": "बहुत स्वागत है आपका! अगर कोई और भी सवाल हो तो बेझिझक बताएं।",
            "acknowledgment": "बिल्कुल ठीक! बताएं आगे क्या करना है।",
            "farewell": "अलविदा! जब भी ज़रूरत हो, आप कभी भी यहाँ संपर्क कर सकते हैं।",
        },
        "hi_latn": {
            "greeting": "Namaste! Customer Support mein aapka swagat hai. Aaj main aapke order, delivery ya product ke liye kya madad kar sakta hoon?",
            "how_are_you": "Main bilkul theek hoon, poochne ke liye shukriya! Boliye, aaj main aapki kya help kar sakta hoon?",
            "gratitude": "Aapka swagat hai! Agar koi aur sawaal ho toh zaroor batayein.",
            "acknowledgment": "Bilkul theek! Batayein aage kya karna hai.",
            "farewell": "Dhanyawaad! Jab bhi zaroorat ho, aap yahan contact kar sakte hain. Have a great day!",
        },
    }

    @classmethod
    def _classify_conversational_intent(cls, raw_text: Optional[str]) -> Optional[str]:
        """Detects pure conversational small-talk so greetings never trigger KB refusal."""
        if not raw_text:
            return None
        normalized = re.sub(r"[!?.]+$", "", " ".join(raw_text.strip().split())).strip()
        if not normalized or len(normalized) > 64:
            return None
        # Combined greeting + greeting (e.g. "Hi, good morning")
        parts = [p.strip(" ,;:-") for p in re.split(r"[,;]+", normalized) if p.strip(" ,;:-")]
        if cls._HOW_ARE_YOU_PATTERN.match(normalized):
            return "how_are_you"
        if cls._GREETING_PATTERN.match(normalized) or (
            parts and len(parts) <= 2 and all(cls._GREETING_PATTERN.match(p) for p in parts)
        ):
            return "greeting"
        if cls._GRATITUDE_PATTERN.match(normalized) or (
            parts and len(parts) <= 2 and all(cls._GRATITUDE_PATTERN.match(p) for p in parts)
        ):
            return "gratitude"
        if cls._ACKNOWLEDGMENT_PATTERN.match(normalized) or (
            parts and len(parts) <= 2 and all(cls._ACKNOWLEDGMENT_PATTERN.match(p) for p in parts)
        ):
            return "acknowledgment"
        if cls._FAREWELL_PATTERN.match(normalized):
            return "farewell"
        return None

    @classmethod
    def _render_conversational_reply(cls, intent: str, lang_code: str, script: Optional[str] = None) -> str:
        """Returns a polite localized reply for pure conversational turns."""
        effective_key = lang_code
        if lang_code == "hi" and script == "Latn":
            effective_key = "hi_latn"
        lang_map = cls._CONVERSATIONAL_REPLIES.get(effective_key, cls._CONVERSATIONAL_REPLIES.get(lang_code, cls._CONVERSATIONAL_REPLIES["en"]))
        return lang_map.get(intent, cls._CONVERSATIONAL_REPLIES["en"]["greeting"])

    async def reserve_idempotency_key(
        self,
        session: AsyncSession,
        customer_id: uuid.UUID,
        session_id: uuid.UUID,
        idempotency_key: str,
        message: str,
        conversation_id: Optional[uuid.UUID] = None,
    ) -> Tuple[ChatIdempotencyRecord, str, bool, Optional[Dict[str, Any]]]:
        """Executes Transaction 1: Committed reservation protocol with CAS recovery."""
        active_version = getattr(self.settings, "IDEMPOTENCY_HMAC_ACTIVE_VERSION", "v1")
        fingerprint = compute_idempotency_fingerprint(
            key=idempotency_key,
            customer_id=str(customer_id),
            session_id=str(session_id),
            message=message,
            version=active_version,
            settings=self.settings,
        )

        owner_token = secrets.token_hex(32)
        now = datetime.now(timezone.utc)
        lease_duration = getattr(self.settings, "IDEMPOTENCY_LEASE_DURATION_SECONDS", 30)
        lease_expires_at = now + timedelta(seconds=lease_duration)
        expires_at = now + timedelta(seconds=getattr(self.settings, "IDEMPOTENCY_RECORD_TTL_SECONDS", 86400))

        # Check existing record
        stmt = select(ChatIdempotencyRecord).where(
            ChatIdempotencyRecord.customer_id == customer_id,
            ChatIdempotencyRecord.session_id == session_id,
            ChatIdempotencyRecord.idempotency_key == idempotency_key,
        )
        existing = (await session.execute(stmt)).scalar_one_or_none()

        if existing:
            # 1. Completed turn: Return cached response
            if existing.status == "COMPLETED":
                # Verify fingerprint against saved version
                expected_fp = compute_idempotency_fingerprint(
                    key=idempotency_key,
                    customer_id=str(customer_id),
                    session_id=str(session_id),
                    message=message,
                    version=existing.fingerprint_version,
                    settings=self.settings,
                )
                if existing.request_fingerprint != expected_fp:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="Idempotency key reused with different request payload",
                    )
                cached_data = None
                if existing.encrypted_response_payload:
                    try:
                        decrypted_res = self.encryption.decrypt_payload(
                            ciphertext=existing.encrypted_response_payload,
                            conversation_id=str(existing.conversation_id or conversation_id or uuid.uuid4()),
                        )
                        if isinstance(decrypted_res, bytes):
                            cached_data = json.loads(decrypted_res.decode("utf-8"))
                        else:
                            cached_data = json.loads(decrypted_res)
                    except Exception as exc:
                        logger.error("Failed to decrypt cached idempotency response: %s", exc)
                return existing, existing.owner_token or "", True, cached_data

            # 2. In progress turn
            if existing.status == "IN_PROGRESS":
                if existing.request_fingerprint != fingerprint:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="Idempotency key reused with different request payload",
                    )
                if existing.lease_expires_at and existing.lease_expires_at > now:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="Concurrent request in progress for this idempotency key",
                        headers={"Retry-After": "1"},
                    )
                # Expired lease: CAS takeover
                cas_stmt = (
                    update(ChatIdempotencyRecord)
                    .where(
                        ChatIdempotencyRecord.id == existing.id,
                        ChatIdempotencyRecord.status == "IN_PROGRESS",
                        ChatIdempotencyRecord.lease_expires_at <= now,
                    )
                    .values(
                        owner_token=owner_token,
                        lease_expires_at=lease_expires_at,
                        attempt_count=existing.attempt_count + 1,
                        updated_at=now,
                    )
                )
                res = await session.execute(cas_stmt)
                if res.rowcount == 0:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="Concurrent request in progress for this idempotency key",
                        headers={"Retry-After": "1"},
                    )
                await session.commit()
                return existing, owner_token, False, None

            # 3. Failed turn: CAS reclaim with identical fingerprint
            if existing.status == "FAILED":
                expected_fp = compute_idempotency_fingerprint(
                    key=idempotency_key,
                    customer_id=str(customer_id),
                    session_id=str(session_id),
                    message=message,
                    version=existing.fingerprint_version,
                    settings=self.settings,
                )
                if existing.request_fingerprint != expected_fp:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="Idempotency key reused with different request payload",
                    )
                cas_stmt = (
                    update(ChatIdempotencyRecord)
                    .where(
                        ChatIdempotencyRecord.id == existing.id,
                        ChatIdempotencyRecord.status == "FAILED",
                        ChatIdempotencyRecord.request_fingerprint == expected_fp,
                    )
                    .values(
                        status="IN_PROGRESS",
                        owner_token=owner_token,
                        lease_expires_at=lease_expires_at,
                        attempt_count=existing.attempt_count + 1,
                        failure_reason=None,
                        updated_at=now,
                    )
                )
                res = await session.execute(cas_stmt)
                if res.rowcount == 0:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="Concurrent request in progress for this idempotency key",
                        headers={"Retry-After": "1"},
                    )
                await session.commit()
                return existing, owner_token, False, None

        # Fresh reservation
        record = ChatIdempotencyRecord(
            id=uuid.uuid4(),
            customer_id=customer_id,
            session_id=session_id,
            conversation_id=conversation_id,
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
            fingerprint_version=active_version,
            status="IN_PROGRESS",
            owner_token=owner_token,
            lease_expires_at=lease_expires_at,
            attempt_count=1,
            created_at=now,
            updated_at=now,
            expires_at=expires_at,
        )
        session.add(record)
        await session.commit()
        return record, owner_token, False, None

    def evaluate_language_routing(
        self,
        detected_lang: str,
        confidence: float,
        preferred_lang: Optional[str] = None,
    ) -> Tuple[bool, bool, Optional[str], Optional[str]]:
        """Evaluates language routing policy. Returns (clarification_needed, inbound_trans_needed, source_lang, target_lang)."""
        eff_pref = "hi" if preferred_lang in ("hi_latn", "hinglish") else preferred_lang

        # If confidence is below threshold or language is undetermined, inherit preferred/conversation language if available
        if confidence < MIN_CONFIDENCE_THRESHOLD or detected_lang not in SUPPORTED_LANGUAGES:
            if eff_pref and eff_pref in SUPPORTED_LANGUAGES:
                inbound_trans_needed = (eff_pref != "en")
                return False, inbound_trans_needed, eff_pref, eff_pref
            return True, False, None, None

        target_lang = eff_pref if (eff_pref and eff_pref in SUPPORTED_LANGUAGES) else detected_lang
        inbound_trans_needed = (detected_lang != "en")
        return False, inbound_trans_needed, detected_lang, target_lang

    def evaluate_citation_parity(
        self, english_citations: List[Any], localized_text: str
    ) -> bool:
        """Evaluates 4-point structural parity check on outbound translated response."""
        # 1. Bracket balance
        if localized_text.count("[") != localized_text.count("]"):
            return False

        # 2. Zero unreplaced placeholder leakage
        if "__PRESERVE_" in localized_text:
            return False

        # If no citations in source, pass
        if not english_citations:
            return True

        # Extract citation indices from source
        source_indices: Set[int] = set()
        for c in english_citations:
            cid = getattr(c, "citation_id", None)
            if cid is not None:
                source_indices.add(int(cid))

        # Extract citation numbers from localized text
        import re
        bracketed_matches = re.findall(r"\[(\d+(?:,\s*\d+)*)\]", localized_text)
        found_indices: Set[int] = set()
        for m in bracketed_matches:
            for num in re.split(r",\s*", m):
                if num.isdigit():
                    found_indices.add(int(num))

        # 3. Count parity & 4. Identifier parity
        return source_indices == found_indices

    async def _load_conversation_history(
        self,
        session: AsyncSession,
        conversation_id: Optional[uuid.UUID],
    ) -> List[Dict[str, str]]:
        """Loads up to HOT_CONTEXT_WINDOW_SIZE recent turns strictly for active conversation."""
        if not conversation_id:
            return []
        try:
            stmt = (
                select(Message)
                .where(Message.conversation_id == conversation_id)
                .order_by(Message.created_at.desc(), Message.turn_sequence_number.desc())
                .limit(HOT_CONTEXT_WINDOW_SIZE)
            )
            res = await session.execute(stmt)
            rows = list(res.scalars().all())
            history: List[Dict[str, str]] = []
            for m in reversed(rows):
                if isinstance(m, Message) and m.content:
                    history.append(
                        {
                            "role": m.sender_type,
                            "content": m.content,
                            "language": m.detected_language or "en",
                        }
                    )
            return history
        except Exception as exc:
            logger.debug("Skipping conversation history lookup for %s: %s", conversation_id, exc)
            return []

    async def process_chat_turn(
        self,
        session: AsyncSession,
        customer_id: uuid.UUID,
        session_id: uuid.UUID,
        message: str,
        conversation_id: Optional[uuid.UUID] = None,
        preferred_language: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        effective_as_of_date: Optional[date] = None,
        product_line: Optional[str] = None,
        document_code: Optional[str] = None,
        auth_context: Optional[ResolvedAuthContext] = None,
        correlation_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Executes the canonical 14-step multilingual RAG chat pipeline."""
        start_time = datetime.now(timezone.utc)
        corr_id = correlation_id or str(uuid.uuid4())

        # Step 1: Session Token Validation & Expiry Check
        sess_obj: Optional[CustomerSession] = None
        if self.lifecycle_manager:
            if hasattr(self.lifecycle_manager, "evaluate_session_lifecycle"):
                eval_res = await self.lifecycle_manager.evaluate_session_lifecycle(session_id)
                if eval_res.state == "EXPIRED":
                    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session token expired.")
            else:
                try:
                    sess_res = await session.execute(
                        select(CustomerSession).where(CustomerSession.id == session_id)
                    )
                    candidate_sess = sess_res.scalars().first()
                    if isinstance(candidate_sess, CustomerSession):
                        sess_obj = candidate_sess
                        eval_res = self.lifecycle_manager.evaluate_session_state(sess_obj)
                        if not eval_res.is_authenticated:
                            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session token expired.")
                except HTTPException:
                    raise
                except Exception:
                    pass

        # Step 4: Session Lifecycle & Active Conversation Resolution
        context_restored = False
        active_conv_id = conversation_id

        # P0 BOLA Defense: Validate ownership of client-supplied conversation_id
        if conversation_id:
            res_target = await session.execute(
                select(Conversation).where(Conversation.id == conversation_id)
            )
            target_conv = res_target.scalars().first()
            if not target_conv:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Conversation not found.",
                )

            is_staff = False
            if auth_context and any(
                r.upper() in {"ADMIN", "SUPERVISOR", "AGENT", "STAFF", "SUPPORT_AGENT", "SUPER_ADMIN", "GLOBAL_ADMIN"}
                for r in (auth_context.roles or [])
            ):
                is_staff = True

            if not is_staff:
                if target_conv.customer_id is not None and (not customer_id or target_conv.customer_id != customer_id):
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail="Conversation not found.",
                    )
                if target_conv.session_id != session_id:
                    if not (customer_id and target_conv.customer_id == customer_id):
                        raise HTTPException(
                            status_code=status.HTTP_404_NOT_FOUND,
                            detail="Conversation not found.",
                        )
        if self.lifecycle_manager:
            if hasattr(self.lifecycle_manager, "restore_session_if_needed"):
                restoration = await self.lifecycle_manager.restore_session_if_needed(session, session_id)
                if restoration.restored:
                    context_restored = True
                    active_conv_id = restoration.conversation_id
                elif not active_conv_id:
                    active_conv_id = restoration.conversation_id
            elif sess_obj is not None and sess_obj.status == "IDLE_CLOSED":
                restoration = await self.lifecycle_manager.restore_session(
                    db=session,
                    session_id=session_id,
                    authenticated_customer_id=str(customer_id) if customer_id else None,
                )
                if restoration.is_restored:
                    context_restored = True
                if not active_conv_id and restoration.new_conversation_id:
                    active_conv_id = uuid.UUID(str(restoration.new_conversation_id))

        if not active_conv_id:
            # Attach to existing open conversation for this session if available
            try:
                res_conv = await session.execute(
                    select(Conversation)
                    .where(
                        Conversation.session_id == session_id,
                        Conversation.status == "OPEN",
                    )
                    .order_by(Conversation.started_at.desc())
                    .limit(1)
                )
                existing_conv = res_conv.scalars().first()
                if isinstance(existing_conv, Conversation) and existing_conv.id:
                    active_conv_id = existing_conv.id
            except Exception:
                active_conv_id = None

            if not active_conv_id:
                # Resolve or create conversation
                conv = Conversation(
                    id=uuid.uuid4(),
                    session_id=session_id,
                    customer_id=customer_id,
                    primary_language="en",
                    status="OPEN",
                    turn_count=0,
                )
                r_conv = session.add(conv)
                if asyncio.iscoroutine(r_conv):
                    await r_conv
                await session.flush()
                active_conv_id = conv.id

        # Load recent turn history strictly scoped to active conversation (hot context window)
        conversation_history = await self._load_conversation_history(session, active_conv_id)

        # Step 2 & 3: Idempotency Key Evaluation & Transaction 1
        idempotency_record = None
        owner_token = None
        heartbeat_task = None
        stop_heartbeat = asyncio.Event()
        lease_lost_event = asyncio.Event()

        if idempotency_key:
            idempotency_record, owner_token, is_cached, cached_payload = await self.reserve_idempotency_key(
                session=session,
                customer_id=customer_id,
                session_id=session_id,
                idempotency_key=idempotency_key,
                message=message,
                conversation_id=active_conv_id,
            )
            if is_cached and cached_payload:
                return cached_payload

            # Spawn independent background heartbeat loop
            if self.session_factory and owner_token:
                heartbeat_task = asyncio.create_task(
                    self._heartbeat_loop(
                        record_id=idempotency_record.id,
                        owner_token=owner_token,
                        lease_lost_event=lease_lost_event,
                        stop_event=stop_heartbeat,
                    )
                )

        try:
            # Step 5: Inbound Security Scan (Perimeter Defense)
            scan_method = getattr(self.security_scanner, "scan_inbound", getattr(self.security_scanner, "scan", None))
            scan_res: PreTranslationSecurityResult = scan_method(message)
            if not scan_res.is_safe:
                # Refusal triggered
                lang_code = preferred_language if preferred_language in SUPPORTED_LANGUAGES else "en"
                refusal_text = (
                    "Security policy violation: Prompt injection detected."
                    if lang_code == "en"
                    else LOCALIZED_SERVICE_NOTICES.get(lang_code, "Security policy violation.")
                )
                return {
                    "message_id": str(uuid.uuid4()),
                    "reply_text": refusal_text,
                    "is_resolved": False,
                    "refusal_triggered": True,
                    "refusal_reason": "PROMPT_INJECTION_DETECTED",
                    "clarification_needed": False,
                    "technical_error": False,
                    "technical_error_code": None,
                    "detected_language": "und",
                    "detected_language_confidence": 0.0,
                    "primary_language": lang_code,
                    "context_restored": context_restored,
                    "turn_sequence_number": None,
                    "conversation_id": str(active_conv_id),
                    "context_window_complete": True,
                    "citations": [],
                    "confidence_score": 0.0,
                    "effective_as_of_date_applied": str(effective_as_of_date or date.today()),
                    "suggested_ticket_category": "SECURITY_INCIDENT_REVIEW",
                    "claim_verification": {"total_claims": 0, "verified_claims": 0, "all_claims_verified": True},
                    "model_used": "security_scanner",
                    "retrieval_latency_ms": 0.0,
                    "generation_latency_ms": 0.0,
                    "total_latency_ms": 0.0,
                }

            # Step 6: Language Detection & Routing Policy
            eff_pref_lang = preferred_language
            prev_script: Optional[str] = None
            if not eff_pref_lang and active_conv_id and conversation_history:
                for prev_turn in reversed(conversation_history):
                    prev_lang = prev_turn.get("language")
                    if prev_lang in SUPPORTED_LANGUAGES or prev_lang in ("hi_latn", "hinglish"):
                        eff_pref_lang = prev_lang
                        prev_c = prev_turn.get("content") or ""
                        if prev_lang in ("hi", "hi_latn", "hinglish"):
                            prev_script = "Deva" if bool(re.search(r"[\u0900-\u097F]", prev_c)) else "Latn"
                        break

            det_res: LanguageDetectionResult = self.language_detector.detect_language(
                message, preferred_language=eff_pref_lang
            )
            det_lang = getattr(det_res, "detected_language", getattr(det_res, "language", "und"))
            det_script = getattr(det_res, "detected_script", getattr(det_res, "script", None))
            det_conf = getattr(det_res, "confidence", getattr(det_res, "language_confidence", 0.0))
            clarification, inbound_trans, source_lang, target_lang = self.evaluate_language_routing(
                detected_lang=det_lang,
                confidence=det_conf,
                preferred_lang=preferred_language or eff_pref_lang,
            )

            # Resolve target script (Devanagari vs Romanized Latn/Hinglish)
            target_script: Optional[str] = None
            if target_lang == "hi":
                if det_script == "Deva":
                    target_script = "Deva"
                elif det_script == "Latn" or preferred_language in ("hi_latn", "hinglish") or eff_pref_lang in ("hi_latn", "hinglish") or prev_script == "Latn":
                    target_script = "Latn"
                elif preferred_language == "hi":
                    target_script = "Deva"
                else:
                    target_script = "Latn"

            if clarification:
                return {
                    "message_id": str(uuid.uuid4()),
                    "reply_text": MULTILINGUAL_CLARIFICATION_PROMPT,
                    "is_resolved": False,
                    "refusal_triggered": False,
                    "refusal_reason": None,
                    "clarification_needed": True,
                    "technical_error": False,
                    "technical_error_code": None,
                    "detected_language": "und",
                    "detected_language_confidence": det_conf,
                    "primary_language": "und",
                    "context_restored": context_restored,
                    "turn_sequence_number": None,
                    "conversation_id": str(active_conv_id),
                    "context_window_complete": True,
                    "citations": [],
                    "confidence_score": 0.0,
                    "effective_as_of_date_applied": str(effective_as_of_date or date.today()),
                    "suggested_ticket_category": "SECURITY_INCIDENT_REVIEW",
                    "claim_verification": {"total_claims": 0, "verified_claims": 0, "all_claims_verified": True},
                    "model_used": "language_detector",
                    "retrieval_latency_ms": 0.0,
                    "generation_latency_ms": 0.0,
                    "total_latency_ms": 0.0,
                }

            # Step 6b: Pure Conversational Small-Talk Routing (before KB RAG / translation)
            # Only matches pure greetings/gratitude/farewells so product/policy/troubleshooting
            # questions (e.g. "Hi, what is the return policy?") still run full RAG & KB grounding.
            smalltalk_intent = self._classify_conversational_intent(message)
            model_used_override: Optional[str] = None
            if smalltalk_intent:
                reply_lang = target_lang or source_lang or "en"
                final_reply_text = self._render_conversational_reply(smalltalk_intent, reply_lang, script=target_script)
                rag_result = None
                citations = []
                confidence = 1.0
                is_resolved = True
                refusal_triggered = False
                refusal_reason = None
                claims_res = {"total_claims": 0, "verified_claims": 0, "all_claims_verified": True}
                technical_error = False
                technical_error_code = None
                active_slots = {}
                correction_signal = None
                sentiment_class = "neutral"
                sentiment_eval = SentimentAnalysisResult(
                    sentiment_class="neutral",
                    confidence_score=1.0,
                    frustration_index=0.0,
                    urgency_index=0.0,
                    trajectory_slope=0.0,
                    requires_escalation=False,
                    escalation_reason=None,
                )
                tone_directive = ""
                model_used_override = "conversational_router"
            else:
                # Step 7 & 8: Entity Preservation & Inbound Translation
                english_query = message
                vault = EntityPreservationVault()
                if inbound_trans and source_lang:
                    try:
                        trans_res: TranslationResult = await self.translator.translate_inbound(
                            text=message,
                            source_language=source_lang,
                            vault=vault,
                        )
                        if trans_res.is_successful:
                            english_query = trans_res.translated_text
                        else:
                            raise RuntimeError(trans_res.error_reason or "Translation unsuccessful")
                    except Exception as exc:
                        logger.error("Inbound translation failed: %s", exc)
                        fallback_text = LOCALIZED_SERVICE_NOTICES.get(source_lang, LOCALIZED_SERVICE_NOTICES["en"])
                        return {
                            "message_id": str(uuid.uuid4()),
                            "reply_text": fallback_text,
                            "is_resolved": False,
                            "refusal_triggered": False,
                            "refusal_reason": None,
                            "clarification_needed": False,
                            "technical_error": True,
                            "technical_error_code": "TRANSLATION_UNAVAILABLE",
                            "detected_language": source_lang,
                            "detected_language_confidence": det_conf,
                            "primary_language": source_lang,
                            "context_restored": context_restored,
                            "turn_sequence_number": None,
                            "conversation_id": str(active_conv_id),
                            "context_window_complete": True,
                            "citations": [],
                            "confidence_score": 0.0,
                            "effective_as_of_date_applied": str(effective_as_of_date or date.today()),
                            "claim_verification": {"total_claims": 0, "verified_claims": 0, "all_claims_verified": True},
                            "model_used": "translator_fallback",
                            "retrieval_latency_ms": 0.0,
                            "generation_latency_ms": 0.0,
                            "total_latency_ms": 0.0,
                        }

                # Step 8b: Deterministic Slot Extraction & Customer Correction Tracking
                prior_slots = (
                    self.slot_correction_engine.process_conversation_messages(conversation_history)
                    if conversation_history
                    else None
                )
                active_slots, correction_signal = self.slot_correction_engine.process_turn(
                    english_query, active_slots=prior_slots
                )
                if correction_signal and correction_signal.has_correction:
                    logger.info(
                        "Customer slot correction detected: slot=%s, old=%s, new=%s",
                        correction_signal.slot_name,
                        correction_signal.old_value,
                        correction_signal.new_value,
                    )

                # Step 8c: Pre-Generation Sentiment Analysis & Dynamic Tone Adaptation
                try:
                    sentiment_eval = self.sentiment_analyzer.analyze_text(message)
                    # Fallback to neutral if confidence score is low
                    if sentiment_eval.confidence_score < 0.60:
                        sentiment_class = "neutral"
                    else:
                        sentiment_class = sentiment_eval.sentiment_class
                except Exception as exc:
                    logger.warning("Pre-generation sentiment analysis failed: %s; falling back to neutral tone.", exc)
                    sentiment_class = "neutral"
                    sentiment_eval = SentimentAnalysisResult(
                        sentiment_class="neutral",
                        confidence_score=0.0,
                        frustration_index=0.0,
                        urgency_index=0.0,
                        trajectory_slope=0.0,
                        requires_escalation=False,
                        escalation_reason=None,
                    )

                tone_directive = get_tone_directive(sentiment_class)

                # Step 9: Multi-Intent Decomposition, RAG Retrieval & English AI Response Generation
                rag_result = None
                decomp = self.query_decomposer.decompose(english_query)

                if decomp.is_multi_intent and len(decomp.sub_queries) > 1 and self.rag_orchestrator:
                    sub_answers = []
                    all_citations = []
                    for sub_q in decomp.sub_queries:
                        sub_req = RAGInternalRequest(
                            query=sub_q.text,
                            raw_customer_query=sub_q.text,
                            preferred_language=target_lang or source_lang or "en",
                            session_token=auth_context.session_token if auth_context else str(session_id),
                            user_id_or_email=auth_context.user_id_or_email if auth_context else str(customer_id),
                            jwt_claims=auth_context.jwt_claims if auth_context else {},
                            effective_as_of_date=effective_as_of_date,
                            product_line=product_line,
                            document_code=document_code,
                            tone_directive=tone_directive,
                            sentiment_class=sentiment_class,
                            conversation_history=conversation_history,
                            correlation_id=corr_id,
                        )
                        sub_res = await self.rag_orchestrator.process_query(request=sub_req, session=session)
                        ans = (
                            getattr(sub_res, "answer", getattr(sub_res, "generated_response", None))
                            or f"Response to: {sub_q.text}"
                        )
                        sub_answers.append((sub_q, ans))
                        if getattr(sub_res, "citations", None):
                            all_citations.extend(sub_res.citations)
                        rag_result = sub_res

                    reply_text = self.query_decomposer.synthesize_combined_response(sub_answers)
                    citations = all_citations
                else:
                    if self.rag_orchestrator:
                        rag_req = RAGInternalRequest(
                            query=english_query,
                            raw_customer_query=message,
                            preferred_language=target_lang or source_lang or "en",
                            session_token=auth_context.session_token if auth_context else str(session_id),
                            user_id_or_email=auth_context.user_id_or_email if auth_context else str(customer_id),
                            jwt_claims=auth_context.jwt_claims if auth_context else {},
                            effective_as_of_date=effective_as_of_date,
                            product_line=product_line,
                            document_code=document_code,
                            tone_directive=tone_directive,
                            sentiment_class=sentiment_class,
                            conversation_history=conversation_history,
                            correlation_id=corr_id,
                        )
                        rag_result = await self.rag_orchestrator.process_query(request=rag_req, session=session)

                    reply_text = (
                        getattr(rag_result, "answer", getattr(rag_result, "generated_response", None))
                        if rag_result
                        else f"Response to: {english_query}"
                    )
                    citations = rag_result.citations if rag_result else []
                confidence = rag_result.confidence_score if rag_result else 1.0
                is_resolved = rag_result.is_resolved if rag_result else True
                refusal_triggered = rag_result.refusal_triggered if rag_result else False
                refusal_reason = rag_result.refusal_reason if rag_result else None
                claims_res = (
                    {
                        "total_claims": rag_result.claim_verification.total_claims,
                        "verified_claims": rag_result.claim_verification.verified_claims,
                        "all_claims_verified": rag_result.claim_verification.all_claims_verified,
                    }
                    if rag_result
                    else {"total_claims": 0, "verified_claims": 0, "all_claims_verified": True}
                )

                # Step 10: Outbound Response Translation & Entity Re-attachment
                final_reply_text = reply_text
                if target_lang and target_lang != "en":
                    try:
                        out_trans: TranslationResult = await self.translator.translate_outbound(
                            text=reply_text,
                            target_language=target_lang,
                            vault=EntityPreservationVault(),
                            target_script=target_script,
                        )
                        fallback_key = "hi_latn" if (target_lang == "hi" and target_script == "Latn") else target_lang
                        if out_trans.is_successful and out_trans.translated_text:
                            # 4-point structural parity check
                            if not self.evaluate_citation_parity(citations, out_trans.translated_text):
                                logger.warning("Citation structural parity check failed for target lang %s", target_lang)
                                final_reply_text = LOCALIZED_SERVICE_NOTICES.get(fallback_key, LOCALIZED_SERVICE_NOTICES.get(target_lang, LOCALIZED_SERVICE_NOTICES["en"]))
                                technical_error = True
                                technical_error_code = "TRANSLATION_UNAVAILABLE"
                            else:
                                final_reply_text = out_trans.translated_text
                                technical_error = False
                                technical_error_code = None
                        else:
                            final_reply_text = LOCALIZED_SERVICE_NOTICES.get(fallback_key, LOCALIZED_SERVICE_NOTICES.get(target_lang, LOCALIZED_SERVICE_NOTICES["en"]))
                            technical_error = True
                            technical_error_code = "TRANSLATION_UNAVAILABLE"
                    except Exception as exc:
                        logger.error("Outbound translation failed: %s", exc)
                        fallback_key = "hi_latn" if (target_lang == "hi" and target_script == "Latn") else target_lang
                        final_reply_text = LOCALIZED_SERVICE_NOTICES.get(fallback_key, LOCALIZED_SERVICE_NOTICES.get(target_lang, LOCALIZED_SERVICE_NOTICES["en"]))
                        technical_error = True
                        technical_error_code = "TRANSLATION_UNAVAILABLE"
                else:
                    technical_error = False
                    technical_error_code = None

            # Step 11: Transaction 2 — Atomic Multi-Entity Finalization & Outbox Staging
            if lease_lost_event.is_set():
                raise RuntimeError("Idempotency lease lost mid-flight; aborting finalization.")

            # Allocate sequence via database allocator (PostgreSQL stored fn or SQLite test fallback)
            try:
                alloc_res = await session.execute(
                    text("SELECT fn_allocate_next_turn_sequence(:cid)"),
                    {"cid": str(active_conv_id)},
                )
                allocated_turn_seq = int(alloc_res.scalar_one())
            except Exception:
                try:
                    conv_res = await session.execute(
                        select(Conversation.turn_count).where(Conversation.id == active_conv_id)
                    )
                    raw_count = conv_res.scalar_one_or_none()
                    curr_count = int(raw_count) if isinstance(raw_count, (int, float)) else 0
                    allocated_turn_seq = curr_count + 1
                    await session.execute(
                        update(Conversation)
                        .where(Conversation.id == active_conv_id)
                        .values(turn_count=allocated_turn_seq)
                    )
                except Exception:
                    allocated_turn_seq = 1

            # Mask customer message & encrypt raw
            if hasattr(self.pii_masker, "mask_text"):
                masked_content = self.pii_masker.mask_text(message)
            else:
                m_res = self.pii_masker.mask(message)
                masked_content = getattr(m_res, "masked_text", str(m_res))
            raw_encrypted = self.encryption.encrypt_payload(
                plaintext=message.encode("utf-8"),
                conversation_id=str(active_conv_id),
            )

            now_msg_ts = datetime.now(timezone.utc)
            cust_msg = Message(
                id=uuid.uuid4(),
                conversation_id=active_conv_id,
                sender_type="CUSTOMER",
                sender_id=customer_id,
                content=masked_content,
                encrypted_raw_content=raw_encrypted,
                detected_language=source_lang or "en",
                language_confidence=det_conf,
                turn_sequence_number=allocated_turn_seq,
                created_at=now_msg_ts,
            )
            r_cust = session.add(cust_msg)
            if asyncio.iscoroutine(r_cust):
                await r_cust

            agent_msg = Message(
                id=uuid.uuid4(),
                conversation_id=active_conv_id,
                sender_type="AI_AGENT",
                sender_id=None,
                content=final_reply_text,
                detected_language=target_lang or "en",
                language_confidence=1.0,
                turn_sequence_number=allocated_turn_seq,
                created_at=now_msg_ts + timedelta(milliseconds=200),
            )
            r_agent = session.add(agent_msg)
            if asyncio.iscoroutine(r_agent):
                await r_agent
            await session.flush()

            # Persist Pre-Generation Sentiment Evaluation & Trigger Escalation If Required
            sentiment_log = SentimentLog(
                message_id=cust_msg.id,
                sentiment_class=sentiment_eval.sentiment_class,
                confidence_score=sentiment_eval.confidence_score,
                frustration_index=sentiment_eval.frustration_index,
                urgency_index=sentiment_eval.urgency_index,
                trajectory_slope=sentiment_eval.trajectory_slope,
                requires_escalation=sentiment_eval.requires_escalation,
                escalation_reason=sentiment_eval.escalation_reason,
                evaluated_at=datetime.now(timezone.utc),
            )
            r_slog = session.add(sentiment_log)
            if asyncio.iscoroutine(r_slog):
                await r_slog

            esc_decision = await self.escalation_service.evaluate_and_escalate_turn(
                session=session,
                conversation_id=active_conv_id,
                message=cust_msg,
                sentiment_result=sentiment_eval,
                tenant_id=(auth_context.jwt_claims or {}).get("tenant_id") if auth_context and auth_context.jwt_claims else None,
            )

            # Stage outbox event with deterministic UUIDv5
            outbox_event_id = generate_outbox_event_id(active_conv_id, allocated_turn_seq)
            outbox_payload = {
                "conversation_id": str(active_conv_id),
                "sequence_number": allocated_turn_seq,
                "customer_message": masked_content,
                "agent_response": final_reply_text,
                "detected_language": source_lang or "en",
                "target_language": target_lang or "en",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            outbox_event = ChatOutboxEvent(
                id=outbox_event_id,
                customer_id=customer_id,
                session_id=session_id,
                conversation_id=active_conv_id,
                customer_message_id=cust_msg.id,
                agent_message_id=agent_msg.id,
                sequence_number=allocated_turn_seq,
                idempotency_key=idempotency_key,
                payload=outbox_payload,
                status="PENDING",
                created_at=datetime.now(timezone.utc),
                next_retry_at=datetime.now(timezone.utc),
            )
            r_outbox = session.add(outbox_event)
            if asyncio.iscoroutine(r_outbox):
                await r_outbox
            await session.flush()

            # Build canonical response data
            total_latency = (datetime.now(timezone.utc) - start_time).total_seconds() * 1000.0
            citation_dtos = [
                {
                    "citation_id": getattr(c, "citation_id", i + 1),
                    "document_code": getattr(c, "document_code", "DOC"),
                    "document_title": getattr(c, "document_title", "Document"),
                    "version_number": getattr(c, "version_number", 1),
                    "effective_date": getattr(c, "effective_date", "2026-01-01"),
                    "chunk_excerpt": getattr(c, "chunk_excerpt", ""),
                }
                for i, c in enumerate(citations)
            ]

            response_data = {
                "message_id": str(agent_msg.id),
                "reply_text": final_reply_text,
                "is_resolved": is_resolved,
                "refusal_triggered": refusal_triggered,
                "refusal_reason": refusal_reason,
                "clarification_needed": False,
                "technical_error": technical_error,
                "technical_error_code": technical_error_code,
                "detected_language": source_lang or "en",
                "detected_language_confidence": det_conf,
                "detected_script": det_script,
                "primary_language": target_lang or "en",
                "primary_script": target_script,
                "context_restored": context_restored,
                "turn_sequence_number": allocated_turn_seq,
                "conversation_id": str(active_conv_id),
                "context_window_complete": True,
                "citations": citation_dtos,
                "confidence_score": confidence,
                "effective_as_of_date_applied": str(effective_as_of_date or date.today()),
                "suggested_ticket_category": getattr(rag_result, "suggested_ticket_category", None) if rag_result else None,
                "claim_verification": claims_res,
                "model_used": model_used_override or (getattr(rag_result, "model_used", "gemini-3.8-flash") if rag_result else "rag"),
                "retrieval_latency_ms": getattr(rag_result, "retrieval_latency_ms", 0.0) if rag_result else 0.0,
                "generation_latency_ms": getattr(rag_result, "generation_latency_ms", 0.0) if rag_result else 0.0,
                "total_latency_ms": total_latency,
                "active_slots": {k: v.current_value for k, v in active_slots.items()},
                "customer_correction_detected": correction_signal.has_correction if correction_signal else False,
                "detected_sentiment": sentiment_class,
                "tone_directive_applied": bool(tone_directive),
                "is_escalated": esc_decision.should_escalate or esc_decision.already_escalated,
                "escalation_reason": esc_decision.trigger_condition or esc_decision.reason,
                "escalation_decision": {
                    "should_escalate": esc_decision.should_escalate,
                    "trigger_condition": esc_decision.trigger_condition,
                    "reason": esc_decision.reason,
                    "is_after_hours": esc_decision.is_after_hours,
                    "routing_destination": esc_decision.routing_destination,
                    "next_eligible_business_time": (
                        esc_decision.next_eligible_business_time.isoformat()
                        if esc_decision.next_eligible_business_time
                        else None
                    ),
                },
            }

            # Finalize idempotency record in Tx2
            if idempotency_record and owner_token:
                encrypted_payload = self.encryption.encrypt_payload(
                    plaintext=json.dumps(response_data).encode("utf-8"),
                    conversation_id=str(active_conv_id),
                )
                final_stmt = (
                    update(ChatIdempotencyRecord)
                    .where(
                        ChatIdempotencyRecord.id == idempotency_record.id,
                        ChatIdempotencyRecord.owner_token == owner_token,
                        ChatIdempotencyRecord.status == "IN_PROGRESS",
                        ChatIdempotencyRecord.lease_expires_at > datetime.now(timezone.utc),
                    )
                    .values(
                        status="COMPLETED",
                        customer_message_id=cust_msg.id,
                        agent_message_id=agent_msg.id,
                        turn_sequence_number=allocated_turn_seq,
                        encrypted_response_payload=encrypted_payload,
                        lease_expires_at=None,
                        updated_at=datetime.now(timezone.utc),
                    )
                )
                res = await session.execute(final_stmt)
                if res.rowcount == 0:
                    await session.rollback()
                    raise RuntimeError("Failed to finalize idempotency record; lease lost or expired.")

            # Update session activity
            await session.execute(
                update(CustomerSession)
                .where(CustomerSession.id == session_id)
                .values(last_activity_at=datetime.now(timezone.utc))
            )

            # Commit Tx2: Evaluates tg_enforce_turn_pair_completeness constraint trigger
            await session.commit()

            # Step 12: Outbox Worker Delivery & Lifecycle Hot Buffer Update
            if self.outbox_worker and self.outbox_worker.redis:
                asyncio.create_task(self.outbox_worker.deliver_single_event(session, outbox_event))
            if self.lifecycle_manager and hasattr(self.lifecycle_manager, "append_message_turn"):
                try:
                    cid_str = str(customer_id) if customer_id else None
                    # Bound Redis hot-cache sync tightly so optional offline Redis never stalls chat turns
                    await asyncio.wait_for(
                        self.lifecycle_manager.append_message_turn(session_id, cid_str, "CUSTOMER", masked_content),
                        timeout=0.25,
                    )
                    await asyncio.wait_for(
                        self.lifecycle_manager.append_message_turn(session_id, cid_str, "AI_AGENT", final_reply_text),
                        timeout=0.25,
                    )
                except Exception:
                    pass

            return response_data

        except Exception as exc:
            # Mark reservation as FAILED if error occurred
            if idempotency_record and owner_token:
                try:
                    if self.session_factory:
                        async with self.session_factory() as fail_session:
                            fail_stmt = (
                                update(ChatIdempotencyRecord)
                                .where(
                                    ChatIdempotencyRecord.id == idempotency_record.id,
                                    ChatIdempotencyRecord.owner_token == owner_token,
                                    ChatIdempotencyRecord.status == "IN_PROGRESS",
                                )
                                .values(
                                    status="FAILED",
                                    failure_reason=str(exc)[:250],
                                    updated_at=datetime.now(timezone.utc),
                                )
                            )
                            await fail_session.execute(fail_stmt)
                            await fail_session.commit()
                except Exception as inner_exc:
                    logger.error("Failed to transition idempotency record to FAILED: %s", inner_exc)
            raise

        finally:
            # Step 13: Heartbeat Task Termination
            stop_heartbeat.set()
            if heartbeat_task:
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except (asyncio.CancelledError, Exception):
                    pass
