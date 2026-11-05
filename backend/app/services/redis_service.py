"""Async Redis Infrastructure Service.

Provides managed connection pooling, TTL-based caching, key namespacing, Pub/Sub channels,
health checks, graceful degradation, and standardized exception mapping for the platform.
"""

import logging
from typing import Any, AsyncIterator, Dict, Optional, Tuple
import redis.asyncio as aioredis
from redis.exceptions import ConnectionError as RedisConnErr
from redis.exceptions import RedisError as RawRedisError
from redis.exceptions import TimeoutError as RedisTimeoutErr

from app.core.config import Settings, settings as default_settings
from app.core.exceptions import RedisConnectionError, RedisTimeoutError
from app.core.logging import configure_safe_logger

logger = configure_safe_logger(logging.getLogger(__name__))


class RedisService:
    """Async Redis service with connection pooling and graceful degradation."""

    def __init__(
        self,
        settings: Optional[Settings] = None,
        namespace: str = "app",
    ) -> None:
        self.settings = settings or default_settings
        self.namespace = namespace
        self.pool: Optional[aioredis.ConnectionPool] = None
        self.client: Optional[aioredis.Redis] = None
        self._is_connected: bool = False

    def _prefixed_key(self, key: str) -> str:
        """Applies namespace prefix if not already present."""
        if not self.namespace:
            return key
        prefix = f"{self.namespace}:"
        if key.startswith(prefix):
            return key
        return f"{prefix}{key}"

    async def connect(self) -> None:
        """Initializes connection pool and client."""
        if self.client is not None:
            return

        try:
            self.pool = aioredis.ConnectionPool.from_url(
                self.settings.redis_url,
                max_connections=self.settings.REDIS_MAX_CONNECTIONS,
                socket_timeout=self.settings.REDIS_SOCKET_TIMEOUT,
                socket_connect_timeout=self.settings.REDIS_CONNECT_TIMEOUT,
                decode_responses=True,
            )
            self.client = aioredis.Redis(connection_pool=self.pool)
            self._is_connected = True
            logger.info("Redis connection pool initialized for host: %s", self.settings.REDIS_HOST)
        except Exception as exc:
            self._is_connected = False
            logger.warning("Failed to initialize Redis pool: %s", str(exc))
            raise RedisConnectionError(
                message=f"Could not connect to Redis at {self.settings.REDIS_HOST}:{self.settings.REDIS_PORT}",
                host=self.settings.REDIS_HOST,
                port=self.settings.REDIS_PORT,
            ) from exc

    async def disconnect(self) -> None:
        """Gracefully closes client and connection pool."""
        if self.client:
            await self.client.aclose()
            self.client = None
        if self.pool:
            await self.pool.disconnect()
            self.pool = None
        self._is_connected = False
        logger.info("Redis connection pool closed.")

    async def ping(self) -> bool:
        """Executes a health check PING against Redis.

        Raises RedisConnectionError or RedisTimeoutError on failure.
        """
        if self.client is None:
            await self.connect()

        try:
            assert self.client is not None
            res = await self.client.ping()
            return bool(res)
        except RedisTimeoutErr as exc:
            logger.warning("Redis ping timed out: %s", str(exc))
            raise RedisTimeoutError("Redis ping operation timed out.") from exc
        except (RedisConnErr, RawRedisError, OSError) as exc:
            logger.warning("Redis ping failed: %s", str(exc))
            raise RedisConnectionError(
                message=f"Redis connection failed: {exc}",
                host=self.settings.REDIS_HOST,
                port=self.settings.REDIS_PORT,
            ) from exc

    async def is_healthy(self) -> bool:
        """Returns True if Redis is reachable, False otherwise (no exception raised)."""
        try:
            return await self.ping()
        except (RedisConnectionError, RedisTimeoutError, Exception):
            return False

    async def get(
        self,
        key: str,
        default: Optional[str] = None,
        raise_on_error: bool = False,
    ) -> Optional[str]:
        """Retrieves a string value from cache.

        Distinguishes between a genuine cache miss (returns default/None when Redis is available)
        and an infrastructure failure (raises or degrades gracefully based on raise_on_error).
        """
        if self.client is None:
            try:
                await self.connect()
            except Exception as exc:
                if raise_on_error:
                    raise RedisConnectionError(f"Redis unavailable: {exc}") from exc
                return default

        k = self._prefixed_key(key)
        try:
            assert self.client is not None
            val = await self.client.get(k)
            if val is None:
                # Genuine cache miss
                return default
            return val
        except RedisTimeoutErr as exc:
            logger.warning("Redis GET timeout for key '%s': %s", k, str(exc))
            if raise_on_error:
                raise RedisTimeoutError(f"Redis GET timed out for key '{key}'.") from exc
            return default
        except (RedisConnErr, RawRedisError, OSError) as exc:
            logger.warning("Redis GET failed for key '%s': %s", k, str(exc))
            if raise_on_error:
                raise RedisConnectionError(f"Redis GET failed for key '{key}'.") from exc
            return default

    async def get_with_status(self, key: str) -> Tuple[Optional[str], bool, bool]:
        """Returns (value, hit, available) for explicit cache audit and telemetry.

        - hit: True if key exists in cache.
        - available: True if Redis responded successfully.
        """
        if self.client is None:
            try:
                await self.connect()
            except Exception:
                return (None, False, False)

        k = self._prefixed_key(key)
        try:
            assert self.client is not None
            val = await self.client.get(k)
            if val is None:
                return (None, False, True)
            return (val, True, True)
        except Exception:
            return (None, False, False)

    async def set(
        self,
        key: str,
        value: str,
        ttl_seconds: Optional[int] = None,
        raise_on_error: bool = False,
    ) -> bool:
        """Stores a value in cache with optional TTL.

        Never returns True on failure. Returns False or raises on error.
        """
        if self.client is None:
            try:
                await self.connect()
            except Exception as exc:
                if raise_on_error:
                    raise RedisConnectionError(f"Redis unavailable: {exc}") from exc
                return False

        k = self._prefixed_key(key)
        try:
            assert self.client is not None
            res = await self.client.set(k, value, ex=ttl_seconds)
            return bool(res)
        except RedisTimeoutErr as exc:
            logger.warning("Redis SET timeout for key '%s': %s", k, str(exc))
            if raise_on_error:
                raise RedisTimeoutError(f"Redis SET timed out for key '{key}'.") from exc
            return False
        except (RedisConnErr, RawRedisError, OSError) as exc:
            logger.warning("Redis SET failed for key '%s': %s", k, str(exc))
            if raise_on_error:
                raise RedisConnectionError(f"Redis SET failed for key '{key}'.") from exc
            return False

    async def delete(self, key: str, raise_on_error: bool = False) -> bool:
        """Deletes a key from cache. Returns True if key was deleted, False if not or error."""
        if self.client is None:
            try:
                await self.connect()
            except Exception as exc:
                if raise_on_error:
                    raise RedisConnectionError(f"Redis unavailable: {exc}") from exc
                return False

        k = self._prefixed_key(key)
        try:
            assert self.client is not None
            count = await self.client.delete(k)
            return bool(count > 0)
        except RedisTimeoutErr as exc:
            logger.warning("Redis DELETE timeout for key '%s': %s", k, str(exc))
            if raise_on_error:
                raise RedisTimeoutError(f"Redis DELETE timed out for key '{key}'.") from exc
            return False
        except (RedisConnErr, RawRedisError, OSError) as exc:
            logger.warning("Redis DELETE failed for key '%s': %s", k, str(exc))
            if raise_on_error:
                raise RedisConnectionError(f"Redis DELETE failed for key '{key}'.") from exc
            return False

    async def exists(self, key: str, raise_on_error: bool = False) -> bool:
        """Returns True if key exists in cache, False otherwise."""
        if self.client is None:
            try:
                await self.connect()
            except Exception as exc:
                if raise_on_error:
                    raise RedisConnectionError(f"Redis unavailable: {exc}") from exc
                return False

        k = self._prefixed_key(key)
        try:
            assert self.client is not None
            res = await self.client.exists(k)
            return bool(res > 0)
        except (RedisConnErr, RawRedisError, OSError) as exc:
            logger.warning("Redis EXISTS failed for key '%s': %s", k, str(exc))
            if raise_on_error:
                raise RedisConnectionError(f"Redis EXISTS failed for key '{key}'.") from exc
            return False

    async def publish(self, channel: str, message: str, raise_on_error: bool = False) -> int:
        """Publishes a message to a Redis Pub/Sub channel. Returns number of subscribers."""
        if self.client is None:
            try:
                await self.connect()
            except Exception as exc:
                if raise_on_error:
                    raise RedisConnectionError(f"Redis unavailable: {exc}") from exc
                return 0

        try:
            assert self.client is not None
            subscribers = await self.client.publish(channel, message)
            return int(subscribers)
        except (RedisConnErr, RawRedisError, OSError) as exc:
            logger.warning("Redis PUBLISH failed for channel '%s': %s", channel, str(exc))
            if raise_on_error:
                raise RedisConnectionError(f"Redis PUBLISH failed for channel '{channel}'.") from exc
            return 0

    async def get_pubsub(self) -> aioredis.client.PubSub:
        """Returns a PubSub client instance for subscribing to channels."""
        if self.client is None:
            await self.connect()
        assert self.client is not None
        return self.client.pubsub()

    async def __aenter__(self) -> "RedisService":
        await self.connect()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.disconnect()


_default_redis_service: Optional[RedisService] = None


def get_redis_service() -> RedisService:
    """Returns a singleton instance of RedisService."""
    global _default_redis_service
    if _default_redis_service is None:
        _default_redis_service = RedisService()
    return _default_redis_service

