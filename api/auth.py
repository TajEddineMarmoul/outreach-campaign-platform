from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import time

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


def _identity_message(*, timestamp: str, method: str, path: str, user_id: str) -> bytes:
    """Return the canonical payload signed by the frontend proxy."""

    return f"{timestamp}\n{method.upper()}\n{path}\n{user_id}".encode("utf-8")


def _reject_invalid_identity() -> None:
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired authentication token",
    )


def _signed_user_id(request: Request) -> str:
    if not BACKEND_IDENTITY_SECRET:
        logger.error("BACKEND_IDENTITY_SECRET is missing in production")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Application authentication is not configured",
        )

    user_id = (request.headers.get("x-backend-user-id") or "").strip()
    timestamp = (request.headers.get("x-backend-auth-timestamp") or "").strip()
    signature = (request.headers.get("x-backend-auth-signature") or "").strip()
    if not user_id or not USER_ID_PATTERN.fullmatch(user_id) or not signature:
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
        ),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(signature, expected):
        _reject_invalid_identity()
    return user_id


def get_current_user_id(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> str:
    """Return the identity authenticated for this request.

    Production accepts only the user-specific assertion created after Clerk
    authentication in the frontend proxy. The narrow local/test paths below
    deliberately remain unavailable in production.
    """

    if IS_PRODUCTION:
        return _signed_user_id(request)

    # A local web app can access one configured account without carrying a
    # production secret onto the developer machine.
    if LOCAL_DEV_USER_ID and credentials:
        local_token = f"local_dev_{LOCAL_DEV_USER_ID}"
        if hmac.compare_digest(credentials.credentials, local_token):
            return LOCAL_DEV_USER_ID

    # Development/testing fallback (never accepted in production).
    if credentials and credentials.credentials.startswith("mock_"):
        return credentials.credentials

    _reject_invalid_identity()
