from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from api.deps import db
from api.routers import (
    analytics,
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
)
from src.platform.db import SessionLocal
from src.platform.migrations import upgrade_database


def _env_flag(name: str, *, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    # A serverless deployment can start multiple instances concurrently. The
    # database is migrated explicitly during deployment instead of on every
    # cold start, avoiding migration races and unnecessary startup work.
    default = not bool(os.getenv("VERCEL"))
    if _env_flag("RUN_DATABASE_MIGRATIONS", default=default):
        conn = db.init_db()
        conn.close()
        upgrade_database()
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
    return {"status": "ok", "database": "ok"}


app.include_router(sender_groups.router)
app.include_router(sender_groups.senders_router)
app.include_router(campaign_delivery.router)
app.include_router(campaign_workspace.router)
app.include_router(campaigns.router)
app.include_router(contacts.router)
app.include_router(email_tracking.router)
app.include_router(gmail_push.router)
app.include_router(templates.router)
app.include_router(settings.router)
app.include_router(oauth.router)
app.include_router(analytics.router)

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
