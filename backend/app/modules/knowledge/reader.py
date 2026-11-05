"""In-Memory Active Vector Index Reader & Synchronization Engine.

Maintains hot-swappable in-memory FAISS vector index pointers for RAG readers and query workers,
implementing the 4-tier synchronization model:
1. Tier 1: Immediate hot-swap upon Redis Pub/Sub reload notification.
2. Tier 2: Periodic background active-version reconciliation loop (healing missed notifications).
3. Tier 3: Worker startup synchronization (boot-time manifest/database loading).
4. Tier 4: Query-time fallback guard and stale-reader replacement.
"""

import asyncio
from datetime import datetime, timezone
import json
import logging
import os
from typing import Any, Callable, Dict, Optional

import faiss
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, settings as default_settings
from app.core.logging import configure_safe_logger
from app.models.knowledge_index import KnowledgeIndex
from app.services.redis_service import RedisService, get_redis_service

logger = configure_safe_logger(logging.getLogger(__name__))


class ActiveIndexReader:
    """Manages in-memory FAISS vector index hot-swapping and multi-tier synchronization."""

    def __init__(
        self,
        settings: Optional[Settings] = None,
        redis_service: Optional[RedisService] = None,
    ) -> None:
        self.settings = settings or default_settings
        self.redis_service = redis_service or get_redis_service()
        self.storage_dir = os.path.abspath(self.settings.FAISS_STORAGE_DIR)
        self.manifest_path = os.path.join(self.storage_dir, "active_index.json")

        self.in_memory_index: Optional[faiss.Index] = None
        self.in_memory_version_tag: Optional[str] = None
        self.in_memory_manifest: Optional[Dict[str, Any]] = None

        self._reconcile_task: Optional[asyncio.Task] = None
        self._pubsub_task: Optional[asyncio.Task] = None
        self._is_running: bool = False

    def read_active_manifest(self) -> Optional[Dict[str, Any]]:
        """Reads and parses active_index.json safely."""
        if not os.path.isfile(self.manifest_path):
            return None
        try:
            with open(self.manifest_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as exc:
            logger.warning("Could not read active_index.json at '%s': %s", self.manifest_path, exc)
            return None

    def load_active_index(self, force: bool = False) -> bool:
        """Loads or hot-swaps the active FAISS index from disk into memory.

        Returns True if a new index was loaded, False if already up to date.
        """
        manifest = self.read_active_manifest()
        if not manifest or not isinstance(manifest, dict):
            logger.warning("Cannot load index: active_index.json is missing or invalid.")
            return False

        active_tag = manifest.get("active_version_tag")
        if not active_tag:
            logger.warning("active_index.json does not specify 'active_version_tag'.")
            return False

        if not force and active_tag == self.in_memory_version_tag and self.in_memory_index is not None:
            return False

        index_file_path = manifest.get("index_file_path")
        if not index_file_path or not os.path.isfile(index_file_path):
            # Fall back to tag directory
            candidate_path = os.path.join(self.storage_dir, active_tag, "index.faiss")
            if os.path.isfile(candidate_path):
                index_file_path = candidate_path
            else:
                logger.error("Active index binary file not found at '%s'.", index_file_path)
                return False

        try:
            loaded_index = faiss.read_index(index_file_path)
        except Exception as exc:
            logger.error("Failed to read FAISS index from '%s': %s", index_file_path, exc)
            return False

        # Atomic pointer swap in memory
        self.in_memory_index = loaded_index
        self.in_memory_version_tag = active_tag
        self.in_memory_manifest = manifest
        logger.info(
            "Hot-swapped in-memory FAISS index to version '%s' (ntotal=%d, d=%d).",
            active_tag,
            loaded_index.ntotal,
            loaded_index.d,
        )
        return True

    async def sync_on_startup(self, session: Optional[AsyncSession] = None) -> bool:
        """Tier 3: Synchronizes active index into memory on worker boot."""
        logger.info("Performing Tier 3 worker startup index synchronization.")
        # Attempt manifest load first
        loaded = self.load_active_index()
        if loaded:
            return True

        # Fallback to querying authoritative database if manifest is absent or empty
        if session:
            try:
                stmt = select(KnowledgeIndex).where(KnowledgeIndex.status == "ACTIVE")
                db_active = (await session.execute(stmt)).scalar_one_or_none()
                if db_active:
                    db_dir = os.path.join(self.storage_dir, db_active.index_version_tag)
                    idx_path = os.path.join(db_dir, "index.faiss")
                    if os.path.isfile(idx_path):
                        self.in_memory_index = faiss.read_index(idx_path)
                        self.in_memory_version_tag = db_active.index_version_tag
                        logger.info(
                            "Tier 3 Startup: Loaded active index '%s' directly from authoritative database record.",
                            db_active.index_version_tag,
                        )
                        return True
            except Exception as exc:
                logger.warning("Database startup query fallback failed: %s", exc)

        return self.in_memory_index is not None

    async def handle_reload_event(self, event_data: Dict[str, Any]) -> bool:
        """Tier 1: Handles immediate Pub/Sub reload notification."""
        new_tag = event_data.get("active_version_tag")
        if not new_tag:
            return False

        if new_tag != self.in_memory_version_tag:
            logger.info("Tier 1: Received reload event for version '%s'. Hot-swapping.", new_tag)
            return self.load_active_index(force=True)
        return False

    async def reconcile_active_version(
        self,
        session: Optional[AsyncSession] = None,
    ) -> bool:
        """Tier 2: Periodic active-version reconciliation to heal missed notifications.

        Detects if in-memory version diverges from Redis cache, manifest, or database.
        Returns True if divergence was detected and healed, False otherwise.
        """
        latest_tag: Optional[str] = None

        # 1. Check Redis fast cache key if available
        try:
            cached_val = await self.redis_service.get("cache:active_index_version", raise_on_error=False)
            if cached_val:
                latest_tag = cached_val
        except Exception:
            pass

        # 2. Check filesystem manifest if cache miss
        if not latest_tag:
            manifest = self.read_active_manifest()
            if manifest:
                latest_tag = manifest.get("active_version_tag")

        # 3. Check database if session provided and still unresolved
        if not latest_tag and session:
            try:
                stmt = select(KnowledgeIndex.index_version_tag).where(KnowledgeIndex.status == "ACTIVE")
                latest_tag = (await session.execute(stmt)).scalar_one_or_none()
            except Exception:
                pass

        if latest_tag and latest_tag != self.in_memory_version_tag:
            logger.warning(
                "Version divergence detected (loaded: %s, latest: %s). Healing stale reader.",
                self.in_memory_version_tag,
                latest_tag,
            )
            return self.load_active_index(force=True)

        return False

    async def run_periodic_reconciliation(
        self,
        interval_seconds: float = 30.0,
        session_provider: Optional[Callable[[], AsyncSession]] = None,
    ) -> None:
        """Runs the Tier 2 periodic reconciliation loop in the background."""
        self._is_running = True
        logger.info("Starting Tier 2 periodic active-version reconciler (interval=%.1fs).", interval_seconds)
        try:
            while self._is_running:
                await asyncio.sleep(interval_seconds)
                session = session_provider() if session_provider else None
                try:
                    await self.reconcile_active_version(session=session)
                except Exception as exc:
                    logger.warning("Error during active-version reconciliation: %s", exc)
                finally:
                    if session:
                        try:
                            await session.close()
                        except Exception:
                            pass
        except asyncio.CancelledError:
            logger.info("Tier 2 periodic active-version reconciler cancelled.")

    def start_background_reconciliation(
        self,
        interval_seconds: float = 30.0,
        session_provider: Optional[Callable[[], AsyncSession]] = None,
    ) -> asyncio.Task:
        """Starts the background reconciliation task."""
        if self._reconcile_task and not self._reconcile_task.done():
            return self._reconcile_task
        self._reconcile_task = asyncio.create_task(
            self.run_periodic_reconciliation(
                interval_seconds=interval_seconds,
                session_provider=session_provider,
            )
        )
        return self._reconcile_task

    def stop(self) -> None:
        """Stops background tasks gracefully."""
        self._is_running = False
        if self._reconcile_task and not self._reconcile_task.done():
            self._reconcile_task.cancel()
            self._reconcile_task = None
        if self._pubsub_task and not self._pubsub_task.done():
            self._pubsub_task.cancel()
            self._pubsub_task = None

    def get_index(self) -> Optional[faiss.Index]:
        """Tier 4: Returns the currently loaded in-memory index; attempts lazy sync if not yet loaded."""
        if self.in_memory_index is None:
            self.load_active_index()
        return self.in_memory_index

    def get_active_version_tag(self) -> Optional[str]:
        """Returns the tag of the currently loaded active index."""
        return self.in_memory_version_tag

