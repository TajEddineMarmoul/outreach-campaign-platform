import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routers import templates


@pytest.fixture
def client(tmp_path):
    conn = sqlite3.connect(tmp_path / "library.db", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE templates (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, title TEXT, subject TEXT, body TEXT, updated_at TEXT)")
    app = FastAPI()
    app.include_router(templates.router)
    app.dependency_overrides[templates.get_db] = lambda: conn
    app.dependency_overrides[templates.get_current_user_id] = lambda: "owner"
    with TestClient(app) as client:
        yield client
    conn.close()


def test_campaign_email_round_trips_rich_formatting_links_and_exact_placeholders(client):
    draft = {"title": "Reusable email", "subject": "Hi {{ First Name }}", "body": '<p>Hello <strong>{{ First Name }}</strong>,</p><p><a href="https://example.test">Our work</a> at {{company_name}}.</p><ul><li>One</li><li>Two</li></ul>'}
    response = client.post("/api/templates", json=draft)
    assert response.status_code == 200
    saved = client.get("/api/templates").json()[0]
    assert {key: saved[key] for key in draft} == draft
    draft["body"] += '<p style="text-align: center">Next step</p>'
    assert client.patch(f"/api/templates/{saved['id']}", json=draft).status_code == 200
    assert client.get("/api/templates").json()[0]["body"] == draft["body"]


@pytest.mark.parametrize("field,value", [("title", "  "), ("subject", ""), ("body", ""), ("subject", "a" * 999), ("body", "x" * 500001)], ids=["blank-name", "blank-subject", "blank-body", "long-subject", "long-body"])
def test_invalid_template_cannot_replace_a_saved_email(client, field, value):
    good = {"title": "Keep", "subject": "Hello", "body": "<p>Keep</p>"}
    saved = client.post("/api/templates", json=good).json()
    assert client.patch(f"/api/templates/{saved['id']}", json={**good, field: value}).status_code == 422
    assert client.get("/api/templates").json()[0]["body"] == good["body"]


def test_saved_templates_are_isolated_by_owner(client):
    saved = client.post("/api/templates", json={"title": "Private", "subject": "Private", "body": "<p>Private</p>"}).json()
    client.app.dependency_overrides[templates.get_current_user_id] = lambda: "another-owner"
    assert client.get("/api/templates").json() == []
    assert client.patch(f"/api/templates/{saved['id']}", json={"title": "Hacked", "subject": "Hacked", "body": "Hacked"}).status_code == 404
    assert client.delete(f"/api/templates/{saved['id']}").status_code == 200
    client.app.dependency_overrides[templates.get_current_user_id] = lambda: "owner"
    assert client.get("/api/templates").json()[0]["title"] == "Private"
