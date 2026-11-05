"""Shared test database engine and session factory infrastructure.

Provisions an isolated in-memory async SQLite database (using aiosqlite) for unit & integration
tests, ensuring zero interaction with production or external databases.
"""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.base import Base
import app.models  # Ensures all models are registered in Base.metadata

# Test database URL - strictly isolated in-memory database
TEST_DB_URL = "sqlite+aiosqlite:///:memory:"

test_engine = create_async_engine(TEST_DB_URL, echo=False)
TestSessionLocal = async_sessionmaker(
    bind=test_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)

