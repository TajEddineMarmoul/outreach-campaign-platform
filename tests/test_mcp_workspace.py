from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from mcp import Client
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from api import auth
from api.routers.audience import _filtered_page
from api.routers.workspace_tokens import TokenCreate, create_token, list_tokens, revoke_token
from outreach_mcp import server as outreach_server
from src.platform import db as platform_db
from src.platform.models import Base, User, WorkspaceAccessToken
from src.platform.time import utcnow


def _request() -> Request:
    return Request({"type": "http", "method": "GET", "path": "/api/campaigns", "headers": [], "query_string": b""})


def test_workspace_token_is_user_scoped_and_revocable(monkeypatch):
    engine = create_engine("sqlite+pysqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[User.__table__, WorkspaceAccessToken.__table__])
    session_factory = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(platform_db, "SessionLocal", session_factory)
    monkeypatch.setattr(auth, "IS_PRODUCTION", True)
    monkeypatch.setattr(auth, "BACKEND_IDENTITY_SECRET", "test-secret")

    with session_factory() as session:
        session.add_all([User(id="user_a"), User(id="user_b")])
        session.commit()
        issued = create_token(TokenCreate(name="Codex"), session=session, user_id="user_a")
        assert issued["token"].startswith("outreach_mcp_")
        assert "token" not in list_tokens(session=session, user_id="user_a")[0]
        assert list_tokens(session=session, user_id="user_b") == []

    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=issued["token"])
    principal = auth.get_current_principal(_request(), credentials)
    assert principal.user_id == "user_a"
    assert not principal.is_admin
    with pytest.raises(HTTPException) as exc:
        auth.require_interactive_user(principal)
    assert exc.value.status_code == 403

    with session_factory() as session:
        revoke_token(issued["id"], session=session, user_id="user_a")
    with pytest.raises(HTTPException) as exc:
        auth.get_current_principal(_request(), credentials)
    assert exc.value.status_code == 401

    engine.dispose()


def test_expired_workspace_token_rejected(monkeypatch):
    engine = create_engine("sqlite+pysqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[User.__table__, WorkspaceAccessToken.__table__])
    session_factory = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(platform_db, "SessionLocal", session_factory)
    with session_factory() as session:
        session.add(User(id="user_a"))
        session.commit()
        issued = create_token(TokenCreate(name="Expired"), session=session, user_id="user_a")
        token = session.get(WorkspaceAccessToken, issued["id"])
        token.expires_at = utcnow() - timedelta(seconds=1)
        session.commit()
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=issued["token"])
    with pytest.raises(HTTPException) as exc:
        auth.get_current_principal(_request(), credentials)
    assert exc.value.status_code == 401
    engine.dispose()


def test_local_mock_identity_cannot_mint_workspace_tokens(monkeypatch):
    monkeypatch.setattr(auth, "IS_PRODUCTION", False)
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="mock_user_a")
    principal = auth.get_current_principal(_request(), credentials)
    with pytest.raises(HTTPException) as exc:
        auth.require_interactive_user(principal)
    assert exc.value.status_code == 403


def test_audience_filter_and_facets_use_effective_status():
    rows = [
        {"id": 1, "email_normalized": "a@example.com", "company_name": "Acme", "title": "Recruiter", "industry": "Tech", "country": "US", "source_type": "csv", "status": "approved", "recipient_status": "sent", "custom_fields": '{"seniority":"senior"}'},
        {"id": 2, "email_normalized": "b@example.com", "company_name": "Acme", "title": "Engineer", "industry": "Tech", "country": "US", "source_type": "sheet", "status": "pending", "recipient_status": "approved", "custom_fields": '{"seniority":"junior"}'},
    ]
    result = _filtered_page(rows, search="", field="seniority", value="senior", status="sent", source_type="", page=1, page_size=50, include_facets=True)
    assert result["total"] == 1
    assert result["items"][0]["contact_id"] == 1
    assert result["facets"]["title"] == [{"value": "Recruiter", "count": 1}]
    assert result["facets"]["custom_fields"] == [{"name": "seniority", "count": 1}]


def test_mcp_exposes_compact_tools_and_partial_setup_results(monkeypatch):
    calls = []

    def fake_api(method, path, *, params=None, body=None):
        calls.append((method, path, body))
        if path == "/api/campaigns" and method == "GET":
            return [{"id": 7, "name": "Test", "status": "draft", "recipient_count": 0, "sent_count": 0}]
        if path == "/api/sender-groups":
            return []
        if path == "/api/settings":
            return {"timezone": "UTC"}
        if method == "GET" and path == "/api/campaigns/7":
            return {"id": 7, "name": "Test", "status": "draft"}
        if path.endswith("/sender-group"):
            raise RuntimeError("Sender group not found")
        return {"status": "success"}

    monkeypatch.setattr(outreach_server, "_api", fake_api)
    result = outreach_server.update_campaign_setup(7, subject="Hello", sender_group_id=999)
    assert result == {
        "campaign_id": 7,
        "completed": ["campaign_content"],
        "error": "Sender group not found",
        "partial": True,
    }
    assert ("PATCH", "/api/campaigns/7", {"subject_template": "Hello"}) in calls

    async def exercise_server():
        async with Client(outreach_server.mcp) as client:
            names = {tool.name for tool in (await client.list_tools()).tools}
            overview = await client.call_tool("workspace_overview", {})
            assert not overview.is_error
            return names, json.loads(overview.content[0].text)

    names, overview = asyncio.run(exercise_server())
    assert {"workspace_overview", "inspect_campaign", "search_audience", "create_campaign", "launch_campaign", "control_campaign"} <= names
    assert overview["campaigns"][0]["id"] == 7
