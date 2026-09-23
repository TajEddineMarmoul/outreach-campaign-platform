from __future__ import annotations

import hashlib
import hmac
import time

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from starlette.requests import Request

from api import auth


def credentials(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def request(method: str = "GET", path: str = "/api/campaigns", headers: dict[str, str] | None = None) -> Request:
    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "headers": [(name.lower().encode(), value.encode()) for name, value in (headers or {}).items()],
            "query_string": b"",
        }
    )


def signed_request(secret: str, user_id: str, *, method: str = "GET", path: str = "/api/campaigns", timestamp: int | None = None) -> Request:
    value = str(int(time.time()) if timestamp is None else timestamp)
    signature = hmac.new(
        secret.encode(),
        auth._identity_message(timestamp=value, method=method, path=path, user_id=user_id),
        hashlib.sha256,
    ).hexdigest()
    return request(
        method,
        path,
        {
            "x-backend-user-id": user_id,
            "x-backend-auth-timestamp": value,
            "x-backend-auth-signature": signature,
        },
    )


def test_signed_identity_maps_to_the_specific_clerk_user(monkeypatch):
    monkeypatch.setattr(auth, "BACKEND_IDENTITY_SECRET", "unit-test-secret")
    monkeypatch.setattr(auth, "IS_PRODUCTION", True)

    assert auth.get_current_user_id(signed_request("unit-test-secret", "user_clerk_a")) == "user_clerk_a"
    assert auth.get_current_user_id(signed_request("unit-test-secret", "user_clerk_b")) == "user_clerk_b"


def test_signed_identity_rejects_an_assertion_for_another_route(monkeypatch):
    monkeypatch.setattr(auth, "BACKEND_IDENTITY_SECRET", "unit-test-secret")
    monkeypatch.setattr(auth, "IS_PRODUCTION", True)
    signed_for_list = signed_request("unit-test-secret", "user_clerk_a", path="/api/campaigns")
    replayed_request = request(
        "GET",
        "/api/campaigns/42",
        dict(signed_for_list.headers),
    )

    with pytest.raises(HTTPException) as exc_info:
        auth.get_current_user_id(replayed_request)

    assert exc_info.value.status_code == 401


def test_signed_identity_rejects_expired_assertion(monkeypatch):
    monkeypatch.setattr(auth, "BACKEND_IDENTITY_SECRET", "unit-test-secret")
    monkeypatch.setattr(auth, "IS_PRODUCTION", True)

    with pytest.raises(HTTPException) as exc_info:
        auth.get_current_user_id(
            signed_request(
                "unit-test-secret",
                "user_clerk_a",
                timestamp=int(time.time()) - auth.IDENTITY_MAX_AGE_SECONDS - 1,
            )
        )

    assert exc_info.value.status_code == 401


def test_production_rejects_legacy_shared_bearer_token(monkeypatch):
    monkeypatch.setattr(auth, "BACKEND_IDENTITY_SECRET", "unit-test-secret")
    monkeypatch.setattr(auth, "IS_PRODUCTION", True)

    with pytest.raises(HTTPException) as exc_info:
        auth.get_current_user_id(request(), credentials("legacy-shared-token"))

    assert exc_info.value.status_code == 401


def test_production_requires_identity_secret(monkeypatch):
    monkeypatch.setattr(auth, "BACKEND_IDENTITY_SECRET", "")
    monkeypatch.setattr(auth, "IS_PRODUCTION", True)

    with pytest.raises(HTTPException) as exc_info:
        auth.get_current_user_id(request())

    assert exc_info.value.status_code == 503


def test_mock_tokens_remain_available_only_outside_production(monkeypatch):
    monkeypatch.setattr(auth, "BACKEND_IDENTITY_SECRET", "")
    monkeypatch.setattr(auth, "IS_PRODUCTION", False)
    assert auth.get_current_user_id(request(), credentials("mock_test_user")) == "mock_test_user"

    monkeypatch.setattr(auth, "IS_PRODUCTION", True)
    with pytest.raises(HTTPException) as exc_info:
        auth.get_current_user_id(request(), credentials("mock_test_user"))

    assert exc_info.value.status_code == 503


def test_local_development_identity_maps_to_configured_user(monkeypatch):
    monkeypatch.setattr(auth, "BACKEND_IDENTITY_SECRET", "")
    monkeypatch.setattr(auth, "LOCAL_DEV_USER_ID", "production-user")
    monkeypatch.setattr(auth, "IS_PRODUCTION", False)

    assert auth.get_current_user_id(request(), credentials("local_dev_production-user")) == "production-user"


def test_local_development_identity_is_rejected_in_production(monkeypatch):
    monkeypatch.setattr(auth, "BACKEND_IDENTITY_SECRET", "unit-test-secret")
    monkeypatch.setattr(auth, "LOCAL_DEV_USER_ID", "production-user")
    monkeypatch.setattr(auth, "IS_PRODUCTION", True)

    with pytest.raises(HTTPException) as exc_info:
        auth.get_current_user_id(request(), credentials("local_dev_production-user"))

    assert exc_info.value.status_code == 401
