"""Development Authentication & Identity Verification API Router.

Provides safe development credentials for local testing and UI workflows:
  POST /api/v1/auth/dev-token    — Issue authoritative credentials for a test account
  GET  /api/v1/auth/me           — Verify current caller identity context

Strict Security Controls:
  - Completely disabled outside development (fails closed with 404).
  - Never fabricates unverified identities; creates and validates authentic DB records.
  - Issues cryptographic tokens identical to production auth verification paths.
"""

from datetime import datetime, timedelta, timezone
import logging
from typing import Any, Dict, List, Optional
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
import jwt
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import ResolvedAuthContext, get_auth_context, get_db
from app.core.config import settings
from app.core.logging import configure_safe_logger
from app.core.security import hash_password
from app.models.auth import Role, User, UserRole
from app.models.customer import Customer, Session as CustomerSession

logger = configure_safe_logger(logging.getLogger(__name__))

router = APIRouter(prefix="/auth", tags=["Authentication"])


# ==============================================================================
# Security Guard
# ==============================================================================

def _check_dev_mode() -> None:
    """Enforces that development credentials endpoints are only accessible in development."""
    if not getattr(settings, "ENABLE_DEV_AUTH_ENDPOINTS", False) or settings.ENVIRONMENT != "development":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Development authentication endpoints are disabled.",
        )


# ==============================================================================
# Schemas
# ==============================================================================

class DevTokenRequest(BaseModel):
    """Request payload to issue credentials for a specific dev test account."""
    persona: str = Field(
        ...,
        description="Account key: 'customer', 'customer_vip', 'agent', 'supervisor', 'admin'",
    )


class DevTokenResponse(BaseModel):
    """Authoritative credential payload for frontend development pairing."""
    persona: str
    auth_type: str  # "SESSION_TOKEN" or "JWT"
    header_name: str  # "X-Session-Token" or "Authorization"
    header_value: str  # Header value ready to attach (e.g. token or "Bearer <token>")
    token: str
    user_id_or_email: str
    customer_id: Optional[str] = None
    roles: List[str] = Field(default_factory=list)
    customer_tier: Optional[str] = None
    display_name: str
    expires_in_seconds: int


class CurrentUserResponse(BaseModel):
    """Authoritative caller identity derived from verified request credentials."""
    auth_type: str
    user_id_or_email: Optional[str] = None
    customer_id: Optional[str] = None
    session_id: Optional[str] = None
    roles: List[str] = Field(default_factory=list)


# ==============================================================================
# Development Test Accounts Definition
# ==============================================================================

DEV_TEST_ACCOUNTS: Dict[str, Dict[str, Any]] = {
    "customer": {
        "key": "customer",
        "name": "Standard Customer",
        "email": "customer@example.com",
        "role_or_tier": "STANDARD",
        "type": "customer",
        "description": "Standard retail customer account",
    },
    "customer_vip": {
        "key": "customer_vip",
        "name": "VIP Customer",
        "email": "vip.customer@example.com",
        "role_or_tier": "VIP",
        "type": "customer",
        "description": "VIP tier customer account",
    },
    "agent": {
        "key": "agent",
        "name": "Support Agent",
        "email": settings.SEED_AGENT_EMAIL,
        "role_or_tier": "AGENT",
        "type": "staff",
        "description": "Customer support agent account",
    },
    "supervisor": {
        "key": "supervisor",
        "name": "Support Supervisor",
        "email": settings.SEED_SUPERVISOR_EMAIL,
        "role_or_tier": "SUPERVISOR",
        "type": "staff",
        "description": "Support operations supervisor account",
    },
    "admin": {
        "key": "admin",
        "name": "System Administrator",
        "email": settings.SEED_ADMIN_EMAIL,
        "role_or_tier": "ADMIN",
        "type": "staff",
        "description": "Enterprise administrator account",
    },
}


# ==============================================================================
# Endpoints
# ==============================================================================

@router.get(
    "/dev-personas",
    summary="Development personas endpoint (permanently removed and disabled)",
    include_in_schema=False,
)
async def list_dev_personas() -> None:
    """The Development Personas feature has been permanently removed and disabled."""
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Development personas feature has been permanently removed and disabled.",
    )


