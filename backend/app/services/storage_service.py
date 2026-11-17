"""Zero-Cost Unified Storage Service and Provider Abstraction Layer.

Provides:
1. BaseStorageAdapter interface establishing the storage contract.
2. LocalStorageAdapter for offline development and testing.
3. PostgresByteaStorageAdapter for zero-cost staging persistence (PostgreSQL BYTEA).
4. Authenticated AES-256-GCM encryption helpers with tenant:key bound associated data.
5. Strict tenant isolation, path traversal defense, file size (15 MB), and tenant quota (400 MB) enforcement.
"""

from abc import ABC, abstractmethod
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Tuple
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, settings as default_settings
from app.core.encryption import ApplicationLayerEncryption, get_encryption_engine
from app.core.exceptions import (
    StorageConfigurationError,
    StorageError,
    StorageIntegrityError,
    StorageNotFoundError,
    StoragePayloadTooLargeError,
    StorageQuotaExceededError,
    StorageSecurityError,
)
from app.core.logging import configure_safe_logger
from app.db.session import AsyncSessionLocal
from app.models.storage import StorageObject

logger = configure_safe_logger(logging.getLogger(__name__))


# =============================================================================
# Data Structures & Validation
# =============================================================================

@dataclass
class StorageObjectMetadata:
    """Standardized metadata representation for an object in storage."""

    tenant_id: str
    key: str
    file_size_bytes: int
    sha256_hash: str
    content_type: str = "application/octet-stream"
    metadata_json: Dict[str, Any] = field(default_factory=dict)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    def to_dict(self) -> Dict[str, Any]:
        """Converts metadata to a dictionary with ISO-formatted timestamps."""
        data = asdict(self)
        if self.created_at:
            data["created_at"] = self.created_at.isoformat()
        if self.updated_at:
            data["updated_at"] = self.updated_at.isoformat()
        return data


def validate_storage_key(key: str) -> str:
    """Validates that a storage key does not violate path boundaries or security rules."""
    if not key or not isinstance(key, str) or not key.strip():
        raise StorageSecurityError("Storage key must be a non-empty string.")
    if "\0" in key:
        raise StorageSecurityError("Storage key contains invalid null bytes.")

    normalized = key.replace("\\", "/")
    if normalized.startswith("/") or (len(key) > 1 and key[1] == ":"):
        raise StorageSecurityError(f"Storage key '{key}' cannot be an absolute path.")

    parts = normalized.split("/")
    if any(p == ".." for p in parts):
        raise StorageSecurityError(f"Storage key '{key}' contains directory traversal sequences ('..').")
    if any(p == "" for p in parts):
        raise StorageSecurityError(f"Storage key '{key}' contains empty or trailing path segments.")

    return normalized


def validate_tenant_id(tenant_id: str) -> str:
    """Validates tenant identifier for safe path and database indexing."""
    if not tenant_id or not isinstance(tenant_id, str):
        raise StorageSecurityError("Tenant ID must be a non-empty string.")
    cleaned = tenant_id.strip()
    if not cleaned or "/" in cleaned or "\\" in cleaned or ".." in cleaned or "\0" in cleaned:
        raise StorageSecurityError(f"Invalid tenant ID '{tenant_id}'.")
    return cleaned


# =============================================================================
# Abstract Base Adapter
# =============================================================================

