"""Application-Layer Authenticated Encryption (AES-256-GCM).

Provides authenticated confidentiality and tamper detection for retained raw dialog content,
as mandated by GDPR data minimization and PCI-DSS requirements.
"""

import os
from typing import Optional, Union
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from app.core.config import settings


class ApplicationLayerEncryption:
    """Provides authenticated AES-256-GCM encryption for sensitive raw customer utterances.

    Structure of encrypted payload:
    [0:12]   - 96-bit (12-byte) cryptographically secure random initialization nonce
    [12:]    - AES-256-GCM ciphertext concatenated with 128-bit authentication tag
    """

    NONCE_LENGTH_BYTES = 12
    KEY_LENGTH_BYTES = 32

    def __init__(self, key: Optional[bytes] = None):
        if key is None:
            key = settings.encryption_key_bytes

        if not isinstance(key, bytes) or len(key) != self.KEY_LENGTH_BYTES:
            raise ValueError(
                f"AES-256 encryption key must be exactly {self.KEY_LENGTH_BYTES} bytes. "
                f"Received {len(key) if isinstance(key, bytes) else type(key)}."
            )
        self._key = key
        self._aesgcm = AESGCM(self._key)

    def encrypt(self, plaintext: str, associated_data: bytes = b"") -> bytes:
        """Encrypts plaintext with a unique random 96-bit nonce.

        Args:
            plaintext: Raw string to encrypt.
            associated_data: Optional authenticated data bound to ciphertext (e.g. message_id).

        Returns:
            Concatenated bytes: nonce (12 bytes) + ciphertext + tag.
        """
        if not isinstance(plaintext, str):
            raise TypeError(f"Plaintext must be a string, got {type(plaintext)}")

        # Cryptographically secure random 96-bit nonce per NIST SP 800-38D
        nonce = os.urandom(self.NONCE_LENGTH_BYTES)
        ciphertext = self._aesgcm.encrypt(nonce, plaintext.encode("utf-8"), associated_data)
        return nonce + ciphertext

    def decrypt(self, encrypted_bytes: bytes, associated_data: bytes = b"") -> str:
        """Decrypts and authenticates ciphertext using the extracted nonce.

        Args:
            encrypted_bytes: Concatenated nonce (12 bytes) + ciphertext + tag.
            associated_data: Authenticated data bound during encryption.

        Returns:
            Decrypted plaintext string.

        Raises:
            ValueError: If payload length is shorter than the 12-byte nonce.
            InvalidTag: If the ciphertext or tag has been modified or corrupted (tamper detection).
        """
        if not isinstance(encrypted_bytes, bytes):
            raise TypeError(f"Encrypted payload must be bytes, got {type(encrypted_bytes)}")

        if len(encrypted_bytes) < self.NONCE_LENGTH_BYTES:
            raise ValueError(
                f"Encrypted payload too short. Minimum length is {self.NONCE_LENGTH_BYTES} bytes."
            )

        nonce = encrypted_bytes[: self.NONCE_LENGTH_BYTES]
        ciphertext_and_tag = encrypted_bytes[self.NONCE_LENGTH_BYTES :]

        # AESGCM.decrypt raises InvalidTag if tag verification fails
        decrypted_bytes = self._aesgcm.decrypt(nonce, ciphertext_and_tag, associated_data)
        return decrypted_bytes.decode("utf-8")

    def encrypt_bytes(self, data: bytes, associated_data: bytes = b"") -> bytes:
        """Encrypts arbitrary raw binary payload with AES-256-GCM.

        Args:
            data: Raw binary bytes to encrypt.
            associated_data: Authenticated associated data bound to ciphertext.

        Returns:
            Concatenated bytes: nonce (12 bytes) + ciphertext + tag.
        """
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError(f"Data must be bytes or bytearray, got {type(data)}")
        nonce = os.urandom(self.NONCE_LENGTH_BYTES)
        ciphertext = self._aesgcm.encrypt(nonce, bytes(data), associated_data)
        return nonce + ciphertext

    def decrypt_bytes(self, encrypted_bytes: bytes, associated_data: bytes = b"") -> bytes:
        """Decrypts and authenticates raw binary ciphertext with AES-256-GCM.

        Args:
            encrypted_bytes: Concatenated nonce (12 bytes) + ciphertext + tag.
            associated_data: Authenticated associated data bound during encryption.

        Returns:
            Decrypted plaintext bytes.

        Raises:
            ValueError: If payload length is shorter than the 12-byte nonce.
            InvalidTag: If ciphertext/tag is corrupted or associated data does not match.
        """
        if not isinstance(encrypted_bytes, (bytes, bytearray)):
            raise TypeError(f"Encrypted payload must be bytes, got {type(encrypted_bytes)}")
        if len(encrypted_bytes) < self.NONCE_LENGTH_BYTES:
            raise ValueError(
                f"Encrypted payload too short. Minimum length is {self.NONCE_LENGTH_BYTES} bytes."
            )
        nonce = encrypted_bytes[: self.NONCE_LENGTH_BYTES]
        ciphertext_and_tag = encrypted_bytes[self.NONCE_LENGTH_BYTES :]
        return self._aesgcm.decrypt(nonce, ciphertext_and_tag, associated_data)

    def encrypt_payload(
        self,
        plaintext: Union[str, bytes],
        conversation_id: Optional[str] = None,
        associated_data: bytes = b"",
    ) -> bytes:
        """Alias for encrypt supporting string or raw bytes and conversation_id associated data."""
        if isinstance(plaintext, bytes):
            text_str = plaintext.decode("utf-8", errors="replace")
        else:
            text_str = plaintext
        ad = associated_data or (conversation_id.encode("utf-8") if conversation_id else b"")
        return self.encrypt(text_str, associated_data=ad)

    def decrypt_payload(
        self,
        encrypted_bytes: Optional[bytes] = None,
        conversation_id: Optional[str] = None,
        associated_data: bytes = b"",
        ciphertext: Optional[bytes] = None,
    ) -> str:
        """Alias for decrypt supporting conversation_id associated data and ciphertext keyword."""
        data = encrypted_bytes if encrypted_bytes is not None else (ciphertext or b"")
        ad = associated_data or (conversation_id.encode("utf-8") if conversation_id else b"")
        return self.decrypt(data, associated_data=ad)



# Global singleton instance initialized with settings
_default_encryption_engine: Optional[ApplicationLayerEncryption] = None


def get_encryption_engine() -> ApplicationLayerEncryption:
    """Returns the default singleton ApplicationLayerEncryption engine."""
    global _default_encryption_engine
    if _default_encryption_engine is None:
        _default_encryption_engine = ApplicationLayerEncryption()
    return _default_encryption_engine

