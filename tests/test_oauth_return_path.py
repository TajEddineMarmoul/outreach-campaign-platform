from datetime import timedelta
from urllib.parse import parse_qs, urlsplit
from unittest.mock import MagicMock
import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, select, text
from alembic.migration import MigrationContext
from alembic.operations import Operations

from api.routers import oauth as callback
from src.platform import oauth
from src.platform.models import Campaign, OAuthState, Sender
from src.platform.time import utcnow
from test_shared_sender_groups import USER, setup  # noqa: F401 - shared isolated SQLite fixture


@pytest.fixture
def flow_setup(setup, tmp_path, monkeypatch):
    client, session, a, b, first, second, foreign = setup
    client.app.include_router(callback.router)
    campaign = Campaign(user_id=USER, name="Return to this campaign", status="paused")
    private = Campaign(user_id="another-owner", name="Private campaign")
    session.add_all([campaign, private])
    session.commit()
    credentials = tmp_path / "google-client.json"
    credentials.write_text("{}")
    start = MagicMock(code_verifier="pkce-test")
    start.authorization_url.return_value = ("https://accounts.google.com/test", None)
    monkeypatch.setattr(oauth, "credentials_file_path", lambda: credentials)
    monkeypatch.setattr(oauth.Flow, "from_client_secrets_file", lambda *args, **kwargs: start)
    exchange = MagicMock()
    exchange.credentials.scopes = ["https://www.googleapis.com/auth/gmail.send"]
    exchange.credentials.to_json.return_value = '{"refresh_token":"test-only"}'
    monkeypatch.setattr(callback, "flow_from_state", lambda state: exchange)
    monkeypatch.setattr(callback, "oauth_email_for_credentials", lambda credentials: first.email)
    monkeypatch.setattr(callback, "encrypt_text", lambda value: "refreshed-test-session")
    return client, session, a, b, first, second, campaign, private, exchange


def begin(values, path=None):
    client, session, a, b, *_ = values
    response = client.post(f"/api/sender-groups/{b.id}/senders/oauth/start", **({"json": {"return_path": path}} if path else {}))
    assert response.status_code == 200, response.text
    return session.scalar(select(OAuthState).where(OAuthState.used_at.is_(None)).order_by(OAuthState.created_at.desc()))


def test_google_connection_returns_to_the_same_campaign_step_and_reuses_saved_account(flow_setup):
    client, session, a, b, first, second, campaign, _, exchange = flow_setup
    path = f"/campaigns/{campaign.id}?step=senders&sender_setup=1"
    state = begin(flow_setup, path)
    assert state.return_path == path
    response = client.get("/api/oauth/callback", params={"state": state.state, "code": "test-code"}, follow_redirects=False)
    target = urlsplit(response.headers["location"])
    assert target.path == f"/campaigns/{campaign.id}"
    assert parse_qs(target.query) == {"step": ["senders"], "sender_setup": ["1"], "oauth": ["success"], "group_id": [str(b.id)], "sender_id": [str(first.id)]}
    assert first.group_id == a.id and first.encrypted_oauth_credentials == "refreshed-test-session"
    assert [group.id for group in first.groups] == [b.id]
    assert second.encrypted_oauth_credentials == "another-encrypted-session"
    assert len(list(session.scalars(select(Sender).where(Sender.user_id == USER)))) == 2
    exchange.fetch_token.assert_called_once_with(code="test-code")
    assert state.used_at is not None
    replay = client.get("/api/oauth/callback", params={"state": state.state, "code": "test-code"}, follow_redirects=False)
    assert "oauth_state_expired" in replay.headers["location"]
    exchange.fetch_token.assert_called_once()


@pytest.mark.parametrize("query", [{"error": "access_denied"}, {}])
def test_cancel_and_missing_code_return_to_campaign_without_losing_credentials(flow_setup, query):
    client, _, _, _, first, _, campaign, *_ = flow_setup
    state = begin(flow_setup, f"/campaigns/{campaign.id}?tab=message&sender_setup=1")
    response = client.get("/api/oauth/callback", params={"state": state.state, **query}, follow_redirects=False)
    target = urlsplit(response.headers["location"])
    assert target.path == f"/campaigns/{campaign.id}" and parse_qs(target.query)["tab"] == ["message"]
    assert parse_qs(target.query)["oauth"] == ["error"]
    assert first.encrypted_oauth_credentials == "unchanged-encrypted-session"
    assert state.used_at is not None


@pytest.mark.parametrize("path", ["https://evil.test/send", "//evil.test/senders", "/\\evil.test", "/senders#x", "/campaigns/1/../../evil", "/settings", "/senders\nLocation:evil", "javascript:alert(1)"])
def test_oauth_return_cannot_redirect_outside_supported_app_pages(flow_setup, path):
    client, session, _, b, *_ = flow_setup
    response = client.post(f"/api/sender-groups/{b.id}/senders/oauth/start", json={"return_path": path})
    assert response.status_code == 422
    assert list(session.scalars(select(OAuthState))) == []


def test_return_campaign_must_belong_to_the_authenticated_user(flow_setup):
    client, session, _, b, _, _, _, private, _ = flow_setup
    response = client.post(f"/api/sender-groups/{b.id}/senders/oauth/start", json={"return_path": f"/campaigns/{private.id}?step=senders"})
    assert response.status_code == 404
    assert list(session.scalars(select(OAuthState))) == []


def test_legacy_start_without_body_still_returns_to_senders(flow_setup):
    client, *_ = flow_setup
    state = begin(flow_setup)
    response = client.get("/api/oauth/callback", params={"state": state.state, "code": "test"}, follow_redirects=False)
    assert urlsplit(response.headers["location"]).path == "/senders"


def test_expired_state_uses_safe_default_and_never_exchanges_tokens(flow_setup):
    client, session, _, _, first, _, campaign, _, exchange = flow_setup
    state = begin(flow_setup, f"/campaigns/{campaign.id}")
    state.expires_at = utcnow() - timedelta(seconds=1)
    session.commit()
    response = client.get("/api/oauth/callback", params={"state": state.state, "code": "test"}, follow_redirects=False)
    assert urlsplit(response.headers["location"]).path == "/senders"
    assert "oauth_state_expired" in response.headers["location"]
    exchange.fetch_token.assert_not_called()
    assert first.encrypted_oauth_credentials == "unchanged-encrypted-session"


def test_return_path_migration_preserves_pending_google_sessions(tmp_path):
    spec = importlib.util.spec_from_file_location("oauth_path_migration", Path(__file__).resolve().parents[1] / "alembic/versions/0017_oauth_return_path.py")
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine(f"sqlite:///{tmp_path / 'oauth-migration.db'}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE oauth_states (state TEXT PRIMARY KEY, code_verifier TEXT NOT NULL)"))
        connection.execute(text("INSERT INTO oauth_states VALUES ('pending-session', 'same-pkce-verifier')"))
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        assert list(connection.execute(text("SELECT * FROM oauth_states"))) == [("pending-session", "same-pkce-verifier", None)]
        assert next(column for column in inspect(connection).get_columns("oauth_states") if column["name"] == "return_path")["nullable"]
        migration.downgrade()
        assert list(connection.execute(text("SELECT * FROM oauth_states"))) == [("pending-session", "same-pkce-verifier")]
    engine.dispose()