class BaseStorageAdapter(ABC):
    """Abstract base class establishing the storage contract across all providers."""

    provider_name: str = "base"

    @abstractmethod
    async def put_object(
        self,
        tenant_id: str,
        key: str,
        payload: bytes,
        content_type: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        session: Optional[AsyncSession] = None,
    ) -> StorageObjectMetadata:
        """Stores a binary object payload under tenant isolation."""
        ...

    @abstractmethod
    async def get_object(
        self,
        tenant_id: str,
        key: str,
        session: Optional[AsyncSession] = None,
    ) -> Tuple[bytes, StorageObjectMetadata]:
        """Retrieves binary object payload and metadata with checksum verification."""
        ...

    @abstractmethod
    async def head_object(
        self,
        tenant_id: str,
        key: str,
        session: Optional[AsyncSession] = None,
    ) -> StorageObjectMetadata:
        """Retrieves object metadata without reading full payload into memory."""
        ...

    @abstractmethod
    async def delete_object(
        self,
        tenant_id: str,
        key: str,
        session: Optional[AsyncSession] = None,
    ) -> bool:
        """Deletes object idempotently. Returns True if deleted, False if not found."""
        ...

    @abstractmethod
    async def object_exists(
        self,
        tenant_id: str,
        key: str,
        session: Optional[AsyncSession] = None,
    ) -> bool:
        """Checks if object exists under tenant isolation."""
        ...

    @abstractmethod
    async def list_objects(
        self,
        tenant_id: str,
        prefix: Optional[str] = None,
        limit: int = 100,
        session: Optional[AsyncSession] = None,
    ) -> List[StorageObjectMetadata]:
        """Lists metadata of objects matching prefix up to limit under tenant isolation."""
        ...

    @abstractmethod
    async def get_tenant_usage_bytes(
        self,
        tenant_id: str,
        session: Optional[AsyncSession] = None,
    ) -> int:
        """Returns total stored payload bytes for the specified tenant."""
        ...


# =============================================================================
# Local Filesystem Storage Adapter
# =============================================================================

