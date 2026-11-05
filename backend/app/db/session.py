"""Database Connection, Async Engine, and Session Dependency.

Configures SQLAlchemy 2.0 async engine and sessionmaker with connection pooling.
Provides the reusable get_db dependency for FastAPI endpoints.
"""

from typing import AsyncGenerator
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from app.core.config import settings

# Async Engine for high-performance FastAPI async operations
# If using SQLite for testing (sqlite+aiosqlite), pool_size and max_overflow are omitted.
is_sqlite = settings.async_database_url.startswith("sqlite")

engine_kwargs = {
    "echo": settings.DEBUG and settings.ENVIRONMENT == "development",
    "future": True,
}

if not is_sqlite:
    engine_kwargs.update({
        "pool_size": settings.DB_POOL_SIZE,
        "max_overflow": settings.DB_MAX_OVERFLOW,
        "pool_timeout": settings.DB_POOL_TIMEOUT,
        "pool_recycle": settings.DB_POOL_RECYCLE,
    })

async_engine = create_async_engine(settings.async_database_url, **engine_kwargs)

# Async Session Factory
AsyncSessionLocal = async_sessionmaker(
    bind=async_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)

# Optional synchronous engine for Alembic and offline migration tasks
sync_engine = None
if not settings.sync_database_url.startswith("sqlite"):
    sync_engine = create_engine(
        settings.sync_database_url,
        pool_size=5,
        max_overflow=5,
        pool_recycle=1800,
        future=True,
    )


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields an asynchronous database session.

    Ensures proper rollback on unhandled exceptions and deterministic session closing.
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

