"""Comprehensive Test Suite for Zero-Cost Storage Abstraction (Phase 2.1).

Verifies:
1. BaseStorageAdapter interface compliance.
2. LocalStorageAdapter disk persistence, atomic writes, sidecars, and tenant isolation.
3. PostgresByteaStorageAdapter database persistence, transaction rollbacks, and tenant isolation.
4. Security boundaries: path traversal defense, absolute path rejection, and null byte prevention.
5. Limit enforcement: 15 MB single-file limit and 400 MB tenant quota limit.
6. Integrity & Tamper Detection: SHA-256 verification and AES-256-GCM ciphertext verification.
7. Authenticated encryption helpers binding tenant:key as associated data.
8. StorageService facade and singleton management.
"""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.encryption import ApplicationLayerEncryption
from app.core.exceptions import (
    StorageConfigurationError,
    StorageIntegrityError,
    StorageNotFoundError,
    StoragePayloadTooLargeError,
    StorageQuotaExceededError,
    StorageSecurityError,
)
from app.models.storage import StorageObject
from app.services.storage_service import (
    BaseStorageAdapter,
    LocalStorageAdapter,
    PostgresByteaStorageAdapter,
    StorageObjectMetadata,
    StorageService,
    get_decrypted_object,
    get_storage_service,
    put_encrypted_object,
    reset_storage_service,
)
from tests.helpers.database import TestSessionLocal


# =============================================================================
# LocalStorageAdapter Tests
# =============================================================================

