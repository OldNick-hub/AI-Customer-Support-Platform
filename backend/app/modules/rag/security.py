"""Server-Side Identity-Bound Security Context & Authorization Resolver.

Enforces the Principle of Zero Client Trust:
- Client-supplied access_level, role, or permitted_regions are completely ignored.
- Identity is derived strictly from server-side authenticated sessions or cryptographic tokens.
- Differentiates customer tiers (PUBLIC, REGISTERED, VIP) and staff roles (AGENT, SUPERVISOR, ADMIN).
- Enforces strict default-deny for unknown customer regions with Safe GLOBAL non-statutory access.
"""

from datetime import datetime, timezone
import logging
from typing import Any, Dict, List, Optional, Set
import uuid

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.exceptions import RAGAuthorizationError
from app.core.logging import configure_safe_logger
from app.models.auth import Role, User
from app.models.customer import Customer, Session

logger = configure_safe_logger(logging.getLogger(__name__))

SAFE_GLOBAL_CATEGORIES: Set[str] = {
    "TROUBLESHOOTING",
    "PRODUCT_MANUAL",
    "GENERAL_FAQ",
    "FAQ",
}

CUSTOMER_ALLOWED_CATEGORIES: Set[str] = {
    "POLICY",
    "TROUBLESHOOTING",
    "PRODUCT_MANUAL",
    "FAQ",
    "GENERAL_FAQ",
    "BILLING",
}

AGENT_ALLOWED_CATEGORIES: Set[str] = {
    "POLICY",
    "TROUBLESHOOTING",
    "PRODUCT_MANUAL",
    "FAQ",
    "GENERAL_FAQ",
    "BILLING",
    "STANDARD_SOP",
    "ORDER_RESOLUTION",
}

SUPERVISOR_ALLOWED_CATEGORIES: Set[str] = {
    "POLICY",
    "TROUBLESHOOTING",
    "PRODUCT_MANUAL",
    "FAQ",
    "GENERAL_FAQ",
    "BILLING",
    "STANDARD_SOP",
    "ORDER_RESOLUTION",
    "SUPERVISOR_CONCESSION",
    "DISCRETIONARY_REFUNDS",
    "HIGH_VALUE_DISPUTES",
    "EXECUTIVE_ESCALATION_SOP",
}

SUPPORTED_STAFF_ROLES: Set[str] = {
    "ADMIN",
    "SUPERVISOR",
    "AGENT",
}


class SecurityContext(BaseModel):
    """Server-side authenticated identity and authorization context."""

    tenant_id: str = Field(default="default", description="Authoritative multi-tenant isolation partition")
    user_id: Optional[str] = None
    customer_id: Optional[str] = None
    session_id: Optional[str] = None
    role: str = Field(
        default="ANONYMOUS_CUSTOMER",
        description="ANONYMOUS_CUSTOMER, STANDARD_CUSTOMER, VIP_CUSTOMER, AGENT, SUPERVISOR, ADMIN",
    )
    access_tier: str = Field(
        default="PUBLIC",
        description="PUBLIC, REGISTERED, VIP, INTERNAL_AGENT, SUPERVISOR, ADMIN",
    )
    permitted_access_tiers: Set[str] = Field(default_factory=lambda: {"PUBLIC"})
    region: str = Field(
        default="UNKNOWN",
        description="Verified customer/staff region; defaults to UNKNOWN for unverified customers",
    )
    permitted_regions: Set[str] = Field(default_factory=lambda: {"GLOBAL"})
    permitted_categories: Set[str] = Field(
        default_factory=lambda: {"TROUBLESHOOTING", "PRODUCT_MANUAL", "GENERAL_FAQ"}
    )
    permitted_product_lines: Optional[Set[str]] = None
    auth_type: str = Field(
        default="ANONYMOUS",
        description="SESSION_TOKEN, JWT, ANONYMOUS",
    )
    is_safe_global_only: bool = Field(
        default=True,
        description="True if region is unknown; restricts to safe non-statutory global documents",
    )


