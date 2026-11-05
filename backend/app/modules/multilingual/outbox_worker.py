"""Transactional Outbox Worker & Redis Timeline Deliverer.

Implements:
1. Deterministic UUIDv5 event ID generation with golden vector guarantee.
2. SELECT ... FOR UPDATE SKIP LOCKED batch claiming.
3. Dual-watermark conversation-scoped Redis ZSET Lua timeline application.
4. Gap detection (W_window_contig vs W_max) and authoritative PostgreSQL fallback.
5. Distributed reconciliation locking with owner token CAS release.
6. Exponential backoff, dead-letter transitions, and automated redrive.
7. Outbox retention pruning (7-day DELIVERED only, preserving PENDING/DEAD_LETTER).
"""

from datetime import datetime, timedelta, timezone
import json
import logging
from typing import Any, Dict, List, Optional, Tuple, Union
import uuid

import redis.asyncio as aioredis
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import configure_safe_logger
from app.models.chat import ChatOutboxEvent
from app.models.customer import Conversation, Message

logger = configure_safe_logger(logging.getLogger(__name__))

# Canonical UUIDv5 Namespace Constant for Chat Outbox Events (v2.16.0)
CHAT_OUTBOX_NAMESPACE_V1 = uuid.UUID("e8a56f2e-4b71-4a9c-9c32-84183d8e52a9")


def generate_outbox_event_id(
    conversation_id: Union[uuid.UUID, str], turn_sequence_number: int
) -> uuid.UUID:
    """Generates canonical deterministic UUIDv5 event ID for conversation turn.

    Golden Vector:
    generate_outbox_event_id("12345678-1234-5678-1234-567812345678", 1)
    strictly produces: "5d56372a-7576-5454-856a-602a0895e80c".
    """
    try:
        canon_conv = str(uuid.UUID(str(conversation_id))).lower()
    except (ValueError, TypeError, AttributeError):
        canon_conv = str(uuid.uuid5(CHAT_OUTBOX_NAMESPACE_V1, str(conversation_id))).lower()
    canon_seq = int(turn_sequence_number)
    name_string = f"v1:{canon_conv}:{canon_seq}"
    return uuid.uuid5(CHAT_OUTBOX_NAMESPACE_V1, name_string)


# Dual-Watermark Lua Script for Atomic Timeline Application
LUA_APPLY_TURN_SCRIPT = """
local event_id = ARGV[1]
local seq_num = tonumber(ARGV[2])
local payload = ARGV[3]
local expected_id = ARGV[4]
local ttl = tonumber(ARGV[5])

-- 1. Identity Validation
if event_id ~= expected_id then
    return {0, "IDENTITY_MISMATCH"}
end

-- 2. Idempotency Check
if redis.call('SISMEMBER', KEYS[3], event_id) == 1 then
    redis.call('EXPIRE', KEYS[1], ttl)
    redis.call('EXPIRE', KEYS[2], ttl)
    redis.call('EXPIRE', KEYS[3], ttl)
    redis.call('EXPIRE', KEYS[4], ttl)
    redis.call('EXPIRE', KEYS[5], ttl)
    return {1, "ALREADY_PRESENT"}
end

-- 3. Watermark Existence Check
local raw_w = redis.call('GET', KEYS[4])
if not raw_w then
    return {0, "UNINITIALIZED_WATERMARK_REQUIRED"}
end
local w_max = tonumber(raw_w)
local window_start = math.max(1, w_max - 9)

-- 4. Stale Event Rejection (below window)
if seq_num < window_start then
    redis.call('SADD', KEYS[3], event_id)
    redis.call('EXPIRE', KEYS[3], ttl)
    return {1, "STALE_OUTSIDE_WINDOW"}
end

-- 5. Insert Event into Buffer
redis.call('ZADD', KEYS[1], seq_num, event_id)
redis.call('HSET', KEYS[2], event_id, payload)
redis.call('SADD', KEYS[3], event_id)

-- 6. Update W_max
if seq_num > w_max then
    w_max = seq_num
    window_start = math.max(1, w_max - 9)
    redis.call('SET', KEYS[4], tostring(w_max), 'EX', ttl)
end

-- 7. Recalculate W_window_contig strictly from window_start
local w_contig = window_start - 1
while true do
    local next_score = w_contig + 1
    if next_score > w_max then
        break
    end
    local found = redis.call('ZRANGEBYSCORE', KEYS[1], next_score, next_score)
    if #found > 0 then
        w_contig = next_score
    else
        break
    end
end
redis.call('SET', KEYS[5], tostring(w_contig), 'EX', ttl)

-- 8. Evict Turns Falling Outside Sliding Window [window_start, w_max]
local to_evict = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', window_start - 1)
for _, old_id in ipairs(to_evict) do
    redis.call('HDEL', KEYS[2], old_id)
end
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', window_start - 1)

-- 9. Refresh TTLs
redis.call('EXPIRE', KEYS[1], ttl)
redis.call('EXPIRE', KEYS[2], ttl)
redis.call('EXPIRE', KEYS[3], ttl)
redis.call('EXPIRE', KEYS[4], ttl)
redis.call('EXPIRE', KEYS[5], ttl)

if w_contig < w_max then
    return {1, "APPLIED_WITH_GAP"}
else
    return {1, "APPLIED_CONTIGUOUS"}
end
"""

