from __future__ import annotations

import os
import secrets
import re
from datetime import timedelta
from urllib.parse import urlsplit

from google_auth_oauthlib.flow import Flow
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.gmail_sender import SCOPES, credentials_file_path
from src.platform.models import Campaign, OAuthState
from src.platform.services import ensure_user, require_group
from src.platform.time import utcnow


FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000")
BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000")
OAUTH_STATE_TTL = timedelta(minutes=15)


def callback_url() -> str:
    return f"{BACKEND_URL}/api/oauth/callback"


def validate_return_path(value: str | None) -> str:
    path = value or "/senders"
    parsed = urlsplit(path)
    if (len(path) > 2048 or not path.startswith("/") or path.startswith("//")
            or parsed.scheme or parsed.netloc or parsed.fragment or "\\" in path
            or any(ord(char) < 32 or ord(char) == 127 for char in path)
            or not (parsed.path == "/senders" or re.fullmatch(r"/campaigns/[1-9][0-9]*", parsed.path))):
        raise ValueError("Return to a sender page or campaign in this app")
    return path


def create_sender_oauth_start(session: Session, *, user_id: str, group_id: int, return_path: str | None = None) -> str:
    ensure_user(session, user_id)
    require_group(session, user_id, group_id)
    return_path = validate_return_path(return_path)
    target = urlsplit(return_path).path
    if target.startswith("/campaigns/"):
        campaign = session.scalar(select(Campaign).where(Campaign.id == int(target.rsplit("/", 1)[1]), Campaign.user_id == user_id))
        if not campaign:
            raise LookupError("Campaign not found")

    path = credentials_file_path()
    if not path.exists():
        raise FileNotFoundError("Google OAuth client credentials JSON is missing.")

    flow = Flow.from_client_secrets_file(str(path), SCOPES, redirect_uri=callback_url())
    state = secrets.token_urlsafe(32)
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="select_account consent",
        state=state,
    )
    session.add(
        OAuthState(
            state=state,
            user_id=user_id,
            group_id=group_id,
            code_verifier=flow.code_verifier,
            return_path=return_path,
            expires_at=utcnow() + OAUTH_STATE_TTL,
        )
    )
    session.flush()
    return auth_url


def load_fresh_oauth_state(session: Session, state_value: str | None) -> OAuthState | None:
    if not state_value:
        return None
    state = session.scalar(select(OAuthState).where(OAuthState.state == state_value).with_for_update())
    if not state or state.used_at:
        return None
    expires_at = state.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=utcnow().tzinfo)
    if expires_at <= utcnow():
        return None
    return state


def flow_from_state(state: OAuthState) -> Flow:
    path = credentials_file_path()
    if not path.exists():
        raise FileNotFoundError("Google OAuth client credentials JSON is missing.")
    return Flow.from_client_secrets_file(
        str(path),
        SCOPES,
        redirect_uri=callback_url(),
        code_verifier=state.code_verifier,
        autogenerate_code_verifier=False,
    )
