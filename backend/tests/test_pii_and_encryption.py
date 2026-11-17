"""Test PII Masking Engine and AES-256-GCM Authenticated Encryption Primitives."""

from datetime import datetime, timezone
import pytest
from cryptography.exceptions import InvalidTag
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.encryption import ApplicationLayerEncryption
from app.core.pii import PIIMaskingEngine
from app.models.customer import Conversation, Customer, Message, Session


def test_pii_credit_card_masking():
    """Asserts that valid Luhn credit cards are masked while non-cards are preserved."""
    # Valid Visa test card (4111 1111 1111 1111)
    card_text = "Please refund to my card 4111 1111 1111 1111 as agreed."
    masked = PIIMaskingEngine.mask_text(card_text)
    assert "****-****-****-1111" in masked
    assert "4111 1111 1111" not in masked

    # Arbitrary sequence of numbers that fails Luhn check
    fake_num = "Order reference 1234 5678 9012 3456 failed."
    not_a_card = PIIMaskingEngine.mask_text(fake_num)
    assert "1234 5678 9012 3456" in not_a_card


def test_pii_email_and_phone_and_ssn_masking():
    """Asserts that emails, phones, and SSNs are appropriately redacted."""
    text = "Contact john.doe@enterprise.com or call +1 (555) 234-5678, SSN 123-45-6789."
    masked = PIIMaskingEngine.mask_text(text)
    assert "j***e@enterprise.com" in masked
    assert "[PHONE_REDACTED]" in masked
    assert "***-**-6789" in masked
    assert "john.doe@" not in masked
    assert "123-45-" not in masked


def test_payment_data_detection_gate():
    """Asserts that raw payment data is reliably identified to prevent vector indexing."""
    assert PIIMaskingEngine.contains_payment_data("My card is 4111 1111 1111 1111") is True
    assert PIIMaskingEngine.contains_payment_data("Security code CVV: 789 on the back") is True
    assert PIIMaskingEngine.contains_payment_data("My monitor does not turn on and smells like smoke") is False


def test_aes_gcm_encryption_roundtrip(encryption_engine: ApplicationLayerEncryption):
    """Verifies AES-256-GCM authenticated encryption and decryption roundtrip."""
    raw_utterance = "I want a refund for my damaged monitor. Card: 4111 1111 1111 1111."
    associated_meta = b"msg_id_turn_101"

    encrypted_payload = encryption_engine.encrypt(raw_utterance, associated_data=associated_meta)

    # Assert payload structure: 12-byte nonce + ciphertext + 16-byte tag
    assert len(encrypted_payload) > 12 + 16
    assert isinstance(encrypted_payload, bytes)

    decrypted = encryption_engine.decrypt(encrypted_payload, associated_data=associated_meta)
    assert decrypted == raw_utterance


def test_aes_gcm_nonce_uniqueness(encryption_engine: ApplicationLayerEncryption):
    """Verifies that encrypting the exact same plaintext produces unique nonces and ciphertexts."""
    text = "Deterministic plaintext for nonce test."
    enc1 = encryption_engine.encrypt(text)
    enc2 = encryption_engine.encrypt(text)

    # First 12 bytes are the random nonce
    nonce1 = enc1[:12]
    nonce2 = enc2[:12]
    assert nonce1 != nonce2
    assert enc1 != enc2


def test_aes_gcm_tamper_detection(encryption_engine: ApplicationLayerEncryption):
    """Asserts that modifying any byte in the ciphertext or tag raises InvalidTag."""
    text = "Sensitive customer utterance."
    encrypted = encryption_engine.encrypt(text)

    # Flip the last byte of the authentication tag
    tampered = bytearray(encrypted)
    tampered[-1] ^= 0x01

    with pytest.raises(InvalidTag):
        encryption_engine.decrypt(bytes(tampered))


def test_aes_gcm_associated_data_mismatch(encryption_engine: ApplicationLayerEncryption):
    """Asserts that decrypting with mismatched associated data fails authentication."""
    text = "Utterance with authenticated metadata."
    encrypted = encryption_engine.encrypt(text, associated_data=b"correct_id")

    with pytest.raises(InvalidTag):
        encryption_engine.decrypt(encrypted, associated_data=b"wrong_id")


@pytest.mark.asyncio
async def test_message_persistence_masked_vs_encrypted(
    session: AsyncSession, encryption_engine: ApplicationLayerEncryption
):
    """Tests that Message persists masked content by default and encrypted raw content securely."""
    customer = Customer(email="pii_test@example.com")
    session.add(customer)
    await session.flush()

    cust_session = Session(
        customer_id=customer.id,
        session_token="sess_pii_123",
        expires_at=datetime.now(timezone.utc),
    )
    session.add(cust_session)
    await session.flush()

    conv = Conversation(session_id=cust_session.id, customer_id=customer.id)
    session.add(conv)
    await session.flush()

    raw_text = "Please refund my card 4111 1111 1111 1111 immediately."
    masked_text = PIIMaskingEngine.mask_text(raw_text)
    encrypted_bytes = encryption_engine.encrypt(raw_text, associated_data=str(conv.id).encode("utf-8"))

    msg = Message(
        conversation_id=conv.id,
        sender_type="CUSTOMER",
        content=masked_text,  # Stored masked by default
        encrypted_raw_content=encrypted_bytes,  # Stored encrypted
    )
    session.add(msg)
    await session.commit()

    # Query back from DB
    result = await session.execute(select(Message).where(Message.id == msg.id))
    persisted_msg = result.scalar_one()

    # 1. Plaintext field is masked
    assert "4111 1111 1111" not in persisted_msg.content
    assert "****-****-****-1111" in persisted_msg.content

    # 2. Decrypting encrypted_raw_content recovers the original raw message
    decrypted_raw = encryption_engine.decrypt(
        persisted_msg.encrypted_raw_content, associated_data=str(conv.id).encode("utf-8")
    )
    assert decrypted_raw == raw_text


@pytest.mark.asyncio
async def test_pii_api_key_and_gemini_header_redaction(monkeypatch):
    """Asserts API keys and bearer tokens are redacted and Gemini Vision uses header auth without URL keys."""
    from unittest.mock import AsyncMock, MagicMock
    from app.modules.evidence.providers.gemini_provider import GeminiVisionOCRProvider

    secret = "AIzaSyFakeKey1234567890ABCDEFGHIJKLMN"
    noisy = f"POST https://generativelanguage.googleapis.com/v1beta/models/x:generateContent?key={secret} Bearer tok_1234567890abcdef"
    masked = PIIMaskingEngine.mask_text(noisy)
    assert secret not in masked
    assert "tok_1234567890abcdef" not in masked
    assert "[REDACTED]" in masked or "[API_KEY_REDACTED]" in masked

    captured = {}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, headers=None, json=None):
            captured["url"] = url
            captured["headers"] = headers or {}
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.json.return_value = {
                "candidates": [{"content": {"parts": [{"text": "Order ID: ORD-100"}]}}]
            }
            return resp

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    provider = GeminiVisionOCRProvider(api_key=secret, model_name="gemini-3.8-flash")
    res = await provider.extract_document(b"\xff\xd8\xff\xe0test", "image/jpeg", "receipt.jpg")
    assert "?key=" not in captured["url"]
    assert captured["headers"].get("x-goog-api-key") == secret
    assert "ORD-100" in res.raw_text


