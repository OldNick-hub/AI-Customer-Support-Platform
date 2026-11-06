"""Secure Storage and Encryption-at-Rest Manager for Evidence Files.

Provides:
1. Path-traversal-safe isolated disk persistence under `settings.EVIDENCE_STORAGE_DIR`.
2. Authenticated AES-256-GCM encryption at rest bound to the unique `evidence_id`.
3. Safe retrieval and decryption pipeline.
"""

import os
from pathlib import Path
from typing import Optional
import uuid

from app.core.config import settings
from app.core.encryption import ApplicationLayerEncryption, get_encryption_engine
from app.core.logging import configure_safe_logger
import logging

logger = configure_safe_logger(logging.getLogger(__name__))


class StorageSecurityError(Exception):
    """Raised when an attempt to escape or violate storage boundaries is detected."""
    pass


class EvidenceStorageManager:
    """Manages disk staging and authenticated AES-256-GCM encryption for evidence files."""

    def __init__(
        self,
        base_dir: Optional[str] = None,
        encryption_engine: Optional[ApplicationLayerEncryption] = None,
    ):
        raw_dir = base_dir or getattr(settings, "EVIDENCE_STORAGE_DIR", "./storage/evidence")
        self.base_dir = Path(raw_dir).resolve()
        self.encryption = encryption_engine or get_encryption_engine()
        # Ensure root evidence storage directory exists
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _resolve_customer_dir(self, customer_id: Optional[uuid.UUID] = None) -> Path:
        """Resolves and creates customer-scoped directory, enforcing path boundary."""
        cust_sub = str(customer_id) if customer_id else "unassigned"
        target_dir = (self.base_dir / cust_sub).resolve()

        if not target_dir.is_relative_to(self.base_dir):
            raise StorageSecurityError(f"Customer directory '{target_dir}' escapes base storage boundary '{self.base_dir}'.")

        target_dir.mkdir(parents=True, exist_ok=True)
        return target_dir

    def store_file(
        self,
        file_bytes: bytes,
        evidence_id: uuid.UUID,
        customer_id: Optional[uuid.UUID] = None,
        extension: str = ".bin",
    ) -> str:
        """Encrypts and persists evidence payload at rest.

        Args:
            file_bytes: Raw plaintext file payload.
            evidence_id: Unique record UUID used as AES-GCM associated data.
            customer_id: Customer UUID for filesystem isolation.
            extension: File extension.

        Returns:
            storage_uri: Local file URI (e.g. file:///... or relative path).
        """
        cust_dir = self._resolve_customer_dir(customer_id)
        file_name = f"{evidence_id}{extension}.enc"
        file_path = (cust_dir / file_name).resolve()

        if not file_path.is_relative_to(self.base_dir):
            raise StorageSecurityError(f"Target path '{file_path}' escapes base storage directory.")

        # Encrypt raw binary bytes with AES-256-GCM, binding evidence_id as authenticated associated data
        assoc_data = str(evidence_id).encode("utf-8")
        nonce = os.urandom(12)
        ciphertext = self.encryption._aesgcm.encrypt(nonce, file_bytes, assoc_data)
        encrypted_payload = nonce + ciphertext

        file_path.write_bytes(encrypted_payload)
        logger.info("Encrypted and stored evidence file %s (%d bytes).", evidence_id, len(encrypted_payload))

        # Return standardized storage URI
        return str(file_path.as_posix())

    def load_file(
        self,
        storage_uri: str,
        evidence_id: uuid.UUID,
    ) -> bytes:
        """Retrieves and decrypts an encrypted binary evidence payload."""
        file_path = Path(storage_uri).resolve()

        if not file_path.is_relative_to(self.base_dir):
            raise StorageSecurityError(f"Storage URI '{storage_uri}' resolves outside authorized storage root.")

        if not file_path.is_file():
            raise FileNotFoundError(f"Evidence file not found on disk: {storage_uri}")

        encrypted_payload = file_path.read_bytes()
        assoc_data = str(evidence_id).encode("utf-8")

        nonce = encrypted_payload[:12]
        ciphertext = encrypted_payload[12:]
        return self.encryption._aesgcm.decrypt(nonce, ciphertext, assoc_data)

    def delete_file(self, storage_uri: str) -> bool:
        """Deletes evidence file from disk."""
        try:
            file_path = Path(storage_uri).resolve()
            if file_path.is_relative_to(self.base_dir) and file_path.is_file():
                file_path.unlink()
                return True
        except Exception as exc:
            logger.warning("Could not delete evidence file %s: %s", storage_uri, exc)
        return False


default_evidence_storage = EvidenceStorageManager()