# Lua Script for Owner-Verified Distributed Lock Release
LUA_RELEASE_LOCK_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
else
    return 0
end
"""

# Lua Script for Atomic Timeline Initialization
LUA_INIT_TIMELINE_SCRIPT = """
local w_auth = tonumber(ARGV[1])
local ttl = tonumber(ARGV[2])
local turns_json = ARGV[3]

local current_w = redis.call('GET', KEYS[4])
if current_w and tonumber(current_w) >= w_auth then
    -- Do not decrease or clobber newer watermark
    return {1, "ALREADY_INITIALIZED_HIGHER"}
end

local window_start = math.max(1, w_auth - 9)
redis.call('SET', KEYS[4], tostring(w_auth), 'EX', ttl)

if turns_json and turns_json ~= "" and turns_json ~= "[]" then
    local cjson = cjson
    local turns = cjson.decode(turns_json)
    for _, t in ipairs(turns) do
        local seq = tonumber(t.sequence_number)
        local eid = t.event_id
        local p = t.payload
        if seq >= window_start and seq <= w_auth then
            redis.call('ZADD', KEYS[1], seq, eid)
            redis.call('HSET', KEYS[2], eid, p)
            redis.call('SADD', KEYS[3], eid)
        end
    end
end

-- Recalculate contiguity
local w_contig = window_start - 1
while true do
    local next_score = w_contig + 1
    if next_score > w_auth then
        break
    end
    local found = redis.call('ZRANGEBYSCORE', KEYS[1], next_score, next_score)
    if #found > 0 then
        w_contig = next_score
    else
        break
    end
end
redis.call('SET', KEYS[5], tostring(w_contig), 'EX', ttl)

redis.call('EXPIRE', KEYS[1], ttl)
redis.call('EXPIRE', KEYS[2], ttl)
redis.call('EXPIRE', KEYS[3], ttl)
redis.call('EXPIRE', KEYS[4], ttl)
redis.call('EXPIRE', KEYS[5], ttl)

