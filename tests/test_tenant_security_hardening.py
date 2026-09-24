from __future__ import annotations

import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from api.routers import campaigns, settings


def test_legacy_log_export_requires_campaign_ownership(monkeypatch):
    app = FastAPI()
    app.include_router(campaigns.router)
    app.dependency_overrides[campaigns.get_db] = lambda: object()
    app.dependency_overrides[campaigns.get_current_user_id] = lambda: "tenant-a"
    monkeypatch.setattr(campaigns.db, "get_campaign", lambda *_args: None)

    response = TestClient(app).get("/api/campaigns/42/logs/export")

    assert response.status_code == 404


def test_legacy_log_export_streams_without_a_shared_file(monkeypatch):
    app = FastAPI()
    app.include_router(campaigns.router)
    app.dependency_overrides[campaigns.get_db] = lambda: object()
    app.dependency_overrides[campaigns.get_current_user_id] = lambda: "tenant-a"
    monkeypatch.setattr(campaigns.db, "get_campaign", lambda *_args: {"id": 42})
    monkeypatch.setattr(
        campaigns,
        "send_log_dataframe",
        lambda *_args, **_kwargs: pd.DataFrame([{"recipient_email": "lead@example.com", "status": "sent"}]),
    )

    response = TestClient(app).get("/api/campaigns/42/logs/export")

    assert response.status_code == 200
    assert response.headers["content-disposition"] == 'attachment; filename="campaign_42_send_log.csv"'
    assert response.text.replace("\r\n", "\n") == "recipient_email,status\nlead@example.com,sent\n"


def test_shared_oauth_routes_require_the_admin_dependency():
    app = FastAPI()
    app.include_router(settings.router)

    def deny_non_admin():
        raise HTTPException(status_code=403, detail="Administrator access is required")

    app.dependency_overrides[settings.require_admin_user] = deny_non_admin
    client = TestClient(app)

    status_response = client.get("/api/oauth/status")
    save_response = client.post(
        "/api/oauth/save-credentials-json",
        json={"content": '{"web":{"client_id":"id","client_secret":"secret"}}'},
    )

    assert status_response.status_code == 403
    assert save_response.status_code == 403
