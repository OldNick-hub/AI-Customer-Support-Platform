"""Test Database Connectivity and Session Isolation."""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
async def test_database_connection_ping(session: AsyncSession):
    """Verifies that the database engine can execute queries and return scalar results."""
    result = await session.execute(text("SELECT 1"))
    scalar_val = result.scalar()
    assert scalar_val == 1


@pytest.mark.asyncio
async def test_session_transaction_rollback(session: AsyncSession):
    """Verifies that uncommitted transactions roll back cleanly."""
    await session.execute(text("SELECT 1"))
    await session.rollback()
    # Ensure session remains active and operational after rollback
    result = await session.execute(text("SELECT 2"))
    assert result.scalar() == 2

