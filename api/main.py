from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"

from fastapi import FastAPI, HTTPException
from fastapi import Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from api.deps import db
from api.routers import (
    analytics,
    audience,
    campaign_delivery,
    campaign_workspace,
    campaigns,
    contacts,
    email_tracking,
    gmail_push,
    oauth,
    sender_groups,
    settings,
    templates,
    workspace_tokens,
)
from src.platform.db import SessionLocal
from src.platform.migrations import upgrade_database
from api.auth import AuthenticatedUser, IS_PRODUCTION, LOCAL_DEV_USER_ID, _signed_principal

try:  # Keep the rest of the API serving if the optional MCP extra is absent.
    from outreach_mcp.server import (
        bound_workspace_user,
        hosted_http_app,
        start_hosted_transport,
    )
except ImportError as exc:  # pragma: no cover - depends on the deployment's extras
    bound_workspace_user = None
    hosted_http_app = None
    start_hosted_transport = None
    # Recorded without its message so the health route can report a cause
    # without exposing deployment internals.
    MCP_IMPORT_ERROR = type(exc).__name__
else:
    MCP_IMPORT_ERROR = ""


def _env_flag(name: str, *, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    # A serverless deployment can start multiple instances concurrently. The
    # database is migrated explicitly during deployment instead of on every
    # cold start, avoiding migration races and unnecessary startup work.
    default = not bool(os.getenv("VERCEL"))
    if _env_flag("RUN_DATABASE_MIGRATIONS", default=default):
        conn = db.init_db()
        conn.close()
        upgrade_database()
    # The route is mounted at import time, because a platform may serve
    # requests without running startup. Startup only needs to bring the
    # transport's session manager up.
    if start_hosted_transport is None:
        yield
        return
    async with start_hosted_transport():
        yield


app = FastAPI(title="Outreach App API", version="1.0.0", lifespan=lifespan)


def _cors_origin_regex() -> str:
    configured = os.getenv("CORS_ALLOW_ORIGIN_REGEX", "").strip()
    if configured:
        return configured
    if os.getenv("APP_ENV", "").strip().lower() == "production":
        return r"^https://www\.outreachemails\.online$"
    return r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$"

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=_cors_origin_regex(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)


@app.get("/health", tags=["health"])
def health():
    try:
        with SessionLocal() as session:
            session.execute(text("SELECT 1"))
    except Exception as exc:
        logging.getLogger("outreach.health").exception("Database health check failed")
        raise HTTPException(status_code=503, detail="Database unavailable") from exc
    return {
        "status": "ok",
        "database": "ok",
        # Whether the hosted MCP transport is mounted, and why not if it is
        # not. Reports an exception class name only, never a message.
        "mcp": "mounted" if hosted_http_app is not None else "unavailable",
        "mcp_import_error": MCP_IMPORT_ERROR,
    }


app.include_router(sender_groups.router)
app.include_router(sender_groups.senders_router)
app.include_router(campaign_delivery.router)
app.include_router(campaign_workspace.router)
app.include_router(campaigns.router)
app.include_router(audience.router)
app.include_router(contacts.router)
app.include_router(email_tracking.router)
app.include_router(gmail_push.router)
app.include_router(templates.router)
app.include_router(workspace_tokens.router)
app.include_router(settings.router)
app.include_router(oauth.router)
app.include_router(analytics.router)


class _SignedMCPApp:
    """Require a fresh frontend proxy assertion before any MCP protocol data."""

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.inner(scope, receive, send)
            return
        request = Request(scope, receive)
        try:
            if (
                not IS_PRODUCTION
                and LOCAL_DEV_USER_ID
                and request.headers.get("authorization") == f"Bearer local_dev_{LOCAL_DEV_USER_ID}"
            ):
                principal = AuthenticatedUser(LOCAL_DEV_USER_ID, auth_method="local")
            else:
                principal = _signed_principal(request)
        except HTTPException as exc:
            response = JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
            await response(scope, receive, send)
            return
        with bound_workspace_user(principal.user_id):
            try:
                await self.inner(scope, receive, send)
            except RuntimeError as exc:
                # The transport reports this when the platform served a request
                # without running application startup. Answer with a clear
                # error instead of leaking an internal failure.
                if "Task group is not initialized" not in str(exc):
                    raise
                response = JSONResponse(
                    {"detail": "The MCP endpoint is still starting up. Retry in a moment."},
                    status_code=503,
                )
                try:
                    await response(scope, receive, send)
                except RuntimeError:
                    # The transport already began its own response.
                    pass


if hosted_http_app is not None:
    # Mounted while the module is imported so the route exists even when the
    # platform serves a request without running application startup.
    app.mount("/", _SignedMCPApp(hosted_http_app()))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