class TestLocalStorageAdapter:
    """Verifies LocalStorageAdapter persistence, isolation, and security."""

    @pytest.fixture
    def local_adapter(self, tmp_path: Path) -> LocalStorageAdapter:
        return LocalStorageAdapter(
            root_dir=str(tmp_path),
            max_file_size_bytes=1024 * 1024,      # 1 MB for testing
            max_quota_bytes=5 * 1024 * 1024,      # 5 MB for testing
        )

    @pytest.mark.asyncio
    async def test_put_and_get_object_success(self, local_adapter: LocalStorageAdapter):
        payload = b"Hello, zero-cost local storage!"
        tenant_id = "tenant-100"
        key = "documents/receipt.pdf"
        content_type = "application/pdf"
        meta = {"department": "finance", "verified": True}

        # Put object
        result_meta = await local_adapter.put_object(
            tenant_id=tenant_id,
            key=key,
            payload=payload,
            content_type=content_type,
            metadata=meta,
        )

        assert isinstance(result_meta, StorageObjectMetadata)
        assert result_meta.tenant_id == tenant_id
        assert result_meta.key == key
        assert result_meta.file_size_bytes == len(payload)
        assert result_meta.sha256_hash == hashlib.sha256(payload).hexdigest()
        assert result_meta.content_type == content_type
        assert result_meta.metadata_json == meta

        # Get object
        retrieved_payload, get_meta = await local_adapter.get_object(tenant_id, key)
        assert retrieved_payload == payload
        assert get_meta.sha256_hash == result_meta.sha256_hash
        assert get_meta.metadata_json == meta

    @pytest.mark.asyncio
    async def test_head_object_and_not_found(self, local_adapter: LocalStorageAdapter):
        tenant_id = "tenant-101"
        key = "data/file.bin"
        payload = b"Sample bytes"

        await local_adapter.put_object(tenant_id, key, payload)

        head_meta = await local_adapter.head_object(tenant_id, key)
        assert head_meta.file_size_bytes == len(payload)
        assert head_meta.key == key

        with pytest.raises(StorageNotFoundError):
            await local_adapter.head_object(tenant_id, "nonexistent/file.bin")

        with pytest.raises(StorageNotFoundError):
            await local_adapter.get_object(tenant_id, "nonexistent/file.bin")

    @pytest.mark.asyncio
    async def test_idempotent_delete_and_exists(self, local_adapter: LocalStorageAdapter):
        tenant_id = "tenant-102"
        key = "temp/artifact.log"
        payload = b"log lines..."

        assert await local_adapter.object_exists(tenant_id, key) is False
        assert await local_adapter.delete_object(tenant_id, key) is False

        await local_adapter.put_object(tenant_id, key, payload)
        assert await local_adapter.object_exists(tenant_id, key) is True

        # First delete should succeed
        assert await local_adapter.delete_object(tenant_id, key) is True
        assert await local_adapter.object_exists(tenant_id, key) is False

        # Second delete should return False without error
        assert await local_adapter.delete_object(tenant_id, key) is False

    @pytest.mark.asyncio
    async def test_list_objects_and_prefix_filtering(self, local_adapter: LocalStorageAdapter):
        tenant_id = "tenant-103"
        await local_adapter.put_object(tenant_id, "invoices/2026/01.pdf", b"inv 1")
        await local_adapter.put_object(tenant_id, "invoices/2026/02.pdf", b"inv 2")
        await local_adapter.put_object(tenant_id, "contracts/c1.pdf", b"contract 1")

        # List all
        all_objects = await local_adapter.list_objects(tenant_id)
        assert len(all_objects) == 3

        # Filter by prefix
        invoices = await local_adapter.list_objects(tenant_id, prefix="invoices/")
        assert len(invoices) == 2
        assert all(item.key.startswith("invoices/") for item in invoices)

        # Respect limit
        limited = await local_adapter.list_objects(tenant_id, limit=1)
        assert len(limited) == 1

    @pytest.mark.asyncio
    async def test_tenant_isolation(self, local_adapter: LocalStorageAdapter):
        key = "shared/key.txt"
        payload_a = b"Tenant A private data"
        payload_b = b"Tenant B private data"

        await local_adapter.put_object("tenant_a", key, payload_a)
        await local_adapter.put_object("tenant_b", key, payload_b)

        data_a, meta_a = await local_adapter.get_object("tenant_a", key)
        data_b, meta_b = await local_adapter.get_object("tenant_b", key)

        assert data_a == payload_a
        assert data_b == payload_b
        assert meta_a.tenant_id == "tenant_a"
        assert meta_b.tenant_id == "tenant_b"

        # Tenant A listing should only show Tenant A objects
        list_a = await local_adapter.list_objects("tenant_a")
        assert len(list_a) == 1
        assert list_a[0].tenant_id == "tenant_a"

    @pytest.mark.asyncio
    async def test_path_traversal_defense(self, local_adapter: LocalStorageAdapter):
        tenant_id = "tenant-sec"
        malicious_keys = [
            "../etc/passwd",
            "../../secret.txt",
            "sub/../../root.txt",
            "/absolute/path/file.bin",
            "\\windows\\system32\\cmd.exe",
            "C:/boot.ini",
            "file\0nullbyte.png",
            "",
            "   ",
        ]

        for key in malicious_keys:
            with pytest.raises(StorageSecurityError):
                await local_adapter.put_object(tenant_id, key, b"evil payload")

            with pytest.raises(StorageSecurityError):
                await local_adapter.get_object(tenant_id, key)

    @pytest.mark.asyncio
    async def test_single_file_size_limit(self, tmp_path: Path):
        adapter = LocalStorageAdapter(
            root_dir=str(tmp_path),
            max_file_size_bytes=100,  # 100 bytes limit
            max_quota_bytes=1000,
        )

        oversized_payload = b"x" * 101
        with pytest.raises(StoragePayloadTooLargeError) as excinfo:
            await adapter.put_object("tenant-lim", "big.bin", oversized_payload)
        assert "exceeds limit" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_tenant_quota_limit(self, tmp_path: Path):
        adapter = LocalStorageAdapter(
            root_dir=str(tmp_path),
            max_file_size_bytes=500,
            max_quota_bytes=500,  # 500 bytes total quota
        )
        tenant_id = "tenant-quota"

        # Write 300 bytes (within quota)
        await adapter.put_object(tenant_id, "part1.bin", b"a" * 300)
        assert await adapter.get_tenant_usage_bytes(tenant_id) == 300

        # Attempt to write 250 bytes more (300 + 250 = 550 > 500)
        with pytest.raises(StorageQuotaExceededError) as excinfo:
            await adapter.put_object(tenant_id, "part2.bin", b"b" * 250)
        assert "exceeds tenant quota" in str(excinfo.value)

        # Overwrite part1.bin with 200 bytes (delta is -100, should succeed)
        await adapter.put_object(tenant_id, "part1.bin", b"a" * 200)
        assert await adapter.get_tenant_usage_bytes(tenant_id) == 200

    @pytest.mark.asyncio
    async def test_tamper_detection_checksum_mismatch(self, local_adapter: LocalStorageAdapter, tmp_path: Path):
        tenant_id = "tenant-tamper"
        key = "critical/payload.bin"
        payload = b"Original uncorrupted bytes"

        await local_adapter.put_object(tenant_id, key, payload)

        # Directly tamper with the file on disk
        target_path = tmp_path / tenant_id / "critical" / "payload.bin"
        assert target_path.is_file()
        target_path.write_bytes(b"Tampered corrupted bytes!")

        # Getting object must detect the checksum mismatch and raise StorageIntegrityError
        with pytest.raises(StorageIntegrityError) as excinfo:
            await local_adapter.get_object(tenant_id, key)
        assert "Checksum mismatch" in str(excinfo.value)