class LocalStorageAdapter(BaseStorageAdapter):
    """Storage adapter backed by local filesystem with atomic writes and sidecar metadata."""

    provider_name: str = "local"

    def __init__(
        self,
        root_dir: Optional[str] = None,
        max_file_size_bytes: Optional[int] = None,
        max_quota_bytes: Optional[int] = None,
    ) -> None:
        raw_root = root_dir or getattr(default_settings, "STORAGE_LOCAL_ROOT", "./storage/local")
        self.root_path = Path(raw_root).resolve()
        self.root_path.mkdir(parents=True, exist_ok=True)
        self.max_file_size_bytes = (
            max_file_size_bytes or default_settings.STORAGE_MAX_FILE_SIZE_BYTES
        )
        self.max_quota_bytes = (
            max_quota_bytes or default_settings.STORAGE_MAX_QUOTA_BYTES
        )

    def _resolve_paths(self, tenant_id: str, key: str) -> Tuple[Path, Path]:
        """Resolves target object and metadata sidecar paths, strictly enforcing containment."""
        safe_tenant = validate_tenant_id(tenant_id)
        safe_key = validate_storage_key(key)

        tenant_dir = (self.root_path / safe_tenant).resolve()
        if not tenant_dir.is_relative_to(self.root_path):
            raise StorageSecurityError(
                f"Tenant directory '{tenant_dir}' escapes storage root '{self.root_path}'."
            )

        target_path = (tenant_dir / safe_key).resolve()
        if not target_path.is_relative_to(tenant_dir):
            raise StorageSecurityError(
                f"Target path '{target_path}' escapes tenant directory '{tenant_dir}'."
            )

        meta_path = Path(str(target_path) + ".meta.json")
        return target_path, meta_path

    async def put_object(
        self,
        tenant_id: str,
        key: str,
        payload: bytes,
        content_type: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        session: Optional[AsyncSession] = None,
    ) -> StorageObjectMetadata:
        if not isinstance(payload, (bytes, bytearray)):
            raise TypeError(f"Payload must be bytes, got {type(payload)}")

        payload_bytes = bytes(payload)
        payload_len = len(payload_bytes)

        if payload_len > self.max_file_size_bytes:
            raise StoragePayloadTooLargeError(
                f"Payload size ({payload_len} bytes) exceeds limit of {self.max_file_size_bytes} bytes."
            )

        target_path, meta_path = self._resolve_paths(tenant_id, key)

        existing_size = target_path.stat().st_size if target_path.is_file() else 0
        delta = payload_len - existing_size

        if delta > 0:
            current_usage = await self.get_tenant_usage_bytes(tenant_id)
            if current_usage + delta > self.max_quota_bytes:
                raise StorageQuotaExceededError(
                    f"Operation exceeds tenant quota of {self.max_quota_bytes} bytes "
                    f"(current: {current_usage} bytes, delta: {delta} bytes)."
                )

        target_path.parent.mkdir(parents=True, exist_ok=True)

        # Atomic write to temporary file in same directory, then rename
        tmp_payload = target_path.parent / f"{target_path.name}.tmp.{uuid.uuid4().hex}"
        try:
            tmp_payload.write_bytes(payload_bytes)
            os.replace(tmp_payload, target_path)
        finally:
            if tmp_payload.exists():
                try:
                    tmp_payload.unlink()
                except OSError:
                    pass

        sha256 = hashlib.sha256(payload_bytes).hexdigest()
        now = datetime.now(timezone.utc)

        # Preserve created_at if sidecar exists
        created_at = now
        if meta_path.is_file():
            try:
                old_meta = json.loads(meta_path.read_text(encoding="utf-8"))
                if "created_at" in old_meta:
                    created_at = datetime.fromisoformat(old_meta["created_at"])
            except Exception:
                pass

        obj_meta = StorageObjectMetadata(
            tenant_id=tenant_id,
            key=key,
            file_size_bytes=payload_len,
            sha256_hash=sha256,
            content_type=content_type or "application/octet-stream",
            metadata_json=metadata or {},
            created_at=created_at,
            updated_at=now,
        )

        tmp_meta = meta_path.parent / f"{meta_path.name}.tmp.{uuid.uuid4().hex}"
        try:
            tmp_meta.write_text(json.dumps(obj_meta.to_dict()), encoding="utf-8")
            os.replace(tmp_meta, meta_path)
        finally:
            if tmp_meta.exists():
                try:
                    tmp_meta.unlink()
                except OSError:
                    pass

        return obj_meta

    async def get_object(
        self,
        tenant_id: str,
        key: str,
        session: Optional[AsyncSession] = None,
    ) -> Tuple[bytes, StorageObjectMetadata]:
        target_path, meta_path = self._resolve_paths(tenant_id, key)

        if not target_path.is_file():
            raise StorageNotFoundError(
                f"Storage object '{key}' not found for tenant '{tenant_id}'."
            )

        payload = target_path.read_bytes()
        actual_hash = hashlib.sha256(payload).hexdigest()

        obj_meta = await self.head_object(tenant_id, key, session=session)
        if obj_meta.sha256_hash != actual_hash:
            raise StorageIntegrityError(
                f"Checksum mismatch for object '{key}': expected {obj_meta.sha256_hash}, calculated {actual_hash}."
            )

        return payload, obj_meta

    async def head_object(
        self,
        tenant_id: str,
        key: str,
        session: Optional[AsyncSession] = None,
    ) -> StorageObjectMetadata:
        target_path, meta_path = self._resolve_paths(tenant_id, key)

        if not target_path.is_file():
            raise StorageNotFoundError(
                f"Storage object '{key}' not found for tenant '{tenant_id}'."
            )

        if meta_path.is_file():
            try:
                raw_json = json.loads(meta_path.read_text(encoding="utf-8"))
                created_at = (
                    datetime.fromisoformat(raw_json["created_at"])
                    if "created_at" in raw_json
                    else None
                )
                updated_at = (
                    datetime.fromisoformat(raw_json["updated_at"])
                    if "updated_at" in raw_json
                    else None
                )
                return StorageObjectMetadata(
                    tenant_id=raw_json.get("tenant_id", tenant_id),
                    key=raw_json.get("key", key),
                    file_size_bytes=raw_json.get("file_size_bytes", target_path.stat().st_size),
                    sha256_hash=raw_json.get("sha256_hash", ""),
                    content_type=raw_json.get("content_type", "application/octet-stream"),
                    metadata_json=raw_json.get("metadata_json", {}),
                    created_at=created_at,
                    updated_at=updated_at,
                )
            except Exception as exc:
                logger.warning("Failed to parse metadata sidecar for %s: %s", key, exc)

        # Fallback if metadata sidecar is missing
        st = target_path.stat()
        file_bytes = target_path.read_bytes()
        sha256 = hashlib.sha256(file_bytes).hexdigest()
        dt = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
        return StorageObjectMetadata(
            tenant_id=tenant_id,
            key=key,
            file_size_bytes=st.st_size,
            sha256_hash=sha256,
            content_type="application/octet-stream",
            metadata_json={},
            created_at=dt,
            updated_at=dt,
        )

    async def delete_object(
        self,
        tenant_id: str,
        key: str,
        session: Optional[AsyncSession] = None,
    ) -> bool:
        target_path, meta_path = self._resolve_paths(tenant_id, key)

        deleted = False
        if target_path.exists():
            target_path.unlink()
            deleted = True
        if meta_path.exists():
            meta_path.unlink()

        return deleted

    async def object_exists(
        self,
        tenant_id: str,
        key: str,
        session: Optional[AsyncSession] = None,
    ) -> bool:
        target_path, _ = self._resolve_paths(tenant_id, key)
        return target_path.is_file()

    async def list_objects(
        self,
        tenant_id: str,
        prefix: Optional[str] = None,
        limit: int = 100,
        session: Optional[AsyncSession] = None,
    ) -> List[StorageObjectMetadata]:
        safe_tenant = validate_tenant_id(tenant_id)
        tenant_dir = (self.root_path / safe_tenant).resolve()
        if not tenant_dir.exists() or not tenant_dir.is_dir():
            return []

        results: List[StorageObjectMetadata] = []
        prefix_norm = prefix.replace("\\", "/") if prefix else None

        for path in tenant_dir.rglob("*"):
            if not path.is_file():
                continue
            if path.name.endswith(".meta.json") or ".tmp." in path.name:
                continue

            rel_key = path.relative_to(tenant_dir).as_posix()
            if prefix_norm and not rel_key.startswith(prefix_norm):
                continue

            try:
                meta = await self.head_object(tenant_id, rel_key, session=session)
                results.append(meta)
                if len(results) >= limit:
                    break
            except Exception:
                continue

        results.sort(key=lambda m: m.created_at or datetime.min, reverse=True)
        return results

    async def get_tenant_usage_bytes(
        self,
        tenant_id: str,
        session: Optional[AsyncSession] = None,
    ) -> int:
        safe_tenant = validate_tenant_id(tenant_id)
        tenant_dir = (self.root_path / safe_tenant).resolve()
        if not tenant_dir.exists() or not tenant_dir.is_dir():
            return 0

        total = 0
        for path in tenant_dir.rglob("*"):
            if path.is_file() and not path.name.endswith(".meta.json") and ".tmp." not in path.name:
                total += path.stat().st_size
        return total


