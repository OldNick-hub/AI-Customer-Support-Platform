"""Pytest Fixtures and Test Database Configuration.

Provisions an isolated in-memory async SQLite database (using aiosqlite) for unit & integration
tests, ensuring zero interaction with production or external databases.
"""

import asyncio
from typing import AsyncGenerator
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.encryption import ApplicationLayerEncryption
from app.db.base import Base
import app.models  # Ensures all models are registered in Base.metadata
from tests.helpers.database import TEST_DB_URL, test_engine, TestSessionLocal

__all__ = ["TEST_DB_URL", "test_engine", "TestSessionLocal"]


@pytest.fixture(scope="session")
def event_loop():
    """Provides an isolated asyncio event loop for async tests."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture(scope="function")
async def init_db():
    """Initializes schema in the isolated test database before each test run."""
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture(scope="function")
async def session(init_db) -> AsyncGenerator[AsyncSession, None]:
    """Yields a transactional async database session isolated to a single test."""
    async with TestSessionLocal() as sess:
        try:
            yield sess
            await sess.commit()
        except Exception:
            await sess.rollback()
            raise
        finally:
            await sess.close()


@pytest.fixture
def encryption_engine() -> ApplicationLayerEncryption:
    """Provides an ApplicationLayerEncryption instance with a known 32-byte test key."""
    test_key = bytes.fromhex("0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef")
    return ApplicationLayerEncryption(key=test_key)

