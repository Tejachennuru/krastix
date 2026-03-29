import os
import base64
import hashlib
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

_PREFIX = "enc:v1:"


def _get_cipher() -> Fernet:
    key = os.getenv("INTEGRATIONS_ENCRYPTION_KEY", "").strip()
    if not key:
        raise RuntimeError("INTEGRATIONS_ENCRYPTION_KEY is not configured")

    # Preferred mode: already a valid Fernet key.
    try:
        return Fernet(key.encode("utf-8"))
    except Exception:
        # Compatibility mode: derive a deterministic Fernet key from any secret string.
        digest = hashlib.sha256(key.encode("utf-8")).digest()
        derived = base64.urlsafe_b64encode(digest)
        return Fernet(derived)


def encrypt_secret(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    plain = value.strip()
    if not plain:
        return ""
    cipher = _get_cipher()
    token = cipher.encrypt(plain.encode("utf-8")).decode("utf-8")
    return f"{_PREFIX}{token}"


def decrypt_secret(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return ""

    if not text.startswith(_PREFIX):
        # Backward compatibility for existing plaintext rows.
        return text

    cipher = _get_cipher()
    raw = text[len(_PREFIX):]
    try:
        return cipher.decrypt(raw.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise RuntimeError("Failed to decrypt integration secret") from exc


def is_encrypted_value(value: Optional[str]) -> bool:
    return bool(value and value.startswith(_PREFIX))
