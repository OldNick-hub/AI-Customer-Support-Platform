"""Non-Production Database Seed Script.

Idempotently provisions essential enterprise roles, non-production test accounts,
a standard customer profile, and the default business hours SLA calendar.
Never uses hardcoded production passwords; reads configurable test credentials from settings.
"""

import asyncio
import logging
from datetime import time
from typing import Dict, Any
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import hash_password
from app.db.session import AsyncSessionLocal
from app.models.auth import Role, User, UserRole
from app.models.customer import Customer
from app.models.ticket import SLACalendar

logger = logging.getLogger(__name__)


async def seed_roles(session: AsyncSession) -> Dict[str, Role]:
    """Provisions standard enterprise RBAC roles if they do not exist."""
    role_definitions = {
        "ADMIN": {
            "description": "System administrator with full configuration and index promotion authority",
            "permissions": ["system:*", "knowledge:*", "tickets:*", "users:*", "audit:*"],
        },
        "SUPERVISOR": {
            "description": "Support supervisor managing agent queues, SLA recalculations, and emergency rollbacks",
            "permissions": ["tickets:manage", "tickets:reassign", "knowledge:rollback", "audit:read"],
        },
        "AGENT": {
            "description": "Customer support tier agent managing customer conversations and evidence analysis",
            "permissions": ["tickets:view", "tickets:update", "chat:respond", "evidence:verify"],
        },
        "AUDITOR": {
            "description": "Compliance auditor with historical RAG query privileges and decrypt audit access",
            "permissions": ["audit:read", "knowledge:read_historical", "compliance:audit"],
        },
    }

    roles_map = {}
    for name, data in role_definitions.items():
        result = await session.execute(select(Role).where(Role.name == name))
        role = result.scalar_one_or_none()
        if not role:
            role = Role(name=name, description=data["description"], permissions=data["permissions"])
            session.add(role)
            await session.flush()
            logger.info("Created role: %s", name)
        roles_map[name] = role

    return roles_map


async def seed_users(session: AsyncSession, roles_map: Dict[str, Role]) -> Dict[str, User]:
    """Provisions non-production developer/test accounts with securely hashed passwords."""
    user_specs = [
        {
            "key": "admin",
            "email": settings.SEED_ADMIN_EMAIL,
            "password": settings.SEED_ADMIN_PASSWORD,
            "full_name": "Platform Administrator (Dev/Test)",
            "department": "Platform Engineering",
            "skills": ["admin", "system_ops"],
            "max_active_tickets": 10,
            "is_on_call": False,
            "role_name": "ADMIN",
        },
        {
            "key": "supervisor",
            "email": settings.SEED_SUPERVISOR_EMAIL,
            "password": settings.SEED_SUPERVISOR_PASSWORD,
            "full_name": "Operations Supervisor (Dev/Test)",
            "department": "Customer Support Operations",
            "skills": ["escalations", "sla_management", "quality_audit"],
            "max_active_tickets": 8,
            "is_on_call": True,
            "role_name": "SUPERVISOR",
        },
        {
            "key": "agent",
            "email": settings.SEED_AGENT_EMAIL,
            "password": settings.SEED_AGENT_PASSWORD,
            "full_name": "Hardware Specialist Agent (Dev/Test)",
            "department": "Hardware Support",
            "skills": ["hardware", "returns", "warranty", "multilingual_es"],
            "max_active_tickets": 5,
            "is_on_call": True,
            "role_name": "AGENT",
        },
    ]

    users_map = {}
    for spec in user_specs:
        result = await session.execute(select(User).where(User.email == spec["email"]))
        user = result.scalar_one_or_none()
        if not user:
            user = User(
                email=spec["email"],
                hashed_password=hash_password(spec["password"]),
                full_name=spec["full_name"],
                department=spec["department"],
                skills=spec["skills"],
                max_active_tickets=spec["max_active_tickets"],
                is_active=True,
                is_on_call=spec["is_on_call"],
            )
            session.add(user)
            await session.flush()

            # Attach role
            role = roles_map[spec["role_name"]]
            user_role = UserRole(user_id=user.id, role_id=role.id)
            session.add(user_role)
            await session.flush()
            logger.info("Created user: %s with role: %s", spec["email"], spec["role_name"])

        users_map[spec["key"]] = user

    return users_map


async def seed_customer(session: AsyncSession) -> Customer:
    """Provisions a baseline non-production customer profile."""
    external_id = "CUST-DEV-001"
    result = await session.execute(select(Customer).where(Customer.external_id == external_id))
    customer = result.scalar_one_or_none()
    if not customer:
        customer = Customer(
            external_id=external_id,
            email="john.doe@example.com",
            phone="+1-555-0199",
            full_name="John Doe (Test Customer)",
            customer_tier="STANDARD",
            metadata_={"locale": "en_US", "account_status": "VERIFIED"},
        )
        session.add(customer)
        await session.flush()
        logger.info("Created test customer: %s", external_id)
    return customer


async def seed_sla_calendar(session: AsyncSession) -> SLACalendar:
    """Provisions the standard business hours SLA calendar."""
    calendar_name = "US_COMMERCIAL_HOURS"
    result = await session.execute(select(SLACalendar).where(SLACalendar.name == calendar_name))
    calendar = result.scalar_one_or_none()
    if not calendar:
        calendar = SLACalendar(
            name=calendar_name,
            timezone="America/New_York",
            work_start_time=time(9, 0, 0),
            work_end_time=time(17, 0, 0),
            work_days=[1, 2, 3, 4, 5],  # Monday to Friday
            holidays=[
                "2026-01-01",  # New Year's Day
                "2026-05-25",  # Memorial Day
                "2026-07-04",  # Independence Day
                "2026-09-07",  # Labor Day
                "2026-11-26",  # Thanksgiving Day
                "2026-12-25",  # Christmas Day
            ],
        )
        session.add(calendar)
        await session.flush()
        logger.info("Created SLA calendar: %s", calendar_name)
    return calendar


async def seed_database(session: AsyncSession) -> Dict[str, Any]:
    """Coordinates full non-production database seeding."""
    roles = await seed_roles(session)
    users = await seed_users(session, roles)
    customer = await seed_customer(session)
    calendar = await seed_sla_calendar(session)
    await session.commit()
    return {
        "roles": roles,
        "users": users,
        "customer": customer,
        "calendar": calendar,
    }


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    logger.info("Starting controlled database seed...")
    async with AsyncSessionLocal() as session:
        await seed_database(session)
    logger.info("Database seeding completed successfully.")


if __name__ == "__main__":
    asyncio.run(main())