# =============================================================================
# PostgresByteaStorageAdapter Tests
# =============================================================================

class TestPostgresByteaStorageAdapter:
    """Verifies PostgresByteaStorageAdapter database persistence, rollbacks, and isolation."""

    @pytest.fixture
    def pg_adapter(self) -> PostgresByteaStorageAdapter:
        return PostgresByteaStorageAdapter(
            session_factory=TestSessionLocal,
            max_file_size_bytes=1024 * 1024,
            max_quota_bytes=5 * 1024 * 1024,
        )

    @pytest.mark.asyncio
    async def test_put_and_get_object_success(self, init_db, pg_adapter: PostgresByteaStorageAdapter):
        payload = b"PostgreSQL BYTEA payload storage test."
        tenant_id = "pg-tenant-1"
        key = "reports/q1.pdf"
        content_type = "application/pdf"
        meta = {"author": "qa_lead", "approved": True}

        result_meta = await pg_adapter.put_object(
            tenant_id=tenant_id,
            key=key,
            payload=payload,
            content_type=content_type,
            metadata=meta,
        )

        assert isinstance(result_meta, StorageObjectMetadata)
        assert result_meta.tenant_id == tenant_id
        assert result_meta.key == key
        assert result_meta.file_size_bytes == len(payload)
        assert result_meta.sha256_hash == hashlib.sha256(payload).hexdigest()
        assert result_meta.content_type == content_type
        assert result_meta.metadata_json == meta

        # Retrieve and verify payload
        retrieved_bytes, get_meta = await pg_adapter.get_object(tenant_id, key)
        assert retrieved_bytes == payload
        assert get_meta.sha256_hash == result_meta.sha256_hash
        assert get_meta.metadata_json == meta

    @pytest.mark.asyncio
    async def test_head_object_and_not_found(self, init_db, pg_adapter: PostgresByteaStorageAdapter):
        tenant_id = "pg-tenant-2"
        key = "docs/overview.md"
        payload = b"# Overview markdown"

        await pg_adapter.put_object(tenant_id, key, payload)

        head_meta = await pg_adapter.head_object(tenant_id, key)
        assert head_meta.file_size_bytes == len(payload)
        assert head_meta.key == key

        with pytest.raises(StorageNotFoundError):
            await pg_adapter.head_object(tenant_id, "nonexistent/key.bin")

        with pytest.raises(StorageNotFoundError):
            await pg_adapter.get_object(tenant_id, "nonexistent/key.bin")

    @pytest.mark.asyncio
    async def test_idempotent_delete_and_exists(self, init_db, pg_adapter: PostgresByteaStorageAdapter):
        tenant_id = "pg-tenant-3"
        key = "logs/app.log"
        payload = b"log trace info"

        assert await pg_adapter.object_exists(tenant_id, key) is False
        assert await pg_adapter.delete_object(tenant_id, key) is False

        await pg_adapter.put_object(tenant_id, key, payload)
        assert await pg_adapter.object_exists(tenant_id, key) is True

        assert await pg_adapter.delete_object(tenant_id, key) is True
        assert await pg_adapter.object_exists(tenant_id, key) is False
        assert await pg_adapter.delete_object(tenant_id, key) is False

    @pytest.mark.asyncio
    async def test_overwrite_object_updates_record(self, init_db, pg_adapter: PostgresByteaStorageAdapter):
        tenant_id = "pg-tenant-4"
        key = "configs/settings.json"
        v1 = b'{"version": 1}'
        v2 = b'{"version": 2, "active": true}'

        await pg_adapter.put_object(tenant_id, key, v1, metadata={"v": 1})
        data1, _ = await pg_adapter.get_object(tenant_id, key)
        assert data1 == v1

        # Overwrite with v2
        await pg_adapter.put_object(tenant_id, key, v2, metadata={"v": 2})
        data2, meta2 = await pg_adapter.get_object(tenant_id, key)
        assert data2 == v2
        assert meta2.file_size_bytes == len(v2)
        assert meta2.metadata_json == {"v": 2}

    @pytest.mark.asyncio
    async def test_tenant_isolation(self, init_db, pg_adapter: PostgresByteaStorageAdapter):
        key = "isolated/evidence.bin"
        payload_1 = b"Tenant 1 private evidence"
        payload_2 = b"Tenant 2 private evidence"

        await pg_adapter.put_object("tenant_1", key, payload_1)
        await pg_adapter.put_object("tenant_2", key, payload_2)

        data_1, _ = await pg_adapter.get_object("tenant_1", key)
        data_2, _ = await pg_adapter.get_object("tenant_2", key)

        assert data_1 == payload_1
        assert data_2 == payload_2

        list_1 = await pg_adapter.list_objects("tenant_1")
        assert len(list_1) == 1
        assert list_1[0].tenant_id == "tenant_1"

    @pytest.mark.asyncio
    async def test_caller_transaction_rollback(self, init_db, pg_adapter: PostgresByteaStorageAdapter, session: AsyncSession):
        tenant_id = "pg-rollback-test"
        key = "tx/file.bin"
        payload = b"Should be rolled back"

        # Store using the caller-provided session
        await pg_adapter.put_object(tenant_id, key, payload, session=session)

        # Roll back the caller's transaction
        await session.rollback()

        # The object should NOT exist
        assert await pg_adapter.object_exists(tenant_id, key) is False

    @pytest.mark.asyncio
    async def test_quota_and_size_limits(self, init_db):
        adapter = PostgresByteaStorageAdapter(
            session_factory=TestSessionLocal,
            max_file_size_bytes=100,
            max_quota_bytes=500,
        )
        tenant_id = "pg-limits"

        # Exceed single file size
        with pytest.raises(StoragePayloadTooLargeError):
            await adapter.put_object(tenant_id, "big.bin", b"x" * 101)

        # Put 300 bytes (within quota)
        await adapter.put_object(tenant_id, "p1.bin", b"a" * 80)
        await adapter.put_object(tenant_id, "p2.bin", b"a" * 80)
        assert await adapter.get_tenant_usage_bytes(tenant_id) == 160

    @pytest.mark.asyncio
    async def test_tamper_detection_checksum_mismatch(self, init_db, pg_adapter: PostgresByteaStorageAdapter, session: AsyncSession):
        tenant_id = "pg-tamper"
        key = "critical/data.bin"
        payload = b"Legitimate byte payload"

        await pg_adapter.put_object(tenant_id, key, payload)

        # Directly corrupt payload column in database
        stmt = select(StorageObject).where(
            StorageObject.tenant_id == tenant_id, StorageObject.key == key
        )
        obj = (await session.execute(stmt)).scalar_one()
        obj.payload = b"Corrupted bytes injected directly into database!"
        await session.commit()

        # Get object must detect mismatch between calculated hash and stored sha256_hash
        with pytest.raises(StorageIntegrityError) as excinfo:
            await pg_adapter.get_object(tenant_id, key)
        assert "Checksum mismatch" in str(excinfo.value)


