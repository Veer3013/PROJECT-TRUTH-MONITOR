from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.core.config import Settings, get_settings

_password_hasher = PasswordHasher(time_cost=2, memory_cost=19_456, parallelism=1)
SESSION_SALT = "projecttruth-session-v1"


def hash_password(password: str) -> str:
    if len(password) < 10:
        raise ValueError("Password must contain at least 10 characters")
    return _password_hasher.hash(password)


def verify_password(password: str, encoded: str) -> bool:
    try:
        return _password_hasher.verify(encoded, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


@dataclass(frozen=True)
class SessionIdentity:
    user_id: str
    issued_at: datetime


def _serializer(settings: Settings | None = None) -> URLSafeTimedSerializer:
    active = settings or get_settings()
    return URLSafeTimedSerializer(active.secret_key, salt=SESSION_SALT)


def create_session_token(user_id: str, settings: Settings | None = None) -> str:
    issued = int(datetime.now(UTC).timestamp())
    nonce = secrets.token_urlsafe(12)
    return _serializer(settings).dumps({"sub": user_id, "iat": issued, "nonce": nonce})


def read_session_token(token: str, settings: Settings | None = None) -> SessionIdentity | None:
    active = settings or get_settings()
    try:
        value: dict[str, Any] = _serializer(active).loads(
            token, max_age=active.session_max_age_seconds
        )
        issued_at = datetime.fromtimestamp(int(value["iat"]), tz=UTC)
        return SessionIdentity(user_id=str(value["sub"]), issued_at=issued_at)
    except (BadSignature, SignatureExpired, KeyError, TypeError, ValueError):
        return None


def csrf_token_for_session(session_token: str, settings: Settings | None = None) -> str:
    active = settings or get_settings()
    return hmac.new(
        active.secret_key.encode("utf-8"),
        f"csrf:{session_token}".encode(),
        hashlib.sha256,
    ).hexdigest()


def csrf_token_matches(session_token: str, submitted: str, settings: Settings | None = None) -> bool:
    expected = csrf_token_for_session(session_token, settings)
    return hmac.compare_digest(expected, submitted)


def new_idempotency_key() -> str:
    return secrets.token_urlsafe(24)