# =============================================================================
# PostgreSQL BYTEA Storage Adapter (Zero-Cost Staging)
# =============================================================================

class PostgresByteaStorageAdapter(BaseStorageAdapter):
    """Storage adapter backed by PostgreSQL BYTEA for zero-cost staging storage."""

    provider_name: str = "postgres"

    def __init__(
        self,
        session_factory: Optional[Callable[[], AsyncSession]] = None,
        max_file_size_bytes: Optional[int] = None,
        max_quota_bytes: Optional[int] = None,
    ) -> None:
        self.session_factory = session_factory or AsyncSessionLocal
        self.max_file_size_bytes = (
            max_file_size_bytes or default_settings.STORAGE_MAX_FILE_SIZE_BYTES
        )
        self.max_quota_bytes = (
            max_quota_bytes or default_settings.STORAGE_MAX_QUOTA_BYTES
        )

    @asynccontextmanager
    async def _resolve_session(
        self, session: Optional[AsyncSession]
    ) -> AsyncIterator[Tuple[AsyncSession, bool]]:
        """Yields the active or newly managed session, along with a caller_owned flag."""
        if session is not None:
            yield session, True
        else:
            async with self.session_factory() as managed_session:
                yield managed_session, False

    async def put_object(
        self,
        tenant_id: str,
        key: str,
        payload: bytes,
        content_type: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        session: Optional[AsyncSession] = None,
    ) -> StorageObjectMetadata:
        safe_tenant = validate_tenant_id(tenant_id)
        safe_key = validate_storage_key(key)

        if not isinstance(payload, (bytes, bytearray)):
            raise TypeError(f"Payload must be bytes, got {type(payload)}")

        payload_bytes = bytes(payload)
        payload_len = len(payload_bytes)

        if payload_len > self.max_file_size_bytes:
            raise StoragePayloadTooLargeError(
                f"Payload size ({payload_len} bytes) exceeds limit of {self.max_file_size_bytes} bytes."
            )

        sha256 = hashlib.sha256(payload_bytes).hexdigest()

        async with self._resolve_session(session) as (sess, caller_owned):
            # Query existing object under tenant isolation
            stmt = select(StorageObject).where(
                StorageObject.tenant_id == safe_tenant,
                StorageObject.key == safe_key,
            )
            existing = (await sess.execute(stmt)).scalar_one_or_none()
            existing_size = existing.file_size_bytes if existing else 0
            delta = payload_len - existing_size

            if delta > 0:
                current_usage = await self.get_tenant_usage_bytes(safe_tenant, session=sess)
                if current_usage + delta > self.max_quota_bytes:
                    raise StorageQuotaExceededError(
                        f"Operation exceeds tenant quota of {self.max_quota_bytes} bytes "
                        f"(current: {current_usage} bytes, delta: {delta} bytes)."
                    )

            now = datetime.now(timezone.utc)
            if existing:
                existing.payload = payload_bytes
                existing.file_size_bytes = payload_len
                existing.sha256_hash = sha256
                existing.content_type = content_type or "application/octet-stream"
                existing.metadata_json = metadata or {}
                existing.updated_at = now
                record = existing
            else:
                record = StorageObject(
                    tenant_id=safe_tenant,
                    key=safe_key,
                    payload=payload_bytes,
                    file_size_bytes=payload_len,
                    sha256_hash=sha256,
                    content_type=content_type or "application/octet-stream",
                    metadata_json=metadata or {},
                )
                sess.add(record)

            if caller_owned:
                await sess.flush()
            else:
                await sess.commit()

            return StorageObjectMetadata(
                tenant_id=record.tenant_id,
                key=record.key,
                file_size_bytes=record.file_size_bytes,
                sha256_hash=record.sha256_hash,
                content_type=record.content_type,
                metadata_json=record.metadata_json,
                created_at=record.created_at,
                updated_at=record.updated_at,
            )

    async def get_object(
        self,
        tenant_id: str,
        key: str,
        session: Optional[AsyncSession] = None,
    ) -> Tuple[bytes, StorageObjectMetadata]:
        safe_tenant = validate_tenant_id(tenant_id)
        safe_key = validate_storage_key(key)

        async with self._resolve_session(session) as (sess, _):
            stmt = select(StorageObject).where(
                StorageObject.tenant_id == safe_tenant,
                StorageObject.key == safe_key,
            )
            record = (await sess.execute(stmt)).scalar_one_or_none()

            if not record:
                raise StorageNotFoundError(
                    f"Storage object '{key}' not found for tenant '{tenant_id}'."
                )

            actual_hash = hashlib.sha256(record.payload).hexdigest()
            if actual_hash != record.sha256_hash:
                raise StorageIntegrityError(
                    f"Checksum mismatch for object '{key}': expected {record.sha256_hash}, calculated {actual_hash}."
                )

            meta = StorageObjectMetadata(
                tenant_id=record.tenant_id,
                key=record.key,
                file_size_bytes=record.file_size_bytes,
                sha256_hash=record.sha256_hash,
                content_type=record.content_type,
                metadata_json=record.metadata_json,
                created_at=record.created_at,
                updated_at=record.updated_at,
            )
            return record.payload, meta

    async def head_object(
        self,
        tenant_id: str,
        key: str,
        session: Optional[AsyncSession] = None,
    ) -> StorageObjectMetadata:
        safe_tenant = validate_tenant_id(tenant_id)
        safe_key = validate_storage_key(key)

        async with self._resolve_session(session) as (sess, _):
            # Do NOT select payload column to keep memory usage low
            stmt = select(
                StorageObject.tenant_id,
                StorageObject.key,
                StorageObject.file_size_bytes,
                StorageObject.sha256_hash,
                StorageObject.content_type,
                StorageObject.metadata_json,
                StorageObject.created_at,
                StorageObject.updated_at,
            ).where(
                StorageObject.tenant_id == safe_tenant,
                StorageObject.key == safe_key,
            )
            row = (await sess.execute(stmt)).one_or_none()

            if not row:
                raise StorageNotFoundError(
                    f"Storage object '{key}' not found for tenant '{tenant_id}'."
                )

            return StorageObjectMetadata(
                tenant_id=row.tenant_id,
                key=row.key,
                file_size_bytes=row.file_size_bytes,
                sha256_hash=row.sha256_hash,
                content_type=row.content_type,
                metadata_json=row.metadata_json,
                created_at=row.created_at,
                updated_at=row.updated_at,
            )

    async def delete_object(
        self,
        tenant_id: str,
        key: str,
        session: Optional[AsyncSession] = None,
    ) -> bool:
        safe_tenant = validate_tenant_id(tenant_id)
        safe_key = validate_storage_key(key)

        async with self._resolve_session(session) as (sess, caller_owned):
            stmt = select(StorageObject).where(
                StorageObject.tenant_id == safe_tenant,
                StorageObject.key == safe_key,
            )
            record = (await sess.execute(stmt)).scalar_one_or_none()

            if not record:
                return False

            await sess.delete(record)
            if caller_owned:
                await sess.flush()
            else:
                await sess.commit()

            return True

    async def object_exists(
        self,
        tenant_id: str,
        key: str,
        session: Optional[AsyncSession] = None,
    ) -> bool:
        safe_tenant = validate_tenant_id(tenant_id)
        safe_key = validate_storage_key(key)

        async with self._resolve_session(session) as (sess, _):
            stmt = select(func.count(StorageObject.id)).where(
                StorageObject.tenant_id == safe_tenant,
                StorageObject.key == safe_key,
            )
            count = (await sess.execute(stmt)).scalar() or 0
            return count > 0

    async def list_objects(
        self,
        tenant_id: str,
        prefix: Optional[str] = None,
        limit: int = 100,
        session: Optional[AsyncSession] = None,
    ) -> List[StorageObjectMetadata]:
        safe_tenant = validate_tenant_id(tenant_id)

        async with self._resolve_session(session) as (sess, _):
            stmt = select(
                StorageObject.tenant_id,
                StorageObject.key,
                StorageObject.file_size_bytes,
                StorageObject.sha256_hash,
                StorageObject.content_type,
                StorageObject.metadata_json,
                StorageObject.created_at,
                StorageObject.updated_at,
            ).where(StorageObject.tenant_id == safe_tenant)

            if prefix:
                safe_prefix = prefix.replace("\\", "/")
                stmt = stmt.where(StorageObject.key.startswith(safe_prefix))

            stmt = stmt.order_by(StorageObject.created_at.desc()).limit(limit)
            rows = (await sess.execute(stmt)).all()

            return [
                StorageObjectMetadata(
                    tenant_id=r.tenant_id,
                    key=r.key,
                    file_size_bytes=r.file_size_bytes,
                    sha256_hash=r.sha256_hash,
                    content_type=r.content_type,
                    metadata_json=r.metadata_json,
                    created_at=r.created_at,
                    updated_at=r.updated_at,
                )
                for r in rows
            ]

    async def get_tenant_usage_bytes(
        self,
        tenant_id: str,
        session: Optional[AsyncSession] = None,
    ) -> int:
        safe_tenant = validate_tenant_id(tenant_id)

        async with self._resolve_session(session) as (sess, _):
            stmt = select(func.coalesce(func.sum(StorageObject.file_size_bytes), 0)).where(
                StorageObject.tenant_id == safe_tenant
            )
            total = (await sess.execute(stmt)).scalar() or 0
            return int(total)


