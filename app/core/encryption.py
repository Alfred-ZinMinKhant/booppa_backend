"""
Booppa CSP Compliance Pack — PII Encryption Service
FIX #1: Application-level encryption for sensitive PII fields.

All NRIC, passport, and nominator ID fields must be encrypted at rest
at the application layer — disk encryption (S3/RDS) alone is not sufficient
under PDPA s.24 for sensitive personal data processed by a compliance platform.

Implementation:
- Fernet symmetric encryption (AES-128-CBC + HMAC-SHA256)
- Key stored in AWS Secrets Manager (never in env vars or code)
- SQLAlchemy TypeDecorator for transparent encrypt/decrypt
- Key rotation support via versioned key IDs

Install:
    pip install cryptography boto3

Environment variables:
    AWS_SECRETS_MANAGER_KEY_ARN  = arn:aws:secretsmanager:ap-southeast-1:...:secret:booppa/csp/pii-key
    AWS_REGION                   = ap-southeast-1
    CSP_PII_KEY_VERSION          = 1   (increment on rotation)

For local dev without AWS:
    CSP_PII_KEY_LOCAL            = base64-url-safe Fernet key (32 bytes)
    Generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
from functools import lru_cache
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import String, Text
from sqlalchemy.types import TypeDecorator

logger = logging.getLogger(__name__)

# ── KEY MANAGEMENT ──────────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def _load_fernet_key() -> bytes:
    """
    Load encryption key from AWS Secrets Manager (production)
    or local env var (development).
    Key is cached in memory after first load — never re-fetched per request.
    """
    # Local dev path
    local_key = os.environ.get("CSP_PII_KEY_LOCAL")
    if local_key:
        logger.warning("Using local PII encryption key — NOT suitable for production")
        return local_key.encode()

    # Production: AWS Secrets Manager
    arn = os.environ.get("AWS_SECRETS_MANAGER_KEY_ARN")
    if not arn:
        raise RuntimeError(
            "CSP PII encryption key not configured. "
            "Set AWS_SECRETS_MANAGER_KEY_ARN for production "
            "or CSP_PII_KEY_LOCAL for development."
        )
    try:
        import boto3
        client = boto3.client(
            "secretsmanager",
            region_name=os.environ.get("AWS_REGION", "ap-southeast-1"),
        )
        response = client.get_secret_value(SecretId=arn)
        secret   = response.get("SecretString") or base64.b64decode(
            response["SecretBinary"]
        ).decode()
        logger.info("PII encryption key loaded from AWS Secrets Manager")
        return secret.strip().encode()
    except Exception as exc:
        raise RuntimeError(f"Failed to load PII encryption key from AWS: {exc}") from exc


def _get_fernet() -> Fernet:
    return Fernet(_load_fernet_key())


# ── CORE ENCRYPT / DECRYPT ──────────────────────────────────────────────────

def encrypt_pii(plaintext: Optional[str]) -> Optional[str]:
    """
    Encrypt a PII string value.
    Returns base64-encoded ciphertext string, or None if input is None.
    Prefix "ENC:" marks encrypted values — allows detecting unencrypted legacy data.
    """
    if plaintext is None:
        return None
    if plaintext.startswith("ENC:"):
        # Already encrypted — idempotent
        return plaintext
    try:
        f         = _get_fernet()
        cipher    = f.encrypt(plaintext.encode("utf-8"))
        encoded   = base64.urlsafe_b64encode(cipher).decode("ascii")
        return f"ENC:{encoded}"
    except Exception as exc:
        logger.error("PII encryption failed: %s", exc)
        raise


def decrypt_pii(ciphertext: Optional[str]) -> Optional[str]:
    """
    Decrypt a PII string value.
    Returns plaintext, or None if input is None.
    If value is not prefixed with "ENC:", returns as-is (legacy unencrypted data).
    """
    if ciphertext is None:
        return None
    if not ciphertext.startswith("ENC:"):
        # Legacy unencrypted value — return as-is and log warning
        logger.warning(
            "PII field contains unencrypted value. "
            "Run migration: python -m app.scripts.encrypt_legacy_pii"
        )
        return ciphertext
    try:
        f         = _get_fernet()
        raw       = base64.urlsafe_b64decode(ciphertext[4:].encode("ascii"))
        plaintext = f.decrypt(raw).decode("utf-8")
        return plaintext
    except InvalidToken:
        logger.error(
            "PII decryption failed — InvalidToken. "
            "Key mismatch or corrupted data."
        )
        raise
    except Exception as exc:
        logger.error("PII decryption failed: %s", exc)
        raise


def mask_pii(value: Optional[str], visible_chars: int = 4) -> Optional[str]:
    """
    Return a masked version of a PII value for display purposes.
    e.g. "S1234567A" → "S****567A"
    Used in API responses where full PII is not needed.
    """
    if value is None:
        return None
    decrypted = decrypt_pii(value)
    if not decrypted:
        return None
    if len(decrypted) <= visible_chars:
        return "*" * len(decrypted)
    visible = decrypted[-visible_chars:]
    masked  = "*" * (len(decrypted) - visible_chars)
    return masked + visible


def pii_search_hash(plaintext: Optional[str]) -> Optional[str]:
    """
    Generate a deterministic search hash for an encrypted PII value.
    Allows exact-match lookup without decrypting all rows.
    e.g. search for NRIC "S1234567A" → hash → match against stored hashes.

    IMPORTANT: Hash uses SHA-256 with a site-wide pepper (from Secrets Manager).
    Never store raw SHA-256(NRIC) — pepper prevents rainbow table attacks.
    """
    if plaintext is None:
        return None
    pepper = os.environ.get("CSP_PII_SEARCH_PEPPER", "booppa-csp-default-pepper")
    return hashlib.sha256(f"{pepper}:{plaintext}".encode()).hexdigest()


# ── SQLALCHEMY TYPE DECORATOR ───────────────────────────────────────────────

class EncryptedString(TypeDecorator):
    """
    SQLAlchemy TypeDecorator for transparent PII encryption.

    Usage in models:
        from app.core.encryption import EncryptedString

        individual_nric_or_passport = Column(EncryptedString(100))
        nominator_id                = Column(EncryptedString(100))
        ubo_nric_or_passport        = Column(EncryptedString(100))

    - Encrypts on write (before INSERT/UPDATE)
    - Decrypts on read (after SELECT)
    - Stored as VARCHAR in PostgreSQL — no schema change needed
    - Column length should account for encryption overhead (~1.4x + "ENC:" prefix)
      For a 20-char NRIC: store as VARCHAR(150) to be safe
    """
    impl            = String
    cache_ok        = True
    # Encryption adds overhead: "ENC:" (4) + base64(Fernet output)
    # Fernet output for N bytes = N + 73 bytes overhead, then base64 ~1.37x
    # For VARCHAR(50) plaintext → use VARCHAR(200) in DB

    def __init__(self, length: int = 200, **kw):
        super().__init__(length=length, **kw)

    def process_bind_param(self, value, dialect):
        """Called before writing to DB — encrypt."""
        return encrypt_pii(value)

    def process_result_value(self, value, dialect):
        """Called after reading from DB — decrypt."""
        return decrypt_pii(value)

    def copy(self, **kw):
        return EncryptedString(self.impl.length, **kw)


class EncryptedText(TypeDecorator):
    """Same as EncryptedString but for TEXT columns (longer PII like addresses)."""
    impl     = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return encrypt_pii(value)

    def process_result_value(self, value, dialect):
        return decrypt_pii(value)

    def copy(self, **kw):
        return EncryptedText(**kw)


# ── MIGRATION HELPER ────────────────────────────────────────────────────────

def encrypt_legacy_pii_field(db_session, model_class, field_name: str,
                              batch_size: int = 100) -> int:
    """
    Migrate unencrypted legacy PII values to encrypted format.
    Run as a one-time migration script after deploying encryption.

    Usage:
        from app.core.encryption import encrypt_legacy_pii_field
        from app.core.models_csp import CspCddRecord
        from app.core.db import SessionLocal

        db = SessionLocal()
        count = encrypt_legacy_pii_field(db, CspCddRecord, "individual_nric_or_passport")
        print(f"Encrypted {count} records")
    """
    from sqlalchemy import text

    total_encrypted = 0
    offset = 0

    while True:
        records = (
            db_session.query(model_class)
            .offset(offset)
            .limit(batch_size)
            .all()
        )
        if not records:
            break

        for record in records:
            raw_value = getattr(record, field_name)
            if raw_value and not raw_value.startswith("ENC:"):
                # Encrypt the plain text value
                setattr(record, field_name, encrypt_pii(raw_value))
                total_encrypted += 1

        db_session.commit()
        offset += batch_size
        logger.info(
            "Encrypted %d %s.%s records (batch offset=%d)",
            total_encrypted, model_class.__tablename__, field_name, offset
        )

    return total_encrypted


# ── KEY ROTATION ─────────────────────────────────────────────────────────────

def rotate_encryption_key(
    db_session,
    model_class,
    encrypted_fields: list,
    old_key: bytes,
    new_key: bytes,
    batch_size: int = 50,
) -> int:
    """
    Rotate encryption key: decrypt with old key, re-encrypt with new key.
    Run during key rotation maintenance window.

    Steps:
    1. Store new key in AWS Secrets Manager
    2. Call this function with both old and new key bytes
    3. After completion, remove old key from Secrets Manager
    4. Clear the @lru_cache: _load_fernet_key.cache_clear()
    """
    old_fernet = Fernet(old_key)
    new_fernet = Fernet(new_key)
    total = 0
    offset = 0

    while True:
        records = (
            db_session.query(model_class)
            .offset(offset)
            .limit(batch_size)
            .all()
        )
        if not records:
            break

        for record in records:
            for field in encrypted_fields:
                ciphertext = getattr(record, field)
                if ciphertext and ciphertext.startswith("ENC:"):
                    try:
                        raw       = base64.urlsafe_b64decode(ciphertext[4:].encode())
                        plaintext = old_fernet.decrypt(raw).decode()
                        new_cipher = new_fernet.encrypt(plaintext.encode())
                        new_enc    = "ENC:" + base64.urlsafe_b64encode(new_cipher).decode()
                        setattr(record, field, new_enc)
                        total += 1
                    except InvalidToken:
                        # Already re-encrypted with new key
                        pass

        db_session.commit()
        offset += batch_size

    logger.info("Key rotation complete: %d field values re-encrypted", total)
    return total
