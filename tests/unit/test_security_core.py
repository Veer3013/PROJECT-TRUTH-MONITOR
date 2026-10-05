from __future__ import annotations

import json
import logging

from app.core.config import Settings
from app.core.logging import JsonFormatter
from app.core.security import (
    create_session_token,
    csrf_token_for_session,
    csrf_token_matches,
    hash_password,
    read_session_token,
    verify_password,
)


def test_password_hash_is_modern_and_verifies() -> None:
    encoded = hash_password("A long local password!2026")
    assert encoded.startswith("$argon2")
    assert verify_password("A long local password!2026", encoded)
    assert not verify_password("wrong password", encoded)


def test_signed_session_and_csrf_are_bound_together() -> None:
    settings = Settings(
        environment="test",
        secret_key="a-test-secret-that-is-long-enough-to-be-safe",
        database_url="sqlite:///:memory:",
    )
    token = create_session_token("user-123", settings)
    identity = read_session_token(token, settings)
    assert identity is not None and identity.user_id == "user-123"
    csrf = csrf_token_for_session(token, settings)
    assert csrf_token_matches(token, csrf, settings)
    assert not csrf_token_matches(token + "tampered", csrf, settings)


def test_json_log_formatter_emits_safe_request_correlation_fields() -> None:
    record = logging.LogRecord(
        name="projecttruth.http",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="request_completed",
        args=(),
        exc_info=None,
    )
    record.request_id = "request-test-123"
    record.method = "GET"
    record.path = "/health/ready"
    record.status_code = 200
    record.duration_ms = 12.5
    payload = json.loads(JsonFormatter().format(record))
    assert payload["request_id"] == "request-test-123"
    assert payload["method"] == "GET"
    assert payload["path"] == "/health/ready"
    assert payload["status_code"] == 200
    assert payload["duration_ms"] == 12.5