class SecurityContextResolver:
    """Resolves authenticated security contexts with server-side identity verification."""

    @staticmethod
    def resolve_anonymous() -> SecurityContext:
        """Returns the default anonymous customer security context with default-deny on regional docs."""
        return SecurityContext(
            user_id=None,
            customer_id=None,
            session_id=None,
            role="ANONYMOUS_CUSTOMER",
            access_tier="PUBLIC",
            permitted_access_tiers={"PUBLIC"},
            region="UNKNOWN",
            permitted_regions={"GLOBAL"},
            permitted_categories=set(SAFE_GLOBAL_CATEGORIES),
            permitted_product_lines=None,
            auth_type="ANONYMOUS",
            is_safe_global_only=True,
        )

    @classmethod
    async def resolve_from_customer_session(
        cls,
        session: AsyncSession,
        session_token: str,
    ) -> SecurityContext:
        """Resolves security context from a valid server-side customer session."""
        if not session_token or not str(session_token).strip():
            raise RAGAuthorizationError(
                message="Supplied customer session token is empty or malformed."
            )

        stmt = (
            select(Session)
            .where(Session.session_token == session_token.strip())
            .options(selectinload(Session.customer))
        )
        res = await session.execute(stmt)
        sess_record = res.scalar_one_or_none()

        if not sess_record:
            raise RAGAuthorizationError(
                message="Customer session not found or invalid.",
                detail={"session_token_prefix": session_token[:8] + "..."},
            )

        now = datetime.now(timezone.utc)
        if sess_record.expires_at <= now:
            raise RAGAuthorizationError(
                message="Customer session has expired.",
                detail={"status": sess_record.status, "expires_at": sess_record.expires_at.isoformat()},
            )

        if sess_record.status == "ARCHIVED":
            raise RAGAuthorizationError(
                message="Customer session is archived and cannot authenticate.",
                detail={"status": sess_record.status},
            )

        if sess_record.status == "IDLE_CLOSED":
            idle_seconds = (now - sess_record.last_activity_at).total_seconds()
            if idle_seconds > 86400:
                raise RAGAuthorizationError(
                    message="Customer session restoration window has expired.",
                    detail={"idle_seconds": idle_seconds},
                )
        elif sess_record.status != "ACTIVE":
            raise RAGAuthorizationError(
                message="Customer session has expired or is inactive.",
                detail={"status": sess_record.status},
            )

        customer = sess_record.customer
        if not customer:
            # Anonymous session without linked customer profile
            return SecurityContext(
                user_id=None,
                customer_id=None,
                session_id=str(sess_record.id),
                role="ANONYMOUS_CUSTOMER",
                access_tier="PUBLIC",
                permitted_access_tiers={"PUBLIC"},
                region="UNKNOWN",
                permitted_regions={"GLOBAL"},
                permitted_categories=set(SAFE_GLOBAL_CATEGORIES),
                permitted_product_lines=None,
                auth_type="SESSION_TOKEN",
                is_safe_global_only=True,
            )

        # Inspect verified customer profile
        tier_upper = (customer.customer_tier or "STANDARD").upper()
        if tier_upper in ("VIP", "ENTERPRISE"):
            role = "VIP_CUSTOMER"
            access_tier = "VIP"
            permitted_tiers = {"PUBLIC", "REGISTERED", "VIP"}
        else:
            role = "STANDARD_CUSTOMER"
            access_tier = "REGISTERED"
            permitted_tiers = {"PUBLIC", "REGISTERED"}

        # Extract verified region from metadata
        meta = customer.metadata_ or {}
        country_code = meta.get("country_code") or meta.get("region")
        if country_code and isinstance(country_code, str) and country_code.strip():
            region = country_code.strip().upper()
            permitted_regions = {"GLOBAL", region}
            is_safe_global_only = False
            permitted_categories = set(CUSTOMER_ALLOWED_CATEGORIES)
        else:
            region = "UNKNOWN"
            permitted_regions = {"GLOBAL"}
            is_safe_global_only = True
            permitted_categories = set(SAFE_GLOBAL_CATEGORIES)

        # Extract verified customer purchased products if present
        purchased_products = meta.get("purchased_products")
        permitted_product_lines = None
        if purchased_products and isinstance(purchased_products, list):
            permitted_product_lines = {p.upper() for p in purchased_products if isinstance(p, str)}

        customer_meta = getattr(customer, "metadata_", {}) or {}
        cust_tenant_id = customer_meta.get("tenant_id") or getattr(customer, "tenant_id", "default")

        return SecurityContext(
            tenant_id=cust_tenant_id,
            user_id=None,
            customer_id=str(customer.id),
            session_id=str(sess_record.id),
            role=role,
            access_tier=access_tier,
            permitted_access_tiers=permitted_tiers,
            region=region,
            permitted_regions=permitted_regions,
            permitted_categories=permitted_categories,
            permitted_product_lines=permitted_product_lines,
            auth_type="SESSION_TOKEN",
            is_safe_global_only=is_safe_global_only,
        )

    @classmethod
    async def resolve_from_staff_user(
        cls,
        session: AsyncSession,
        user_id_or_email: str,
        jwt_claims: Optional[Dict[str, Any]] = None,
    ) -> SecurityContext:
        """Resolves staff security context from authenticated User and Role records."""
        is_uuid = False
        try:
            parsed_uuid = uuid.UUID(user_id_or_email)
            is_uuid = True
        except ValueError:
            pass

        if is_uuid:
            stmt = select(User).where(User.id == parsed_uuid).options(selectinload(User.roles))
        else:
            stmt = select(User).where(User.email == user_id_or_email).options(selectinload(User.roles))

        res = await session.execute(stmt)
        user = res.scalar_one_or_none()

        if not user or not user.is_active:
            raise RAGAuthorizationError(
                message="Staff user not found or inactive.",
                detail={"identifier": user_id_or_email},
            )

        raw_roles = user.roles or []
        role_names = {r.name.upper() for r in raw_roles if r and getattr(r, "name", None)}

        # Strict Default-Deny: 1. Empty database roles are denied
        if not role_names:
            logger.warning("Staff user %s has no assigned database roles; denying access.", user_id_or_email)
            raise RAGAuthorizationError(
                message="Staff user has no assigned database roles.",
                detail={"identifier": user_id_or_email},
            )

        # Strict Default-Deny: 2. Unknown or unsupported roles are denied (including mixed valid + unknown)
        unsupported = role_names - SUPPORTED_STAFF_ROLES
        if unsupported:
            logger.warning(
                "Staff user %s has unsupported role assignments %s; denying access.",
                user_id_or_email,
                unsupported,
            )
            raise RAGAuthorizationError(
                message=f"Staff user has unsupported role assignments: {sorted(list(unsupported))}.",
                detail={"identifier": user_id_or_email, "unsupported_roles": sorted(list(unsupported))},
            )

        staff_tenant_id = (jwt_claims or {}).get("tenant_id") or "default"

        # 3. Explicit ADMIN role
        if "ADMIN" in role_names:
            return SecurityContext(
                tenant_id=staff_tenant_id,
                user_id=str(user.id),
                customer_id=None,
                session_id=None,
                role="ADMIN",
                access_tier="ADMIN",
                permitted_access_tiers={
                    "PUBLIC",
                    "REGISTERED",
                    "VIP",
                    "INTERNAL_AGENT",
                    "SUPERVISOR",
                    "ADMIN",
                },
                region="GLOBAL",
                permitted_regions={"GLOBAL", "US", "EU", "APAC"},
                permitted_categories={
                    "POLICY",
                    "TROUBLESHOOTING",
                    "PRODUCT_MANUAL",
                    "FAQ",
                    "GENERAL_FAQ",
                    "BILLING",
                    "STANDARD_SOP",
                    "ORDER_RESOLUTION",
                    "SUPERVISOR_CONCESSION",
                    "DISCRETIONARY_REFUNDS",
                    "HIGH_VALUE_DISPUTES",
                    "EXECUTIVE_ESCALATION_SOP",
                    "SYSTEM_ADMIN",
                    "SECURITY_AUDIT",
                    "REGULATORY_ARCHIVE",
                },
                permitted_product_lines=None,
                auth_type="JWT",
                is_safe_global_only=False,
            )

        # 4. Explicit SUPERVISOR role
        if "SUPERVISOR" in role_names:
            return SecurityContext(
                tenant_id=staff_tenant_id,
                user_id=str(user.id),
                customer_id=None,
                session_id=None,
                role="SUPERVISOR",
                access_tier="SUPERVISOR",
                permitted_access_tiers={
                    "PUBLIC",
                    "REGISTERED",
                    "VIP",
                    "INTERNAL_AGENT",
                    "SUPERVISOR",
                },
                region="GLOBAL",
                permitted_regions={"GLOBAL", "US", "EU", "APAC"},
                permitted_categories=set(SUPERVISOR_ALLOWED_CATEGORIES),
                permitted_product_lines=None,
                auth_type="JWT",
                is_safe_global_only=False,
            )

        # 5. Explicit AGENT role (No implicit fallback)
        if "AGENT" in role_names:
            dept = (user.department or "").upper()
            permitted_regions = {"GLOBAL"}
            if "US" in dept:
                permitted_regions.add("US")
            elif "EU" in dept:
                permitted_regions.add("EU")
            elif "APAC" in dept:
                permitted_regions.add("APAC")
            else:
                permitted_regions.update({"US", "EU", "APAC"})

            # Product scope bound to user skills if present
            permitted_products = None
            if user.skills and isinstance(user.skills, list):
                permitted_products = {s.upper() for s in user.skills if isinstance(s, str)}

            return SecurityContext(
                tenant_id=staff_tenant_id,
                user_id=str(user.id),
                customer_id=None,
                session_id=None,
                role="AGENT",
                access_tier="INTERNAL_AGENT",
                permitted_access_tiers={"PUBLIC", "REGISTERED", "VIP", "INTERNAL_AGENT"},
                region=dept or "GLOBAL",
                permitted_regions=permitted_regions,
                permitted_categories=set(AGENT_ALLOWED_CATEGORIES),
                permitted_product_lines=permitted_products,
                auth_type="JWT",
                is_safe_global_only=False,
            )

        # Fail-closed defensive denial
        raise RAGAuthorizationError(
            message="No authorized staff role matched.",
            detail={"identifier": user_id_or_email, "roles": sorted(list(role_names))},
        )

    @classmethod
    async def resolve(
        cls,
        session: AsyncSession,
        session_token: Optional[str] = None,
        jwt_claims: Optional[Dict[str, Any]] = None,
        user_id_or_email: Optional[str] = None,
        tenant_id: Optional[str] = None,
    ) -> SecurityContext:
        """Unified resolver that prioritizes staff JWT, then customer session, then anonymous.

        NEVER accepts client-specified access_level, role, or permitted_regions from untrusted payloads.
        """
        # If staff identifier provided or JWT claims with staff subject
        sec_ctx: SecurityContext
        if user_id_or_email:
            sec_ctx = await cls.resolve_from_staff_user(
                session=session,
                user_id_or_email=user_id_or_email,
                jwt_claims=jwt_claims,
            )
        elif jwt_claims and "sub" in jwt_claims:
            sec_ctx = await cls.resolve_from_staff_user(
                session=session,
                user_id_or_email=jwt_claims["sub"],
                jwt_claims=jwt_claims,
            )
        elif session_token is not None:
            if not str(session_token).strip():
                raise RAGAuthorizationError(
                    message="Supplied customer session token is empty or malformed."
                )
            sec_ctx = await cls.resolve_from_customer_session(
                session=session,
                session_token=session_token,
            )
        else:
            sec_ctx = cls.resolve_anonymous()

        if tenant_id:
            sec_ctx.tenant_id = tenant_id
        return sec_ctx

