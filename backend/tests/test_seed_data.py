"""Test Controlled Database Seed Script and Idempotency."""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import verify_password
from app.db.seed import seed_database
from app.models.auth import Role, User
from app.models.customer import Customer
from app.models.ticket import SLACalendar


@pytest.mark.asyncio
async def test_seed_database_execution_and_idempotency(session: AsyncSession):
    """Verifies that seed_database provisions all non-production fixtures idempotently."""
    # Run seed 1
    seed_result = await seed_database(session)

    # 1. Verify Roles
    roles = (await session.execute(select(Role))).scalars().all()
    assert len(roles) == 4
    role_names = {r.name for r in roles}
    assert role_names == {"ADMIN", "SUPERVISOR", "AGENT", "AUDITOR"}

    # 2. Verify Users & Password Hashing
    users = (await session.execute(select(User))).scalars().all()
    assert len(users) == 3
    user_emails = {u.email for u in users}
    assert settings.SEED_ADMIN_EMAIL in user_emails
    assert settings.SEED_SUPERVISOR_EMAIL in user_emails
    assert settings.SEED_AGENT_EMAIL in user_emails

    admin_user = next(u for u in users if u.email == settings.SEED_ADMIN_EMAIL)
    assert verify_password(settings.SEED_ADMIN_PASSWORD, admin_user.hashed_password) is True
    assert verify_password("WrongPassword!", admin_user.hashed_password) is False

    # 3. Verify Customer
    customer = (await session.execute(select(Customer))).scalar_one()
    assert customer.external_id == "CUST-DEV-001"
    assert customer.customer_tier == "STANDARD"

    # 4. Verify SLA Calendar
    calendar = (await session.execute(select(SLACalendar))).scalar_one()
    assert calendar.name == "US_COMMERCIAL_HOURS"
    assert len(calendar.holidays) >= 6

    # 5. Idempotency test: Re-run seed_database
    re_seed_result = await seed_database(session)
    users_after = (await session.execute(select(User))).scalars().all()
    assert len(users_after) == 3  # No duplicate users created