# =============================================================================
# Authenticated Encryption Helpers (AES-256-GCM)
# =============================================================================

async def put_encrypted_object(
    adapter: BaseStorageAdapter,
    tenant_id: str,
    key: str,
    payload: bytes,
    content_type: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    session: Optional[AsyncSession] = None,
    encryption_engine: Optional[ApplicationLayerEncryption] = None,
) -> StorageObjectMetadata:
    """Encrypts payload with authenticated AES-256-GCM binding tenant:key as associated data."""
    if not isinstance(payload, (bytes, bytearray)):
        raise TypeError(f"Payload must be bytes, got {type(payload)}")

    engine = encryption_engine or get_encryption_engine()
    assoc_data = f"{tenant_id}:{key}".encode("utf-8")
    encrypted_payload = engine.encrypt_bytes(bytes(payload), associated_data=assoc_data)

    meta = dict(metadata or {})
    meta["encrypted"] = True
    meta["encryption_algorithm"] = "AES-256-GCM"
    meta["assoc_data_binding"] = f"{tenant_id}:{key}"

    return await adapter.put_object(
        tenant_id=tenant_id,
        key=key,
        payload=encrypted_payload,
        content_type=content_type,
        metadata=meta,
        session=session,
    )


async def get_decrypted_object(
    adapter: BaseStorageAdapter,
    tenant_id: str,
    key: str,
    session: Optional[AsyncSession] = None,
    encryption_engine: Optional[ApplicationLayerEncryption] = None,
) -> Tuple[bytes, StorageObjectMetadata]:
    """Retrieves and decrypts payload, enforcing AES-256-GCM authentication and tamper detection."""
    engine = encryption_engine or get_encryption_engine()
    encrypted_payload, meta = await adapter.get_object(tenant_id=tenant_id, key=key, session=session)

    assoc_data = f"{tenant_id}:{key}".encode("utf-8")
    try:
        decrypted_payload = engine.decrypt_bytes(encrypted_payload, associated_data=assoc_data)
    except Exception as exc:
        raise StorageIntegrityError(
            f"Tamper detection: Decryption verification failed for '{key}' under tenant '{tenant_id}': {exc}"
        ) from exc

    return decrypted_payload, meta


