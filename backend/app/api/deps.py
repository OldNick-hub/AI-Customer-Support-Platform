"""FastAPI Dependency Providers.

Coordinates:
1. Asynchronous database session lifecycle (get_db).
2. Request correlation and distributed tracing headers (get_correlation_id).
3. Authoritative server-side authentication (get_auth_context):
   - Customer Session Token (X-Session-Token) validation against PostgreSQL.
   - Staff JWT Bearer Token (Authorization: Bearer) cryptographic validation (HS256).
   - Rejection of missing, invalid, expired, malformed, or conflicting credentials.
4. RAG Orchestrator singleton dependency (get_rag_orchestrator).
"""

from datetime import datetime, timezone
import logging
from typing import Any, AsyncGenerator, Dict, List, Optional
import uuid

from fastapi import Depends, Header, HTTPException, Request, status
import jwt
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.logging import configure_safe_logger
from app.db.session import get_db
from app.models.auth import Role, User
from app.models.customer import Customer, Session as CustomerSession
from app.modules.rag.orchestrator import RAGOrchestrator

logger = configure_safe_logger(logging.getLogger(__name__))


class ResolvedAuthContext(BaseModel):
    """Authoritative server-side identity context derived from verified credentials."""

    session_token: Optional[str] = None
    user_id_or_email: Optional[str] = None
    jwt_claims: Optional[Dict[str, Any]] = None
    auth_type: str  # "SESSION_TOKEN" or "JWT"
    customer_id: Optional[str] = None
    session_id: Optional[str] = None
    roles: List[str] = Field(default_factory=list)


def get_correlation_id(
    request: Request,
    x_request_id: Optional[str] = Header(None, alias="X-Request-ID"),
    x_correlation_id: Optional[str] = Header(None, alias="X-Correlation-ID"),
) -> str:
    """Extracts or generates request correlation ID for distributed tracing."""
    raw_id = x_request_id or x_correlation_id
    if raw_id and isinstance(raw_id, str) and raw_id.strip():
        corr_id = raw_id.strip()[:128]
    else:
        corr_id = str(uuid.uuid4())

    request.state.correlation_id = corr_id
    return corr_id