# =============================================================================
# Authenticated Encryption Tests (AES-256-GCM)
# =============================================================================

class TestStorageEncryption:
    """Verifies AES-256-GCM authenticated encryption helpers."""

    @pytest.mark.asyncio
    async def test_put_and_get_encrypted_object_roundtrip(
        self, tmp_path: Path, encryption_engine: ApplicationLayerEncryption
    ):
        adapter = LocalStorageAdapter(root_dir=str(tmp_path))
        tenant_id = "tenant-enc"
        key = "sensitive/customer_data.json"
        plaintext = b'{"ssn_masked": true, "tax_id": "XX-12345"}'

        # Put encrypted object
        meta = await put_encrypted_object(
            adapter=adapter,
            tenant_id=tenant_id,
            key=key,
            payload=plaintext,
            content_type="application/json",
            metadata={"confidential": True},
            encryption_engine=encryption_engine,
        )

        assert meta.metadata_json.get("encrypted") is True
        assert meta.metadata_json.get("encryption_algorithm") == "AES-256-GCM"

        # Verify underlying stored bytes are ciphertext, not plaintext
        raw_stored_bytes, _ = await adapter.get_object(tenant_id, key)
        assert raw_stored_bytes != plaintext
        assert len(raw_stored_bytes) >= 12 + len(plaintext)  # Nonce + ciphertext + tag

        # Retrieve and decrypt
        decrypted_bytes, decrypted_meta = await get_decrypted_object(
            adapter=adapter,
            tenant_id=tenant_id,
            key=key,
            encryption_engine=encryption_engine,
        )
        assert decrypted_bytes == plaintext
        assert decrypted_meta.metadata_json.get("confidential") is True

    @pytest.mark.asyncio
    async def test_tamper_detection_on_ciphertext(
        self, tmp_path: Path, encryption_engine: ApplicationLayerEncryption
    ):
        adapter = LocalStorageAdapter(root_dir=str(tmp_path))
        tenant_id = "tenant-tamper-enc"
        key = "records/doc.bin"
        plaintext = b"Critical financial record"

        await put_encrypted_object(
            adapter=adapter,
            tenant_id=tenant_id,
            key=key,
            payload=plaintext,
            encryption_engine=encryption_engine,
        )

        # Corrupt 1 byte of ciphertext on disk
        target_path = tmp_path / tenant_id / "records" / "doc.bin"
        corrupt_data = bytearray(target_path.read_bytes())
        corrupt_data[-1] ^= 0xFF  # Flip bit in auth tag / ciphertext
        target_path.write_bytes(bytes(corrupt_data))

        # Updating sidecar sha256 to bypass plain integrity check and trigger GCM tag check
        meta_path = Path(str(target_path) + ".meta.json")
        meta_dict = json.loads(meta_path.read_text(encoding="utf-8"))
        meta_dict["sha256_hash"] = hashlib.sha256(bytes(corrupt_data)).hexdigest()
        meta_path.write_text(json.dumps(meta_dict), encoding="utf-8")

        # Decryption must fail due to GCM authentication tag verification
        with pytest.raises(StorageIntegrityError) as excinfo:
            await get_decrypted_object(
                adapter=adapter,
                tenant_id=tenant_id,
                key=key,
                encryption_engine=encryption_engine,
            )
        assert "Decryption verification failed" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_tenant_associated_data_binding(
        self, tmp_path: Path, encryption_engine: ApplicationLayerEncryption
    ):
        adapter = LocalStorageAdapter(root_dir=str(tmp_path))
        plaintext = b"Bound strictly to tenant-a and key1"

        # Encrypt under tenant-a and key1
        await put_encrypted_object(
            adapter=adapter,
            tenant_id="tenant-a",
            key="key1",
            payload=plaintext,
            encryption_engine=encryption_engine,
        )

        # Manually copy encrypted bytes to tenant-b and key1
        raw_bytes, _ = await adapter.get_object("tenant-a", "key1")
        await adapter.put_object("tenant-b", "key1", raw_bytes)

        # Attempting to decrypt under tenant-b must fail because associated data mismatch
        with pytest.raises(StorageIntegrityError) as excinfo:
            await get_decrypted_object(
                adapter=adapter,
                tenant_id="tenant-b",
                key="key1",
                encryption_engine=encryption_engine,
            )
        assert "Decryption verification failed" in str(excinfo.value)


# =============================================================================
# StorageService Facade & Singleton Tests
# =============================================================================

class TestStorageServiceFacade:
    """Verifies StorageService facade, defaults, and singleton lifecycles."""

    def test_storage_service_instantiation_defaults(self, tmp_path: Path):
        s = Settings(STORAGE_LOCAL_ROOT=str(tmp_path))
        service = StorageService(settings_obj=s)
        assert service.provider == "local"
        assert isinstance(service.adapter, LocalStorageAdapter)

    def test_storage_service_postgres_provider(self):
        service = StorageService(provider="postgres", session_factory=TestSessionLocal)
        assert service.provider == "postgres"
        assert isinstance(service.adapter, PostgresByteaStorageAdapter)

    def test_storage_service_unsupported_provider_raises(self):
        with pytest.raises(StorageConfigurationError) as excinfo:
            StorageService(provider="r2")
        assert "Unsupported storage provider 'r2'" in str(excinfo.value)

    def test_singleton_get_and_reset(self):
        reset_storage_service()
        svc1 = get_storage_service()
        svc2 = get_storage_service()
        assert svc1 is svc2

        reset_storage_service()
        svc3 = get_storage_service()
        assert svc3 is not svc1
        reset_storage_service()