# =============================================================================
# Storage Service Facade & Singleton
# =============================================================================

class StorageService:
    """Unified storage service facade coordinating provider adapters and encryption."""

    def __init__(
        self,
        provider: Optional[str] = None,
        adapter: Optional[BaseStorageAdapter] = None,
        settings_obj: Optional[Settings] = None,
        session_factory: Optional[Callable[[], AsyncSession]] = None,
    ) -> None:
        self.settings = settings_obj or default_settings

        if adapter is not None:
            self.adapter = adapter
            self.provider = getattr(adapter, "provider_name", "custom")
        else:
            prov = (provider or self.settings.STORAGE_PROVIDER).lower().strip()
            self.provider = prov

            if prov == "local":
                self.adapter = LocalStorageAdapter(
                    root_dir=self.settings.STORAGE_LOCAL_ROOT,
                    max_file_size_bytes=self.settings.STORAGE_MAX_FILE_SIZE_BYTES,
                    max_quota_bytes=self.settings.STORAGE_MAX_QUOTA_BYTES,
                )
            elif prov == "postgres":
                self.adapter = PostgresByteaStorageAdapter(
                    session_factory=session_factory or AsyncSessionLocal,
                    max_file_size_bytes=self.settings.STORAGE_MAX_FILE_SIZE_BYTES,
                    max_quota_bytes=self.settings.STORAGE_MAX_QUOTA_BYTES,
                )
            else:
                raise StorageConfigurationError(
                    f"Unsupported storage provider '{prov}'. Supported providers are: 'local', 'postgres'."
                )

    async def put_object(
        self,
        tenant_id: str,
        key: str,
        payload: bytes,
        content_type: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        session: Optional[AsyncSession] = None,
    ) -> StorageObjectMetadata:
        return await self.adapter.put_object(
            tenant_id=tenant_id,
            key=key,
            payload=payload,
            content_type=content_type,
            metadata=metadata,
            session=session,
        )

    async def get_object(
        self,
        tenant_id: str,
        key: str,
        session: Optional[AsyncSession] = None,
    ) -> Tuple[bytes, StorageObjectMetadata]:
        return await self.adapter.get_object(tenant_id=tenant_id, key=key, session=session)

    async def head_object(
        self,
        tenant_id: str,
        key: str,
        session: Optional[AsyncSession] = None,
    ) -> StorageObjectMetadata:
        return await self.adapter.head_object(tenant_id=tenant_id, key=key, session=session)

    async def delete_object(
        self,
        tenant_id: str,
        key: str,
        session: Optional[AsyncSession] = None,
    ) -> bool:
        return await self.adapter.delete_object(tenant_id=tenant_id, key=key, session=session)

    async def object_exists(
        self,
        tenant_id: str,
        key: str,
        session: Optional[AsyncSession] = None,
    ) -> bool:
        return await self.adapter.object_exists(tenant_id=tenant_id, key=key, session=session)

    async def list_objects(
        self,
        tenant_id: str,
        prefix: Optional[str] = None,
        limit: int = 100,
        session: Optional[AsyncSession] = None,
    ) -> List[StorageObjectMetadata]:
        return await self.adapter.list_objects(
            tenant_id=tenant_id, prefix=prefix, limit=limit, session=session
        )

    async def get_tenant_usage_bytes(
        self,
        tenant_id: str,
        session: Optional[AsyncSession] = None,
    ) -> int:
        return await self.adapter.get_tenant_usage_bytes(tenant_id=tenant_id, session=session)

    async def put_encrypted_object(
        self,
        tenant_id: str,
        key: str,
        payload: bytes,
        content_type: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        session: Optional[AsyncSession] = None,
        encryption_engine: Optional[ApplicationLayerEncryption] = None,
    ) -> StorageObjectMetadata:
        return await put_encrypted_object(
            adapter=self.adapter,
            tenant_id=tenant_id,
            key=key,
            payload=payload,
            content_type=content_type,
            metadata=metadata,
            session=session,
            encryption_engine=encryption_engine,
        )

    async def get_decrypted_object(
        self,
        tenant_id: str,
        key: str,
        session: Optional[AsyncSession] = None,
        encryption_engine: Optional[ApplicationLayerEncryption] = None,
    ) -> Tuple[bytes, StorageObjectMetadata]:
        return await get_decrypted_object(
            adapter=self.adapter,
            tenant_id=tenant_id,
            key=key,
            session=session,
            encryption_engine=encryption_engine,
        )


_default_storage_service: Optional[StorageService] = None


def get_storage_service(provider: Optional[str] = None) -> StorageService:
    """Returns singleton instance of the unified StorageService."""
    global _default_storage_service
    if _default_storage_service is None or (provider and _default_storage_service.provider != provider):
        _default_storage_service = StorageService(provider=provider)
    return _default_storage_service


def reset_storage_service() -> None:
    """Resets the singleton instance for isolated test fixtures."""
    global _default_storage_service
    _default_storage_service = None

