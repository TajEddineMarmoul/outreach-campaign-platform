from __future__ import annotations

import json
import os

os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.deps import get_current_user_id, require_admin_user
from api.schemas import SettingsUpdate
from src.gmail_sender import credentials_file_path
from src.platform.db import get_session
from src.platform.models import Campaign, UserSettings
from src.platform.services import (
    autopilot_reschedule_state,
    ensure_user,
    set_user_timezone,
    validate_timezone_name,
)
from src.platform.time import utcnow


router = APIRouter()
DEFAULT_SETTINGS = {
    "max_daily_cap": 50,
    "bounce_rate_pause_threshold": 5.0,
    "max_consecutive_errors": 3,
}


class CredentialsContent(BaseModel):
    content: str


class TimezoneUpdate(BaseModel):
    timezone: str
    only_if_unset: bool = False


def _set_timezone(session: Session, user_id: str, value: str) -> tuple[bool, int]:
    try:
        validate_timezone_name(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    settings = session.get(UserSettings, user_id)
    if settings and settings.timezone == value:
        return False, 0

    # Delivery workers lock campaigns first. Keep the same lock order so a
    # timezone change cannot race a batch created with the previous timezone.
    campaigns = list(
        session.scalars(
            select(Campaign)
            .where(
                Campaign.user_id == user_id,
                Campaign.status == "autopilot",
                Campaign.timezone.is_(None),
            )
            .order_by(Campaign.id)
            .with_for_update()
        )
    )
    session.scalar(
        select(UserSettings)
        .where(UserSettings.user_id == user_id)
        .with_for_update()
    )
    changed = set_user_timezone(session, user_id, value)
    if not changed:
        return False, 0

    now = utcnow()
    for campaign in campaigns:
        state = autopilot_reschedule_state(session, campaign, now=now)
        campaign.scheduled_at = state["next_at"]
        campaign.send_settings = {
            **(campaign.send_settings or {}),
            "pause_reason": state["pause_reason"],
        }
    return True, len(campaigns)


@router.get("/api/settings")
def get_settings(
    session: Session = Depends(get_session),
    user_id: str = Depends(get_current_user_id),
):
    ensure_user(session, user_id)
    settings = session.get(UserSettings, user_id)
    defaults = {**DEFAULT_SETTINGS, **(settings.defaults or {})}
    session.commit()
    return {
        "timezone": settings.timezone,
        "max_daily_cap": defaults["max_daily_cap"],
        "bounce_rate_pause_threshold": defaults["bounce_rate_pause_threshold"],
        "max_consecutive_errors": defaults["max_consecutive_errors"],
    }


@router.patch("/api/settings")
def patch_settings(
    req: SettingsUpdate,
    session: Session = Depends(get_session),
    user_id: str = Depends(get_current_user_id),
):
    ensure_user(session, user_id)
    settings = session.get(UserSettings, user_id)
    _set_timezone(session, user_id, req.timezone)
    settings.defaults = {
        **(settings.defaults or {}),
        "timezone_initialized": True,
        "max_daily_cap": req.max_daily_cap,
        "bounce_rate_pause_threshold": req.bounce_rate_pause_threshold,
        "max_consecutive_errors": req.max_consecutive_errors,
    }
    session.commit()
    return {"status": "success"}


@router.patch("/api/settings/timezone")
def patch_timezone(
    req: TimezoneUpdate,
    session: Session = Depends(get_session),
    user_id: str = Depends(get_current_user_id),
):
    ensure_user(session, user_id)
    settings = session.get(UserSettings, user_id)
    if req.only_if_unset and (
        bool((settings.defaults or {}).get("timezone_initialized"))
        or settings.timezone != "UTC"
    ):
        session.commit()
        return {"status": "success", "timezone": settings.timezone, "changed": False}
    changed, rescheduled_campaigns = _set_timezone(session, user_id, req.timezone)
    settings = session.get(UserSettings, user_id)
    settings.defaults = {
        **(settings.defaults or {}),
        "timezone_initialized": True,
    }
    if not changed:
        session.commit()
        return {"status": "success", "timezone": settings.timezone, "changed": False}
    session.commit()
    return {
        "status": "success",
        "timezone": settings.timezone,
        "changed": True,
        "rescheduled_campaigns": rescheduled_campaigns,
    }


@router.get("/api/oauth/status")
def get_oauth_status(_admin_user_id: str = Depends(require_admin_user)):
    return {"credentials_json_present": credentials_file_path().exists()}


@router.post("/api/oauth/save-credentials-json")
def save_credentials(
    req: CredentialsContent,
    _admin_user_id: str = Depends(require_admin_user),
):
    try:
        data = json.loads(req.content)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Expected a JSON object")

    client_section = data.get("web") or data.get("installed")
    if not client_section:
        raise HTTPException(status_code=400, detail="OAuth JSON must contain a 'web' or 'installed' key")

    client_id = (client_section.get("client_id") or "").strip()
    client_secret = (client_section.get("client_secret") or "").strip()
    if not client_id or not client_secret:
        raise HTTPException(status_code=400, detail="Missing client_id or client_secret")

    path = credentials_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return {"status": "success", "client_type": "web" if "web" in data else "installed"}
