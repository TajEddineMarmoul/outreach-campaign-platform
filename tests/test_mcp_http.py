"""The hosted MCP transport must preserve the authenticated workspace identity."""

from __future__ import annotations

import hashlib
import hmac
import time

from fastapi.testclient import TestClient

import api.auth as auth
import api.deps as deps
import api.main as main
import outreach_mcp.server as mcp_server
from api.routers import campaigns as campaigns_router

SIGNATURE_KEY = "test-mcp-signature"


def _headers(user_id: str, *, signature_key: str) -> dict[str, str]:
    timestamp = str(int(time.time()))
    signature = hmac.new(
        signature_key.encode("utf-8"),
        auth._identity_message(
            timestamp=timestamp,
            method="POST",
            path="/mcp",
            user_id=user_id,
            role="member",
        ),
        hashlib.sha256,
    ).hexdigest()
    return {
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
        "x-backend-user-id": user_id,
        "x-backend-user-role": "member",
        "x-backend-auth-timestamp": timestamp,
        "x-backend-auth-signature": signature,
    }


def _mcp_request() -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "workspace_overview", "arguments": {"limit": 1}},
    }


def test_http_mcp_requires_signed_identity_and_binds_user(monkeypatch):
    monkeypatch.setattr(auth, "BACKEND_IDENTITY_SECRET", SIGNATURE_KEY)
    monkeypatch.setattr(auth, "IS_PRODUCTION", True)
    monkeypatch.setattr(main, "IS_PRODUCTION", True)

    def fake_api(method, path, *, params=None, body=None):
        assert mcp_server._bound_workspace_user.get() == "user_one"
        if path == "/api/campaigns":
            return [{"id": 1, "name": "Own campaign", "status": "draft"}]
        if path == "/api/sender-groups":
            return []
        if path == "/api/settings":
            return {"timezone": "UTC"}
        raise AssertionError(f"Unexpected API path: {path}")

    monkeypatch.setattr(mcp_server, "_api", fake_api)
    request = _mcp_request()
    with TestClient(main.app) as client:
        missing = client.post("/mcp", json=request)
        assert missing.status_code == 401

        forged = client.post("/mcp", json=request, headers=_headers("user_one", signature_key="wrong"))
        assert forged.status_code == 401

        valid = client.post("/mcp", json=request, headers=_headers("user_one", signature_key=SIGNATURE_KEY))
        assert valid.status_code == 200
        result = valid.json()["result"]
        assert result.get("isError") is not True
        assert "Own campaign" in result["content"][0]["text"]

    assert mcp_server._bound_workspace_user.get() is None


def test_hosted_api_calls_resolve_the_bound_workspace(monkeypatch):
    """A hosted tool call must reach the API as the workspace bound to it."""

    monkeypatch.setattr(auth, "BACKEND_IDENTITY_SECRET", SIGNATURE_KEY)
    monkeypatch.setattr(auth, "IS_PRODUCTION", True)
    monkeypatch.setattr(main, "IS_PRODUCTION", True)
    mcp_server._hosted_client.cache_clear()

    resolved: list[str] = []
    monkeypatch.setattr(deps.db, "list_campaigns", lambda conn, user_id: resolved.append(user_id) or [])
    main.app.dependency_overrides[deps.get_db] = lambda: None
    try:
        with TestClient(main.app) as client:
            with mcp_server.bound_workspace_user("user_one"):
                assert mcp_server._api("GET", "/api/campaigns") == []
            with mcp_server.bound_workspace_user("user_two"):
                assert mcp_server._api("GET", "/api/campaigns") == []

            # An assertion that expired before dispatch must be rejected.
            stale = mcp_server._hosted_headers("GET", "/api/campaigns", "user_one")
            stale["x-backend-auth-timestamp"] = str(int(time.time()) - 3600)
            assert client.get("/api/campaigns", headers=stale).status_code == 401
    finally:
        main.app.dependency_overrides.clear()

    assert resolved == ["user_one", "user_two"]
    assert mcp_server._bound_workspace_user.get() is None