@router.post(
    "/dev-token",
    response_model=DevTokenResponse,
    summary="Issue authoritative credentials for a development account (development only)",
)
async def issue_dev_token(
    request: DevTokenRequest,
    db: AsyncSession = Depends(get_db),
) -> DevTokenResponse:
    """Provisions or loads real DB entities for the account and returns valid auth credentials."""
    _check_dev_mode()

    persona_key = request.persona.lower().strip()
    if persona_key not in DEV_TEST_ACCOUNTS:
        valid_keys = list(DEV_TEST_ACCOUNTS.keys())
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown persona or account key '{request.persona}'. Available keys: {valid_keys}",
        )

    persona_info = DEV_TEST_ACCOUNTS[persona_key]
    now = datetime.now(timezone.utc)

    # --------------------------------------------------------------------------
    # Case A: Customer Personas (X-Session-Token)
    # --------------------------------------------------------------------------
    if persona_info["type"] == "customer":
        cust_stmt = select(Customer).where(Customer.email == persona_info["email"])
        cust_res = await db.execute(cust_stmt)
        customer = cust_res.scalar_one_or_none()

        if not customer:
            customer = Customer(
                external_id=f"DEV-{persona_key.upper()}-{uuid.uuid4().hex[:6]}",
                email=persona_info["email"],
                full_name=persona_info["name"],
                customer_tier=persona_info["role_or_tier"],
                metadata_={"account_status": "VERIFIED", "environment": "development", "tenant_id": "default"},
            )
            db.add(customer)
            await db.flush()

        sess_stmt = (
            select(CustomerSession)
            .where(
                CustomerSession.customer_id == customer.id,
                CustomerSession.status == "ACTIVE",
                CustomerSession.expires_at > now,
            )
            .order_by(CustomerSession.expires_at.desc())
        )
        sess_res = await db.execute(sess_stmt)
        session_record = sess_res.scalar_one_or_none()

        if not session_record:
            token_str = f"dev-sess-{persona_key}-{uuid.uuid4().hex}"
            session_record = CustomerSession(
                customer_id=customer.id,
                session_token=token_str,
                current_language="en",
                status="ACTIVE",
                last_activity_at=now,
                expires_at=now + timedelta(days=7),
            )
            db.add(session_record)
            await db.flush()
        else:
            token_str = session_record.session_token

        await db.commit()

        return DevTokenResponse(
            persona=persona_key,
            auth_type="SESSION_TOKEN",
            header_name="X-Session-Token",
            header_value=token_str,
            token=token_str,
            user_id_or_email=str(customer.id),
            customer_id=str(customer.id),
            roles=[],
            customer_tier=customer.customer_tier,
            display_name=customer.full_name or persona_info["name"],
            expires_in_seconds=7 * 86400,
        )

    # --------------------------------------------------------------------------
    # Case B: Staff Personas (Authorization: Bearer <JWT>)
    # --------------------------------------------------------------------------
    role_name = persona_info["role_or_tier"]
    role_stmt = select(Role).where(Role.name == role_name)
    role_res = await db.execute(role_stmt)
    role = role_res.scalar_one_or_none()
    if not role:
        role = Role(
            name=role_name,
            description=f"Enterprise {role_name} role",
            permissions=["system:*"] if role_name == "ADMIN" else ["tickets:*"],
        )
        db.add(role)
        await db.flush()

    user_stmt = select(User).where(User.email == persona_info["email"]).options(selectinload(User.roles))
    user_res = await db.execute(user_stmt)
    user = user_res.scalar_one_or_none()

    if not user:
        user = User(
            email=persona_info["email"],
            hashed_password=hash_password("DevStaffPassword123!"),
            full_name=persona_info["name"],
            department="Customer Support Operations" if role_name != "ADMIN" else "Platform Engineering",
            skills=["general_support"],
            max_active_tickets=10,
            is_active=True,
            is_on_call=True,
        )
        db.add(user)
        await db.flush()

        user_role = UserRole(user_id=user.id, role_id=role.id)
        db.add(user_role)
        await db.flush()

        # Reload user with roles
        user_res = await db.execute(select(User).where(User.id == user.id).options(selectinload(User.roles)))
        user = user_res.scalar_one()

    # Verify role association
    existing_roles = [r.name for r in (user.roles or [])]
    if role_name not in existing_roles:
        user_role = UserRole(user_id=user.id, role_id=role.id)
        db.add(user_role)
        await db.flush()
        existing_roles.append(role_name)

    payload = {
        "sub": str(user.id),
        "email": user.email,
        "roles": existing_roles or [role_name],
        "name": user.full_name,
        "tenant_id": "default",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=24)).timestamp()),
    }
    token = jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
    await db.commit()

    return DevTokenResponse(
        persona=persona_key,
        auth_type="JWT",
        header_name="Authorization",
        header_value=f"Bearer {token}",
        token=token,
        user_id_or_email=str(user.id),
        customer_id=None,
        roles=existing_roles,
        customer_tier=None,
        display_name=user.full_name,
        expires_in_seconds=24 * 3600,
    )


@router.get(
    "/me",
    response_model=CurrentUserResponse,
    summary="Get current caller identity context",
)
async def get_current_user_context(
    auth: ResolvedAuthContext = Depends(get_auth_context),
) -> CurrentUserResponse:
    """Returns authoritative server-side identity derived from the provided credentials."""
    return CurrentUserResponse(
        auth_type=auth.auth_type,
        user_id_or_email=auth.user_id_or_email,
        customer_id=auth.customer_id,
        session_id=auth.session_id,
        roles=auth.roles,
    )

