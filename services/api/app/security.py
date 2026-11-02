import base64
import hashlib
import hmac
import secrets
from datetime import UTC, datetime

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from cryptography.fernet import Fernet

from .config import get_settings

password_hasher = PasswordHasher()


def _fernet() -> Fernet:
    key = base64.urlsafe_b64encode(hashlib.sha256(get_settings().session_secret.encode()).digest())
    return Fernet(key)


def encrypt_secret(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt_secret(value: str) -> str:
    return _fernet().decrypt(value.encode()).decode()


def hash_password(password: str) -> str:
    return password_hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return password_hasher.verify(password_hash, password)
    except VerificationError:
        return False


def create_import_signature(timestamp: str, nonce: str, body: bytes) -> str:
    return create_import_signature_for_source(timestamp, nonce, body, None)


def create_import_signature_for_source(
    timestamp: str, nonce: str, body: bytes, source: str | None
) -> str:
    message = b".".join([timestamp.encode(), nonce.encode(), body])
    return hmac.new(_import_secret(source).encode(), message, hashlib.sha256).hexdigest()


def _import_secret(source: str | None) -> str:
    settings = get_settings()
    configured = {}
    for pair in settings.import_hmac_secrets.split(","):
        if "=" in pair:
            key, value = pair.split("=", 1)
            configured[key.strip()] = value.strip()
    return configured.get(source or "", settings.import_hmac_secret)


def verify_import_signature(
    timestamp: str, nonce: str, signature: str, body: bytes, source: str | None = None
) -> bool:
    try:
        timestamp_value = int(timestamp)
    except ValueError:
        return False
    now = int(datetime.now(UTC).timestamp())
    if abs(now - timestamp_value) > get_settings().import_signature_ttl_seconds:
        return False
    expected = create_import_signature_for_source(timestamp, nonce, body, source)
    return hmac.compare_digest(expected, signature)


def new_token() -> str:
    return secrets.token_urlsafe(32)


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def hash_service_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
