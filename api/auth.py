from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import time
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

logger = logging.getLogger(__name__)

# The browser never calls the API directly in production. The Next.js backend
# proxy verifies the Clerk session, then attaches a short-lived HMAC assertion
# for that specific Clerk user. A shared bearer token cannot represent a user:
# it was the cause of every signed-in account being mapped to one workspace.
security = HTTPBearer(auto_error=False)

# APP_ACCESS_TOKEN is accepted only as a temporary migration source for the
# HMAC key. It is never accepted as a bearer credential, so existing Vercel
# configuration can receive the tenant-isolation fix without a gap in service.
BACKEND_IDENTITY_SECRET = os.getenv("BACKEND_IDENTITY_SECRET") or os.getenv("APP_ACCESS_TOKEN", "")
LOCAL_DEV_USER_ID = os.getenv("LOCAL_DEV_USER_ID", "")
IS_PRODUCTION = os.getenv("APP_ENV", "").strip().lower() == "production"
IDENTITY_MAX_AGE_SECONDS = 60
IDENTITY_MAX_CLOCK_SKEW_SECONDS = 5
USER_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,255}$")
ROLE_MEMBER = "member"
ROLE_ADMIN = "admin"


@dataclass(frozen=True)
class AuthenticatedUser:
    """Identity asserted by the authenticated frontend proxy."""

    user_id: str
    is_admin: bool = False
    auth_method: str = "proxy"


def _workspace_token_principal(raw_token: str) -> AuthenticatedUser:
    from sqlalchemy import select
    from src.platform.db import SessionLocal
    from src.platform.models import WorkspaceAccessToken
    from src.platform.time import utcnow

    if not raw_token.startswith("outreach_mcp_") or len(raw_token) > 160:
        _reject_invalid_identity()
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    with SessionLocal() as session:
        token = session.scalar(
            select(WorkspaceAccessToken).where(WorkspaceAccessToken.token_hash == token_hash)
        )
        if not token or not hmac.compare_digest(token.token_hash, token_hash) or token.revoked_at:
            _reject_invalid_identity()
        expires_at = token.expires_at
        if expires_at.tzinfo is None:
            from datetime import timezone
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= utcnow():
            _reject_invalid_identity()
        return AuthenticatedUser(user_id=token.user_id, auth_method="workspace_token")


def _identity_message(
    *,
    timestamp: str,
    method: str,
    path: str,
    user_id: str,
    role: str = ROLE_MEMBER,
) -> bytes:
    """Return the canonical payload signed by the frontend proxy."""

    return f"{timestamp}\n{method.upper()}\n{path}\n{user_id}\n{role}".encode("utf-8")


def identity_headers(
    *,
    secret: str,
    method: str,
    path: str,
    user_id: str,
    role: str = ROLE_MEMBER,
    timestamp: str | None = None,
) -> dict[str, str]:
    """Build the signed assertion the API accepts for one user.

    The frontend proxy and the hosted MCP endpoint both call this so a single
    implementation defines the assertion format.
    """

    issued_at = timestamp or str(int(time.time()))
    signature = hmac.new(
        secret.encode("utf-8"),
        _identity_message(
            timestamp=issued_at, method=method, path=path, user_id=user_id, role=role
        ),
        hashlib.sha256,
    ).hexdigest()
    return {
        "x-backend-user-id": user_id,
        "x-backend-user-role": role,
        "x-backend-auth-timestamp": issued_at,
        "x-backend-auth-signature": signature,
    }


def _reject_invalid_identity() -> None:
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired authentication token",
    )


def _signed_principal(request: Request) -> AuthenticatedUser:
    if not BACKEND_IDENTITY_SECRET:
        logger.error("BACKEND_IDENTITY_SECRET is missing in production")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Application authentication is not configured",
        )

    user_id = (request.headers.get("x-backend-user-id") or "").strip()
    timestamp = (request.headers.get("x-backend-auth-timestamp") or "").strip()
    signature = (request.headers.get("x-backend-auth-signature") or "").strip()
    role = (request.headers.get("x-backend-user-role") or ROLE_MEMBER).strip().lower()
    if not user_id or not USER_ID_PATTERN.fullmatch(user_id) or not signature:
        _reject_invalid_identity()
    if role not in {ROLE_MEMBER, ROLE_ADMIN}:
        _reject_invalid_identity()
    try:
        timestamp_value = int(timestamp)
    except ValueError:
        _reject_invalid_identity()

    age = time.time() - timestamp_value
    if age > IDENTITY_MAX_AGE_SECONDS or age < -IDENTITY_MAX_CLOCK_SKEW_SECONDS:
        _reject_invalid_identity()

    expected = hmac.new(
        BACKEND_IDENTITY_SECRET.encode("utf-8"),
        _identity_message(
            timestamp=timestamp,
            method=request.method,
            path=request.url.path,
            user_id=user_id,
            role=role,
        ),
        hashlib.sha256,
    ).hexdigest()
    if hmac.compare_digest(signature, expected):
        return AuthenticatedUser(user_id=user_id, is_admin=role == ROLE_ADMIN)

    # Keep normal requests available while the frontend and API deployments
    # roll out. Old proxies never carried a role and therefore can only become
    # regular users; privileged routes still require the new signed admin role.
    if role == ROLE_MEMBER and not request.headers.get("x-backend-user-role"):
        legacy_expected = hmac.new(
            BACKEND_IDENTITY_SECRET.encode("utf-8"),
            f"{timestamp}\n{request.method.upper()}\n{request.url.path}\n{user_id}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        if hmac.compare_digest(signature, legacy_expected):
            return AuthenticatedUser(user_id=user_id)
    _reject_invalid_identity()


def get_current_principal(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> AuthenticatedUser:
    """Return the authenticated user and their cryptographically bound role."""

    if not isinstance(credentials, HTTPAuthorizationCredentials):
        credentials = None
    if credentials and credentials.credentials.startswith("outreach_mcp_"):
        return _workspace_token_principal(credentials.credentials)

    if IS_PRODUCTION:
        return _signed_principal(request)

    if LOCAL_DEV_USER_ID and credentials:
        local_token = f"local_dev_{LOCAL_DEV_USER_ID}"
        if hmac.compare_digest(credentials.credentials, local_token):
            return AuthenticatedUser(
                user_id=LOCAL_DEV_USER_ID,
                is_admin=os.getenv("LOCAL_DEV_IS_ADMIN", "").strip().lower() in {"1", "true", "yes", "on"},
                auth_method="local",
            )

    if credentials and credentials.credentials.startswith("mock_"):
        return AuthenticatedUser(user_id=credentials.credentials, auth_method="mock")

    _reject_invalid_identity()


def get_current_user_id(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> str:
    """Return the identity authenticated for this request.

    Production accepts only the user-specific assertion created after Clerk
    authentication in the frontend proxy. The narrow local/test paths below
    deliberately remain unavailable in production.
    """

    return get_current_principal(request, credentials).user_id


def require_admin_user(
    principal: AuthenticatedUser = Depends(get_current_principal),
) -> str:
    """Allow a global configuration change only to a Clerk admin."""

    if not principal.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrator access is required",
        )
    return principal.user_id


def require_interactive_user(
    principal: AuthenticatedUser = Depends(get_current_principal),
) -> str:
    """Token issuance and revocation require an interactive Clerk session."""

    if principal.auth_method != "proxy":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Sign in to manage access tokens")
    return principal.user_id