async def get_auth_context(
    request: Request,
    db: AsyncSession = Depends(get_db),
    x_session_token: Optional[str] = Header(None, alias="X-Session-Token"),
    authorization: Optional[str] = Header(None, alias="Authorization"),
) -> ResolvedAuthContext:
    """Resolves authoritative server-side identity from customer session or staff JWT.

    Strict Security Guarantees:
    1. Anonymous access is FORBIDDEN on /api/v1/chat/message -> raises 401.
    2. Conflicting authentication headers (both session token and bearer JWT provided) -> raises 401.
    3. Invalid, expired, malformed, or missing tokens fail closed -> raises 401.
    4. Client-supplied customer IDs, roles, access levels, or security contexts are NEVER trusted.
    """
    has_session = bool(x_session_token and isinstance(x_session_token, str) and x_session_token.strip())
    has_auth_header = bool(authorization and isinstance(authorization, str) and authorization.strip())

    # Case 1: Missing all credentials -> Fail Closed (401)
    if not has_session and not has_auth_header:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication credentials are required to interact with the knowledge assistant.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Case 2: Conflicting credentials -> Fail Closed (401)
    if has_session and has_auth_header:
        logger.warning("Rejected conflicting authentication credentials (both X-Session-Token and Authorization provided).")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Conflicting authentication credentials provided. Provide either X-Session-Token or Authorization: Bearer, not both.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Case 3: Staff JWT Authentication (Authorization: Bearer <token>)
    if has_auth_header:
        auth_str = str(authorization).strip()
        parts = auth_str.split()
        if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid Authorization header format. Expected 'Bearer <token>'.",
                headers={"WWW-Authenticate": "Bearer"},
            )

        token = parts[1].strip()

        # Strict 7-Point Cryptographic JWT Validation
        try:
            payload = jwt.decode(
                token,
                settings.SECRET_KEY,
                algorithms=[settings.ALGORITHM],
                options={"verify_exp": True, "verify_signature": True},
            )
        except jwt.ExpiredSignatureError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Staff authentication token has expired.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        except jwt.InvalidTokenError as exc:
            logger.warning("JWT validation failed: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid staff authentication token.",
                headers={"WWW-Authenticate": "Bearer"},
            )

        sub = payload.get("sub")
        if not sub or not str(sub).strip():
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Staff token missing valid subject ('sub') claim.",
                headers={"WWW-Authenticate": "Bearer"},
            )

        sub_str = str(sub).strip()

        # Database User Existence & Active Check
        is_uuid = False
        try:
            parsed_uuid = uuid.UUID(sub_str)
            is_uuid = True
        except ValueError:
            pass

        if is_uuid:
            stmt = select(User).where(User.id == parsed_uuid).options(selectinload(User.roles))
        else:
            stmt = select(User).where(User.email == sub_str).options(selectinload(User.roles))

        res = await db.execute(stmt)
        user = res.scalar_one_or_none()

        if not user:
            logger.warning("Staff user not found in database for subject: %s", sub_str)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Staff user record not found.",
                headers={"WWW-Authenticate": "Bearer"},
            )

        if not user.is_active:
            logger.warning("Inactive staff user account rejected: %s", user.email)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Staff user account is inactive or disabled.",
                headers={"WWW-Authenticate": "Bearer"},
            )

        role_names = [r.name for r in (user.roles or [])]

        return ResolvedAuthContext(
            user_id_or_email=str(user.id),
            jwt_claims=payload,
            auth_type="JWT",
            roles=role_names,
        )

    # Case 4: Customer Session Authentication (X-Session-Token)
    token_str = str(x_session_token).strip()
    if not token_str:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Supplied customer session token is empty or malformed.",
        )

    stmt = (
        select(CustomerSession)
        .where(CustomerSession.session_token == token_str)
        .options(selectinload(CustomerSession.customer))
    )
    res = await db.execute(stmt)
    sess_record = res.scalar_one_or_none()

    if not sess_record:
        logger.warning("Customer session token not found in database.")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Customer session not found or invalid.",
        )

    now = datetime.now(timezone.utc)
    if sess_record.expires_at <= now:
        logger.warning("Customer session has expired (%s).", sess_record.expires_at)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Customer session has expired or is inactive.",
        )

    if sess_record.status == "ARCHIVED":
        logger.warning("Customer session is archived.")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Customer session is archived and cannot authenticate.",
        )

    if sess_record.status == "IDLE_CLOSED":
        idle_seconds = (now - sess_record.last_activity_at).total_seconds()
        if idle_seconds > 86400:
            logger.warning("Customer session 24h restoration window has expired (%s s).", idle_seconds)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Customer session restoration window has expired.",
            )
    elif sess_record.status != "ACTIVE":
        logger.warning("Customer session has invalid status (%s).", sess_record.status)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Customer session has expired or is inactive.",
        )

    return ResolvedAuthContext(
        session_token=token_str,
        auth_type="SESSION_TOKEN",
        customer_id=str(sess_record.customer_id) if sess_record.customer_id else None,
        session_id=str(sess_record.id),
    )


# RAG Orchestrator Cached Dependency
_orchestrator_instance: Optional[RAGOrchestrator] = None
_multilingual_orchestrator_instance: Optional[Any] = None


def get_rag_orchestrator() -> RAGOrchestrator:
    """Returns singleton/cached instance of RAGOrchestrator.

    Can be overridden in tests via app.dependency_overrides[get_rag_orchestrator].
    """
    global _orchestrator_instance
    if _orchestrator_instance is None:
        _orchestrator_instance = RAGOrchestrator()
    return _orchestrator_instance


def get_multilingual_orchestrator() -> Any:
    """Returns singleton/cached instance of MultilingualOrchestrator.

    Can be overridden in tests via app.dependency_overrides[get_multilingual_orchestrator].
    """
    global _multilingual_orchestrator_instance
    if _multilingual_orchestrator_instance is None:
        from app.db.session import AsyncSessionLocal
        from app.modules.multilingual.orchestrator import MultilingualOrchestrator
        _multilingual_orchestrator_instance = MultilingualOrchestrator(
            session_factory=AsyncSessionLocal,
            rag_orchestrator=get_rag_orchestrator(),
        )
    return _multilingual_orchestrator_instance

