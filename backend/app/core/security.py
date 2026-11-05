"""Security and Authentication Utilities.

Provides secure password hashing via pwdlib[bcrypt] compatible with Python 3.12,
per ADR-07 in TECHNOLOGY_DECISIONS.md.
"""

from pwdlib import PasswordHash
from pwdlib.hashers.bcrypt import BcryptHasher

# Configure password hasher with BcryptHasher (recommended for modern Python 3.12)
password_hash = PasswordHash((BcryptHasher(),))


def hash_password(password: str) -> str:
    """Hashes a plaintext password using bcrypt with constant-time protection."""
    if not password:
        raise ValueError("Password cannot be empty.")
    return password_hash.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verifies a plaintext password against a stored hash in constant time."""
    if not plain_password or not hashed_password:
        return False
    return password_hash.verify(plain_password, hashed_password)