return {1, "INITIALIZED"}
"""


class OutboxWorker:
    """Coordinates resilient delivery of chat outbox events to Redis timeline."""

    def __init__(
        self,
        redis_client: Optional[aioredis.Redis] = None,
        ttl_seconds: int = 86400,
        max_attempts: int = 10,
    ) -> None:
        self.redis = redis_client
        self.ttl_seconds = ttl_seconds
        self.max_attempts = max_attempts

    def _get_redis_keys(self, conversation_id: Union[uuid.UUID, str]) -> List[str]:
        cid = str(conversation_id)
        return [
            f"conv:{cid}:timeline",
            f"conv:{cid}:turn_payloads",
            f"conv:{cid}:seen_events",
            f"conv:{cid}:watermark",
            f"conv:{cid}:window_contiguous_watermark",
        ]

    async def acquire_reconciliation_lock(
        self, conversation_id: Union[uuid.UUID, str], owner_token: str, timeout_seconds: int = 5
    ) -> bool:
        """Acquires distributed lock for initializing or reconciling timeline."""
        if not self.redis:
            return True
        lock_key = f"conv:{conversation_id}:reconcile_lock"
        acquired = await self.redis.set(lock_key, owner_token, ex=timeout_seconds, nx=True)
        return bool(acquired)

    async def release_reconciliation_lock(
        self, conversation_id: Union[uuid.UUID, str], owner_token: str
    ) -> bool:
        """Releases distributed lock using owner-verified atomic Lua script."""
        if not self.redis:
            return True
        lock_key = f"conv:{conversation_id}:reconcile_lock"
        res = await self.redis.eval(LUA_RELEASE_LOCK_SCRIPT, 1, lock_key, owner_token)
        return bool(res == 1)

    async def initialize_timeline_from_postgres(
        self, session: AsyncSession, conversation_id: uuid.UUID
    ) -> Tuple[bool, str]:
        """Initializes Redis timeline buffer from PostgreSQL authoritative turn_count."""
        if not self.redis:
            return True, "REDIS_DISABLED"

        conv_res = await session.execute(
            select(Conversation.turn_count).where(Conversation.id == conversation_id)
        )
        row = conv_res.scalar_one_or_none()
        w_auth = row if row is not None else 0

        # Query turns in window [max(1, w_auth - 9), w_auth]
        window_start = max(1, w_auth - 9)
        outbox_res = await session.execute(
            select(ChatOutboxEvent)
            .where(
                ChatOutboxEvent.conversation_id == conversation_id,
                ChatOutboxEvent.sequence_number >= window_start,
                ChatOutboxEvent.sequence_number <= w_auth,
            )
            .order_by(ChatOutboxEvent.sequence_number.asc())
        )
        events = outbox_res.scalars().all()

        turns_data = []
        for ev in events:
            turns_data.append(
                {
                    "event_id": str(ev.id),
                    "sequence_number": ev.sequence_number,
                    "payload": json.dumps(ev.payload) if isinstance(ev.payload, (dict, list)) else str(ev.payload),
                }
            )

        keys = self._get_redis_keys(conversation_id)
        res = await self.redis.eval(
            LUA_INIT_TIMELINE_SCRIPT,
            5,
            *keys,
            str(w_auth),
            str(self.ttl_seconds),
            json.dumps(turns_data),
        )
        return bool(res[0] == 1), str(res[1])

    async def deliver_single_event(
        self, session: AsyncSession, event: ChatOutboxEvent
    ) -> Tuple[bool, str]:
        """Delivers a single outbox event to Redis using atomic Lua script."""
        if not self.redis:
            event.status = "DELIVERED"
            event.delivered_at = datetime.now(timezone.utc)
            return True, "REDIS_DISABLED"

        expected_id = str(generate_outbox_event_id(event.conversation_id, event.sequence_number))
        event_id = str(event.id)
        payload_str = json.dumps(event.payload) if isinstance(event.payload, (dict, list)) else str(event.payload)
        keys = self._get_redis_keys(event.conversation_id)

        try:
            res = await self.redis.eval(
                LUA_APPLY_TURN_SCRIPT,
                5,
                *keys,
                event_id,
                str(event.sequence_number),
                payload_str,
                expected_id,
                str(self.ttl_seconds),
            )

            status_code, reason = int(res[0]), str(res[1])

            if status_code == 0 and reason == "UNINITIALIZED_WATERMARK_REQUIRED":
                # Initialize timeline from PostgreSQL and retry once
                await self.initialize_timeline_from_postgres(session, event.conversation_id)
                res = await self.redis.eval(
                    LUA_APPLY_TURN_SCRIPT,
                    5,
                    *keys,
                    event_id,
                    str(event.sequence_number),
                    payload_str,
                    expected_id,
                    str(self.ttl_seconds),
                )
                status_code, reason = int(res[0]), str(res[1])

            if status_code == 1:
                event.status = "DELIVERED"
                event.delivered_at = datetime.now(timezone.utc)
                event.last_error = None
                return True, reason
            else:
                event.last_error = f"LUA_REJECTION:{reason}"[:128]
                return False, reason

        except Exception as exc:
            event.attempt_count += 1
            # Exponential backoff: 1s, 2s, 4s, 8s, up to 60s
            delay = min(60, 2 ** (min(event.attempt_count, 6) - 1))
            event.next_retry_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
            event.last_error = f"{exc.__class__.__name__}:{str(exc)[:100]}"[:128]

            if event.attempt_count >= self.max_attempts:
                event.status = "DEAD_LETTER"
                logger.critical(
                    "Outbox event %s transitioned to DEAD_LETTER after %d attempts: %s",
                    event.id,
                    event.attempt_count,
                    event.last_error,
                )
            else:
                logger.warning(
                    "Outbox delivery failed for event %s (attempt %d): %s",
                    event.id,
                    event.attempt_count,
                    event.last_error,
                )
            return False, str(exc)

    async def claim_and_process_pending_events(
        self, session: AsyncSession, batch_size: int = 10
    ) -> List[Tuple[str, bool, str]]:
        """Claims pending outbox events using FOR UPDATE SKIP LOCKED and delivers them."""
        now = datetime.now(timezone.utc)
        stmt = (
            select(ChatOutboxEvent)
            .where(
                ChatOutboxEvent.status.in_(["PENDING", "IN_FLIGHT"]),
                ChatOutboxEvent.next_retry_at <= now,
            )
            .order_by(ChatOutboxEvent.created_at.asc())
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        )
        result = await session.execute(stmt)
        events = result.scalars().all()

        results = []
        for event in events:
            event.status = "IN_FLIGHT"
            success, reason = await self.deliver_single_event(session, event)
            results.append((str(event.id), success, reason))

        await session.commit()
        return results

    async def redrive_dead_letters(self, session: AsyncSession, limit: int = 50) -> int:
        """Resets DEAD_LETTER events to PENDING with next_retry_at = NOW()."""
        stmt = (
            update(ChatOutboxEvent)
            .where(ChatOutboxEvent.status == "DEAD_LETTER")
            .values(
                status="PENDING",
                attempt_count=0,
                next_retry_at=datetime.now(timezone.utc),
            )
            .execution_options(synchronize_session=False)
        )
        res = await session.execute(stmt)
        await session.commit()
        return int(res.rowcount)

    async def prune_delivered_events(
        self, session: AsyncSession, retention_days: int = 7
    ) -> int:
        """Prunes DELIVERED outbox events older than retention_days.

        Strictly preserves PENDING, IN_FLIGHT, and DEAD_LETTER events.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
        stmt = (
            delete(ChatOutboxEvent)
            .where(
                ChatOutboxEvent.status == "DELIVERED",
                ChatOutboxEvent.delivered_at <= cutoff,
            )
            .execution_options(synchronize_session=False)
        )
        res = await session.execute(stmt)
        await session.commit()
        return int(res.rowcount)

    async def check_timeline_continuity(
        self, conversation_id: Union[uuid.UUID, str]
    ) -> Tuple[bool, int, int]:
        """Checks Redis timeline continuity. Returns (is_complete, w_window_contig, w_max)."""
        if not self.redis:
            return True, 0, 0
        keys = self._get_redis_keys(conversation_id)
        raw_w_max = await self.redis.get(keys[3])
        raw_w_contig = await self.redis.get(keys[4])

        if raw_w_max is None or raw_w_contig is None:
            return False, 0, 0

        w_max = int(raw_w_max)
        w_contig = int(raw_w_contig)
        return (w_contig >= w_max), w_contig, w_max

