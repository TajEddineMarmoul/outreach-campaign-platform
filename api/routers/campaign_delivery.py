from __future__ import annotations

import logging
import hmac
import os
import csv
from datetime import datetime, time, timezone
from io import StringIO
from typing import Literal

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.orm import Session

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, model_validator

from api.auth import get_current_user_id
from api.delivery_safety import require_delivery_enabled
from src.platform.gmail_activity import sync_selected_gmail_activity
from src.platform.db import get_session
from src.platform.jobs import create_send_jobs_for_next_batch, has_unsent_approved_recipients
from src.platform.models import AutopilotDaySchedule, Campaign, CampaignRecipient, Contact, EmailTrackingEvent, GmailActivityEvent, SendJob, SendLog, Sender
from src.platform.scheduler import WEEKDAY_NAMES, next_autopilot_run
from src.platform.services import autopilot_reschedule_state, campaign_daily_capacity, campaign_zone, connected_senders, ensure_user, require_group, serialize_group, user_zone, validate_timezone_name
from src.platform.time import utcnow
from src.platform.worker import recover_stale_jobs, run_worker_cycle
from src.template_engine import extract_template_variables, missing_template_variables


router = APIRouter(tags=["campaign-delivery"])


@router.post("/internal/worker/tick", tags=["worker"])
def worker_tick(x_worker_token: str | None = Header(default=None)):
    expected = os.getenv("WORKER_TICK_TOKEN")
    if not expected or not x_worker_token or not hmac.compare_digest(x_worker_token, expected):
        raise HTTPException(status_code=401, detail="Invalid worker token")
    require_delivery_enabled()
    recovered = recover_stale_jobs(stale_after_minutes=10)
    try:
        configured_max_jobs = int(os.getenv("WORKER_MAX_JOBS", "25"))
    except ValueError:
        configured_max_jobs = 25
    processed = run_worker_cycle(max_jobs=max(1, min(configured_max_jobs, 25)))
    return {"status": "ok", "recovered": recovered, "processed": processed}


class DeliveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    timezone: str | None = Field(default=None, min_length=1, max_length=80)


class CampaignSenderGroupSelect(DeliveryRequest):
    sender_group_id: int


class SendNowRequest(DeliveryRequest):
    delay_minutes: int = Field(default=5, ge=0, le=1440)
    dry_run: bool = False


class ScheduleRequest(DeliveryRequest):
    scheduled_at: datetime
    delay_minutes: int = Field(default=5, ge=0, le=1440)
    dry_run: bool = False


class DayScheduleEntry(DeliveryRequest):
    cap: int = Field(..., ge=1)
    start: str = Field(default="09:00", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    end: str = Field(default="17:00", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")

    @model_validator(mode="after")
    def validate_window(self):
        if time.fromisoformat(self.start) >= time.fromisoformat(self.end):
            raise ValueError("start must be earlier than end")
        return self


class AutopilotRequest(DeliveryRequest):
    schedule: dict[str, DayScheduleEntry] = Field(..., min_length=1)
    delay_minutes: int = Field(default=5, ge=0, le=1440)
    pacing_mode: Literal["fixed_delay", "spread_evenly"] = "fixed_delay"
    scheduled_at: datetime | None = None
    dry_run: bool = False

    @model_validator(mode="after")
    def validate_days(self):
        unknown = sorted(set(self.schedule) - set(WEEKDAY_NAMES))
        if unknown:
            raise ValueError(f"unknown autopilot days: {', '.join(unknown)}")
        return self


class SendSettingsUpdate(DeliveryRequest):
    mode: str | None = Field(default=None, pattern="^(send_now|schedule|autopilot)$")
    delay_minutes: int | None = Field(default=None, ge=0, le=1440)
    pacing_mode: Literal["fixed_delay", "spread_evenly"] | None = None
    dry_run: bool | None = None
    scheduled_at: datetime | None = None
    schedule: dict[str, DayScheduleEntry] | None = None

    @model_validator(mode="after")
    def validate_schedule(self):
        if self.mode == "autopilot" and not self.schedule:
            raise ValueError("autopilot requires at least one enabled day")
        if self.schedule is not None:
            unknown = sorted(set(self.schedule) - set(WEEKDAY_NAMES))
            if unknown:
                raise ValueError(f"unknown autopilot days: {', '.join(unknown)}")
        return self


class AutopilotDailyLimitsUpdate(BaseModel):
    """The one schedule control that is safe to change while Autopilot runs."""

    model_config = ConfigDict(extra="forbid")
    daily_limits: dict[str, int] = Field(..., min_length=1)

    @model_validator(mode="after")
    def validate_days(self):
        unknown = sorted(set(self.daily_limits) - set(WEEKDAY_NAMES))
        if unknown:
            raise ValueError(f"unknown autopilot days: {', '.join(unknown)}")
        invalid = sorted(day for day, cap in self.daily_limits.items() if cap < 1)
        if invalid:
            raise ValueError("Each daily limit must be at least 1")
        return self


EDIT_LOCKED_STATUSES = {"sending", "scheduled", "autopilot"}
# Paused campaigns can retry failed recipients. Stopped/ended campaigns resume
# pending work without automatically retrying recipients with a send error.
RESUMABLE_STATUSES = {"paused", "ended", "stopped"}
CLOSED_STATUSES = {"ended", "stopped"}


def apply_campaign_timezone(
    session: Session,
    campaign: Campaign,
    timezone_name: str | None,
) -> None:
    if timezone_name is None:
        return
    try:
        validate_timezone_name(timezone_name)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    campaign.timezone = timezone_name


def campaign_datetime_utc(session: Session, campaign: Campaign, value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=campaign_zone(session, campaign))
    return value.astimezone(timezone.utc)


def require_campaign_editable(session: Session, campaign_id: int, user_id: str) -> Campaign:
    campaign = require_campaign(session, campaign_id, user_id)
    if campaign.status in EDIT_LOCKED_STATUSES:
        raise HTTPException(
            status_code=409,
            detail="Stop the campaign before editing its composer, send options, or recipients",
        )
    return campaign


def require_campaign_ready_to_start(
    session: Session,
    campaign_id: int,
    user_id: str,
) -> Campaign:
    campaign = require_campaign_editable(session, campaign_id, user_id)
    if campaign.status == "paused":
        raise HTTPException(
            status_code=409,
            detail="This campaign is paused. Save your changes, then use Resume sending.",
        )
    return campaign


def require_campaign(session: Session, campaign_id: int, user_id: str) -> Campaign:
    campaign = session.scalar(
        select(Campaign).where(Campaign.id == campaign_id, Campaign.user_id == user_id)
    )
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return campaign


def queued_job_ids(session: Session, campaign_id: int, user_id: str) -> list[int]:
    return list(
        session.scalars(
            select(SendJob.id)
            .where(
                SendJob.campaign_id == campaign_id,
                SendJob.user_id == user_id,
                SendJob.status.in_(("queued", "retry")),
            )
            .order_by(SendJob.id)
        )
    )


def has_failed_recipients(session: Session, campaign_id: int, user_id: str) -> bool:
    return session.scalar(
        select(CampaignRecipient.contact_id)
        .join(Contact, Contact.id == CampaignRecipient.contact_id)
        .where(
            CampaignRecipient.campaign_id == campaign_id,
            CampaignRecipient.status == "failed",
            Contact.user_id == user_id,
            Contact.status.in_(("approved", "sent")),
        )
        .limit(1)
    ) is not None


def require_connected_delivery_group(session: Session, campaign: Campaign, user_id: str):
    if not campaign.selected_sender_group_id:
        raise HTTPException(status_code=400, detail="Campaign does not have a sender group selected")
    try:
        group = require_group(session, user_id, campaign.selected_sender_group_id)
    except LookupError:
        raise HTTPException(status_code=400, detail="Selected sender group no longer exists")
    if not connected_senders(group):
        raise HTTPException(status_code=409, detail="Selected sender group has no connected senders")
    return group


def require_known_template_variables(session: Session, campaign: Campaign) -> None:
    available_headers: set[str] = {"email"}
    custom_fields_rows = session.scalars(
        select(Contact.custom_fields)
        .join(CampaignRecipient, CampaignRecipient.contact_id == Contact.id)
        .where(CampaignRecipient.campaign_id == campaign.id)
    )
    for custom_fields in custom_fields_rows:
        if isinstance(custom_fields, dict):
            available_headers.update(str(header) for header in custom_fields)

    used_variables = {
        variable
        for template in (
            campaign.subject_template,
            campaign.body_template,
            campaign.fallback_body_template,
        )
        for variable in extract_template_variables(template or "")
    }
    unknown = sorted(used_variables - available_headers)
    if unknown:
        raise HTTPException(
            status_code=409,
            detail=f"Unknown CSV template variables: {', '.join(unknown)}",
        )


def recipient_template_validation(
    session: Session,
    campaign: Campaign,
    user_id: str,
    *,
    skip_invalid: bool = False,
) -> dict:
    """Report incomplete template data and optionally reject those recipients.

    Counts are calculated per variable, while ``skipped_recipient_count`` is a
    de-duplicated recipient count. This matters when one CSV row is missing
    more than one required value.
    """
    eligible_status = or_(
        CampaignRecipient.status == "approved",
        CampaignRecipient.status.is_(None),
        func.trim(CampaignRecipient.status) == "",
    )
    if skip_invalid:
        # Preserve compatibility with recipients created before the approved
        # server default was introduced.
        session.execute(
            update(CampaignRecipient)
            .where(
                CampaignRecipient.campaign_id == campaign.id,
                or_(
                    CampaignRecipient.status.is_(None),
                    func.trim(CampaignRecipient.status) == "",
                ),
            )
            .values(status="approved")
        )

    rows = list(
        session.execute(
            select(CampaignRecipient, Contact)
            .join(Contact, Contact.id == CampaignRecipient.contact_id)
            .where(
                CampaignRecipient.campaign_id == campaign.id,
                Contact.user_id == user_id,
                Contact.status.in_(("approved", "sent")),
                eligible_status,
            )
            .order_by(CampaignRecipient.created_at, CampaignRecipient.contact_id)
        )
    )
    templates = (
        campaign.subject_template or "",
        campaign.body_template or campaign.fallback_body_template or "",
    )
    variables: list[str] = []
    for template in templates:
        for variable in extract_template_variables(template):
            if variable not in variables:
                variables.append(variable)

    missing_counts = {variable: 0 for variable in variables}
    invalid_contact_ids: list[int] = []
    overlap_recipient_count = 0
    for recipient, contact in rows:
        custom_fields = contact.custom_fields if isinstance(contact.custom_fields, dict) else {}
        context = dict(custom_fields)
        context.setdefault("email", contact.email_normalized)
        missing: list[str] = []
        for template in templates:
            for variable in missing_template_variables(template, context):
                if variable not in missing:
                    missing.append(variable)
        if not missing:
            continue
        invalid_contact_ids.append(recipient.contact_id)
        if len(missing) > 1:
            overlap_recipient_count += 1
        for variable in missing:
            missing_counts[variable] += 1

    if skip_invalid and invalid_contact_ids:
        session.execute(
            update(CampaignRecipient)
            .where(
                CampaignRecipient.campaign_id == campaign.id,
                CampaignRecipient.contact_id.in_(invalid_contact_ids),
            )
            .values(status="rejected")
        )

    skipped_count = len(invalid_contact_ids)
    return {
        "checked_recipient_count": len(rows),
        "ready_recipient_count": len(rows) - skipped_count,
        "skipped_recipient_count": skipped_count,
        "overlap_recipient_count": overlap_recipient_count,
        "missing_by_variable": [
            {"variable": variable, "row_count": missing_counts[variable]}
            for variable in variables
            if missing_counts[variable]
        ],
    }


def no_ready_recipients_detail(validation: dict) -> dict:
    return {
        "code": "no_recipients_with_complete_template_data",
        "message": (
            "No recipients have all template values. "
            f"{validation['skipped_recipient_count']} recipients would be skipped."
        ),
        "recipient_validation": validation,
    }


def reopen_failed_recipients(session: Session, campaign_id: int, user_id: str) -> int:
    failed_contact_ids = list(
        session.scalars(
            select(CampaignRecipient.contact_id)
            .join(Contact, Contact.id == CampaignRecipient.contact_id)
            .where(
                CampaignRecipient.campaign_id == campaign_id,
                CampaignRecipient.status == "failed",
                Contact.user_id == user_id,
                Contact.status.in_(("approved", "sent")),
            )
        )
    )
    if not failed_contact_ids:
        return 0

    session.execute(
        delete(SendJob).where(
            SendJob.campaign_id == campaign_id,
            SendJob.recipient_id.in_(failed_contact_ids),
        )
    )
    session.execute(
        update(CampaignRecipient)
        .where(
            CampaignRecipient.campaign_id == campaign_id,
            CampaignRecipient.contact_id.in_(failed_contact_ids),
        )
        .values(status="approved", reset_at=utcnow())
    )
    return len(failed_contact_ids)


def ensure_campaign_shell(
    session: Session,
    *,
    campaign_id: int,
    user_id: str,
    name: str | None = None,
    subject_template: str = "",
    body_template: str = "",
    fallback_body_template: str = "",
) -> Campaign:
    ensure_user(session, user_id)
    campaign = session.scalar(select(Campaign).where(Campaign.id == campaign_id, Campaign.user_id == user_id))
    if campaign:
        if not campaign.timezone:
            campaign.timezone = user_zone(session, user_id).key
        return campaign
    campaign = Campaign(
        id=campaign_id,
        user_id=user_id,
        name=name or f"Campaign {campaign_id}",
        subject_template=subject_template,
        body_template=body_template,
        fallback_body_template=fallback_body_template,
        status="draft",
        timezone=user_zone(session, user_id).key,
    )
    session.add(campaign)
    session.flush()
    return campaign


@router.patch("/api/campaigns/{campaign_id}/sender-group")
def patch_campaign_sender_group(
    campaign_id: int,
    req: CampaignSenderGroupSelect,
    session: Session = Depends(get_session),
    user_id: str = Depends(get_current_user_id),
):
    try:
        group = require_group(session, user_id, req.sender_group_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="Sender group not found")
    campaign = session.scalar(select(Campaign).where(Campaign.id == campaign_id, Campaign.user_id == user_id))
    if campaign:
        require_campaign_editable(session, campaign_id, user_id)
    else:
        campaign = ensure_campaign_shell(session, campaign_id=campaign_id, user_id=user_id)
    campaign.selected_sender_group_id = group.id
    session.commit()
    return {"status": "success", "sender_group": serialize_group(session, group)}


@router.patch("/api/campaigns/{campaign_id}/send-settings")
def patch_send_settings(
    campaign_id: int,
    req: SendSettingsUpdate,
    session: Session = Depends(get_session),
    user_id: str = Depends(get_current_user_id),
):
    campaign = require_campaign_editable(session, campaign_id, user_id)
    apply_campaign_timezone(session, campaign, req.timezone)
    settings = dict(campaign.send_settings or {})
    if req.mode is not None:
        settings["mode"] = req.mode
    if req.delay_minutes is not None:
        settings["delay_minutes"] = req.delay_minutes
    if req.pacing_mode is not None:
        settings["pacing_mode"] = req.pacing_mode
    if req.dry_run is not None:
        settings["dry_run"] = req.dry_run
    if "scheduled_at" in req.model_fields_set:
        if req.scheduled_at is None:
            settings.pop("draft_scheduled_at", None)
        else:
            scheduled_at = campaign_datetime_utc(session, campaign, req.scheduled_at)
            settings["draft_scheduled_at"] = scheduled_at.isoformat()
    campaign.send_settings = settings
    if req.schedule is not None:
        session.execute(delete(AutopilotDaySchedule).where(AutopilotDaySchedule.campaign_id == campaign.id))
        for day_name, entry in req.schedule.items():
            session.add(
                AutopilotDaySchedule(
                    campaign_id=campaign.id,
                    day_of_week=day_name,
                    daily_cap=entry.cap,
                    start_time=entry.start,
                    end_time=entry.end,
                )
            )
    session.commit()
    return {"status": "success", "send_settings": campaign.send_settings}


@router.patch("/api/campaigns/{campaign_id}/autopilot/daily-limits")
def patch_autopilot_daily_limits(
    campaign_id: int,
    req: AutopilotDailyLimitsUpdate,
    session: Session = Depends(get_session),
    user_id: str = Depends(get_current_user_id),
):
    """Update Autopilot day caps without interrupting an active campaign.

    Message, audience, senders, timing and mode still go through the normal
    editable-campaign guard. Day caps are checked again at queue and delivery
    time, so lowering one immediately prevents extra sends beyond the new cap.
    """
    campaign = require_campaign(session, campaign_id, user_id)
    settings = dict(campaign.send_settings or {})
    if campaign.status != "autopilot" or settings.get("mode") != "autopilot":
        raise HTTPException(
            status_code=409,
            detail="Autopilot daily limits can only be changed while Autopilot is running.",
        )

    schedules = {
        schedule.day_of_week: schedule
        for schedule in session.scalars(
            select(AutopilotDaySchedule).where(
                AutopilotDaySchedule.campaign_id == campaign.id
            )
        )
    }
    unavailable_days = sorted(set(req.daily_limits) - set(schedules))
    if unavailable_days:
        raise HTTPException(
            status_code=422,
            detail=(
                "Autopilot is not enabled on "
                f"{', '.join(unavailable_days)}. Enable that day before setting a limit."
            ),
        )

    for day_name, daily_cap in req.daily_limits.items():
        schedules[day_name].daily_cap = daily_cap

    # Re-evaluate the active day only. This wakes an Autopilot campaign that
    # had paused at its cap after its cap increases, and pauses it immediately
    # when a newly lower cap has already been consumed. It deliberately leaves
    # normal pacing alone when the campaign still has capacity.
    now = utcnow()
    session.flush()
    reschedule = autopilot_reschedule_state(session, campaign, now=now)
    capacity = reschedule.get("capacity")
    was_at_campaign_cap = settings.get("pause_reason") == "campaign_daily_cap_reached"
    if capacity is not None and capacity["remaining"] <= 0:
        campaign.scheduled_at = reschedule["next_at"]
        settings["pause_reason"] = "campaign_daily_cap_reached"
    elif was_at_campaign_cap:
        campaign.scheduled_at = reschedule["next_at"]
        settings["pause_reason"] = reschedule["pause_reason"]
    campaign.send_settings = settings
    session.commit()

    return {
        "status": "success",
        "daily_limits": {
            day_name: schedules[day_name].daily_cap
            for day_name in sorted(schedules)
        },
        "capacity": capacity,
        "next_batch_at": campaign.scheduled_at.isoformat()
        if campaign.scheduled_at
        else None,
    }


@router.get("/api/campaigns/{campaign_id}/sender-group")
def get_campaign_sender_group(
    campaign_id: int,
    session: Session = Depends(get_session),
    user_id: str = Depends(get_current_user_id),
):
    campaign = session.scalar(select(Campaign).where(Campaign.id == campaign_id, Campaign.user_id == user_id))
    if not campaign or not campaign.selected_sender_group_id:
        return {"sender_group": None}
    try:
        group = require_group(session, user_id, campaign.selected_sender_group_id)
    except LookupError:
        return {"sender_group": None}
    return {"sender_group": serialize_group(session, group)}


@router.get("/api/campaigns/{campaign_id}/recipient-template-validation")
def get_recipient_template_validation(
    campaign_id: int,
    session: Session = Depends(get_session),
    user_id: str = Depends(get_current_user_id),
):
    campaign = require_campaign(session, campaign_id, user_id)
    return recipient_template_validation(session, campaign, user_id)


@router.post("/api/campaigns/{campaign_id}/send-now")
def post_send_now(
    campaign_id: int,
    req: SendNowRequest = SendNowRequest(),
    session: Session = Depends(get_session),
    user_id: str = Depends(get_current_user_id),
):
    require_delivery_enabled()
    logger = logging.getLogger("outreach.send")
    logger.info("send-now start campaign=%s user=%s", campaign_id, user_id)
    campaign = require_campaign_ready_to_start(session, campaign_id, user_id)
    apply_campaign_timezone(session, campaign, req.timezone)
    require_connected_delivery_group(session, campaign, user_id)
    previous_status = campaign.status
    logger.info("campaign id=%s status=%s sender_group_id=%s", campaign.id, campaign.status, campaign.selected_sender_group_id)
    try:
        campaign.status = "sending"
        campaign.send_settings = {
            **(campaign.send_settings or {}),
            "mode": "send_now",
            "delay_minutes": req.delay_minutes,
            "dry_run": req.dry_run,
            "pause_reason": None,
        }
        retried_failed = reopen_failed_recipients(session, campaign_id, user_id)
        validation = recipient_template_validation(
            session,
            campaign,
            user_id,
            skip_invalid=True,
        )
        campaign.send_settings = {
            **(campaign.send_settings or {}),
            "recipient_validation": validation,
        }
        if validation["checked_recipient_count"] and not validation["ready_recipient_count"]:
            raise HTTPException(status_code=409, detail=no_ready_recipients_detail(validation))
        existing_ids = queued_job_ids(session, campaign_id, user_id)
        logger.info("existing queued/retry jobs count=%s", len(existing_ids))
        if existing_ids:
            session.commit()
            return {
                "status": "queued",
                "queued": len(existing_ids),
                "mode": "resume",
                "retried_failed": retried_failed,
                "recipient_validation": validation,
            }

        logger.info("creating new send jobs batch")
        result = create_send_jobs_for_next_batch(
            session,
            user_id=user_id,
            campaign_id=campaign_id,
            delay_minutes=req.delay_minutes,
        )
        logger.info("create_send_jobs result: %s", result)
        if result.get("exhausted"):
            campaign.status = "ended"
            campaign.scheduled_at = None
        elif result.get("reason_code") == "daily_caps_reached":
            campaign.status = "paused"
            campaign.scheduled_at = None
            campaign.send_settings = {
                **(campaign.send_settings or {}),
                "pause_reason": "daily_caps_reached",
            }
        elif not result.get("job_ids"):
            campaign.status = previous_status
        session.commit()
        job_ids = result.get("job_ids", [])
        if job_ids:
            result["queued"] = len(job_ids)
        else:
            logger.warning("no jobs created, reason: %s", result.get("reason", "unknown"))
            raise HTTPException(
                status_code=409,
                detail=result.get("reason", "No email was queued"),
            )
    except (LookupError, ValueError) as exc:
        logger.error("send-now error: %s", exc)
        session.rollback()
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "status": "queued" if result.get("queued") else campaign.status,
        **result,
        "retried_failed": retried_failed,
        "recipient_validation": validation,
    }


@router.post("/api/campaigns/{campaign_id}/schedule")
def post_schedule(
    campaign_id: int,
    req: ScheduleRequest,
    session: Session = Depends(get_session),
    user_id: str = Depends(get_current_user_id),
):
    require_delivery_enabled()
    campaign = require_campaign_ready_to_start(session, campaign_id, user_id)
    apply_campaign_timezone(session, campaign, req.timezone)
    require_connected_delivery_group(session, campaign, user_id)
    retried_failed = reopen_failed_recipients(session, campaign_id, user_id)
    validation = recipient_template_validation(
        session,
        campaign,
        user_id,
        skip_invalid=True,
    )
    if validation["checked_recipient_count"] and not validation["ready_recipient_count"]:
        raise HTTPException(status_code=409, detail=no_ready_recipients_detail(validation))
    approved_recipients = session.scalar(
        select(func.count())
        .select_from(CampaignRecipient)
        .join(Contact, Contact.id == CampaignRecipient.contact_id)
        .where(
            CampaignRecipient.campaign_id == campaign_id,
            CampaignRecipient.status == "approved",
            Contact.status.in_(("approved", "sent")),
        )
    ) or 0
    if approved_recipients == 0:
        raise HTTPException(status_code=409, detail="Campaign has no approved recipients")
    scheduled_at = campaign_datetime_utc(session, campaign, req.scheduled_at)
    campaign.status = "scheduled"
    campaign.scheduled_at = scheduled_at
    campaign.send_settings = {
        **(campaign.send_settings or {}),
        "mode": "schedule",
        "delay_minutes": req.delay_minutes,
        "dry_run": req.dry_run,
        "pause_reason": None,
        "recipient_validation": validation,
    }
    session.commit()
    return {
        "status": "scheduled",
        "scheduled_at": campaign.scheduled_at.isoformat(),
        "retried_failed": retried_failed,
        "recipient_validation": validation,
    }


@router.post("/api/campaigns/{campaign_id}/autopilot/start")
def post_autopilot_start(
    campaign_id: int,
    req: AutopilotRequest,
    session: Session = Depends(get_session),
    user_id: str = Depends(get_current_user_id),
):
    require_delivery_enabled()
    campaign = require_campaign_ready_to_start(session, campaign_id, user_id)
    apply_campaign_timezone(session, campaign, req.timezone)
    require_connected_delivery_group(session, campaign, user_id)
    retried_failed = reopen_failed_recipients(session, campaign_id, user_id)
    validation = recipient_template_validation(
        session,
        campaign,
        user_id,
        skip_invalid=True,
    )
    if validation["checked_recipient_count"] and not validation["ready_recipient_count"]:
        raise HTTPException(status_code=409, detail=no_ready_recipients_detail(validation))
    approved_recipients = session.scalar(
        select(func.count())
        .select_from(CampaignRecipient)
        .join(Contact, Contact.id == CampaignRecipient.contact_id)
        .where(
            CampaignRecipient.campaign_id == campaign_id,
            CampaignRecipient.status == "approved",
            Contact.status.in_(("approved", "sent")),
        )
    ) or 0
    if approved_recipients == 0:
        raise HTTPException(status_code=409, detail="Campaign has no approved recipients")

    campaign.status = "autopilot"
    campaign.send_settings = {
        **(campaign.send_settings or {}),
        "mode": "autopilot",
        "delay_minutes": req.delay_minutes,
        "pacing_mode": req.pacing_mode,
        "dry_run": req.dry_run,
        "pause_reason": None,
        "recipient_validation": validation,
    }

    session.execute(
        delete(AutopilotDaySchedule).where(AutopilotDaySchedule.campaign_id == campaign.id)
    )
    for day_name, entry in req.schedule.items():
        session.add(
            AutopilotDaySchedule(
                campaign_id=campaign.id,
                day_of_week=day_name,
                daily_cap=entry.cap,
                start_time=entry.start,
                end_time=entry.end,
            )
        )
    scheduled_at = req.scheduled_at
    if scheduled_at is not None:
        campaign.scheduled_at = campaign_datetime_utc(session, campaign, scheduled_at)
    else:
        campaign.scheduled_at = next_autopilot_run(session, campaign, now=utcnow())
    session.commit()
    return {
        "status": "autopilot",
        "scheduled_at": campaign.scheduled_at.isoformat(),
        "retried_failed": retried_failed,
        "recipient_validation": validation,
    }


@router.post("/api/campaigns/{campaign_id}/pause")
def post_pause(
    campaign_id: int,
    session: Session = Depends(get_session),
    user_id: str = Depends(get_current_user_id),
):
    campaign = require_campaign(session, campaign_id, user_id)
    if campaign.status == "paused":
        return {"status": "paused"}
    if campaign.status not in {"sending", "scheduled", "autopilot"}:
        raise HTTPException(status_code=409, detail=f"Cannot pause a campaign that is {campaign.status}")
    campaign.status = "paused"
    session.commit()
    return {"status": "paused"}


@router.post("/api/campaigns/{campaign_id}/resume")
def post_resume(
    campaign_id: int,
    session: Session = Depends(get_session),
    user_id: str = Depends(get_current_user_id),
):
    """Continue a campaign that paused or stopped with work still available.

    A completed campaign can continue for newly added recipients. A stopped
    campaign can also pick up jobs that were already queued when a send error
    interrupted the batch. Recipients with a final result are left untouched.
    """
    require_delivery_enabled()
    campaign = require_campaign(session, campaign_id, user_id)
    previous_status = campaign.status
    if previous_status not in RESUMABLE_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot resume a campaign that is {previous_status}",
        )
    resuming_closed = previous_status in CLOSED_STATUSES
    job_ids = queued_job_ids(session, campaign_id, user_id)
    if resuming_closed:
        has_pending_work = bool(job_ids) or has_unsent_approved_recipients(
            session, campaign, user_id=user_id
        )
    else:
        # A paused campaign resumes work that was already accepted, including
        # recipients that failed and were left open for a retry.
        has_pending_work = (
            bool(job_ids)
            or has_failed_recipients(session, campaign_id, user_id)
            or has_unsent_approved_recipients(
                session,
                campaign,
                user_id=user_id,
                include_failed=True,
            )
        )
    if not has_pending_work:
        raise HTTPException(
            status_code=409,
            detail=(
                "Every recipient already has a delivery result. "
                "Add recipients to continue this campaign."
            ),
        )
    mode = (campaign.send_settings or {}).get("mode", "send_now")
    if resuming_closed:
        # Closed campaigns keep retries explicit. Existing queued jobs resume
        # as-is; new jobs are only created for recipients with no prior job.
        retried_failed = 0
    else:
        retried_failed = reopen_failed_recipients(session, campaign_id, user_id)
    campaign.status = "autopilot" if mode == "autopilot" else "sending"
    resume_at = campaign.scheduled_at
    if resume_at and resume_at.tzinfo is None:
        resume_at = resume_at.replace(tzinfo=timezone.utc)
    if not job_ids and mode == "schedule" and resume_at and resume_at > utcnow():
        campaign.status = "scheduled"
        session.commit()
        return {
            "status": "scheduled",
            "queued": 0,
            "scheduled_at": campaign.scheduled_at.isoformat(),
            "retried_failed": retried_failed,
        }
    result: dict = {}
    if not job_ids:
        try:
            result = create_send_jobs_for_next_batch(
                session,
                user_id=user_id,
                campaign_id=campaign_id,
                delay_minutes=int((campaign.send_settings or {}).get("delay_minutes", 5)),
            )
        except (LookupError, ValueError) as exc:
            session.rollback()
            raise HTTPException(status_code=400, detail=str(exc))
        job_ids = result.get("job_ids", [])
        # Job creation writes each recipient inside a savepoint, so persist the
        # queued recipients before the status decision below is applied.
        session.commit()
        if result.get("exhausted"):
            # Nothing is eligible yet. Keep the finished state instead of
            # dropping the campaign into an active status with no work.
            campaign.status = "ended"
            campaign.scheduled_at = None
            campaign.send_settings = {
                **(campaign.send_settings or {}),
                "pause_reason": None,
            }
        elif (
            mode == "autopilot"
            and result.get("reason_code")
            in ("autopilot_day_disabled", "autopilot_outside_window")
        ):
            # A closed campaign resumed outside its window is accepted and
            # scheduled for the next valid window, rather than reported as a
            # failed resume and left stopped.
            campaign.status = "autopilot"
            campaign.scheduled_at = next_autopilot_run(
                session,
                campaign,
                now=utcnow(),
            )
            campaign.send_settings = {
                **(campaign.send_settings or {}),
                "pause_reason": result["reason_code"],
            }
        elif resuming_closed and not result.get("created"):
            # A closed campaign that queued nothing keeps its previous status,
            # whatever blocked it. It can be resumed again once that clears.
            campaign.status = previous_status
            campaign.scheduled_at = None
            campaign.send_settings = {
                **(campaign.send_settings or {}),
                "pause_reason": result.get("reason_code"),
            }
        elif result.get("reason_code") in ("daily_caps_reached", "campaign_daily_cap_reached"):
            # Autopilot wakes up on its next scheduled day; the other modes
            # wait for someone to free sender capacity.
            if mode == "autopilot":
                campaign.status = "autopilot"
                campaign.scheduled_at = next_autopilot_run(session, campaign, now=utcnow(), force_next_day=True)
            else:
                campaign.status = "paused"
                campaign.scheduled_at = None
                campaign.send_settings = {
                    **(campaign.send_settings or {}),
                    "pause_reason": result.get("reason_code", "daily_caps_reached"),
                }
        elif result.get("reason_code") in (
            "senders_temporarily_unavailable",
            "no_connected_senders",
            "sender_unavailable",
        ):
            if mode == "autopilot":
                campaign.status = "autopilot"
                campaign.scheduled_at = next_autopilot_run(session, campaign, now=utcnow())
            else:
                campaign.status = "paused"
                campaign.scheduled_at = None
            campaign.send_settings = {
                **(campaign.send_settings or {}),
                "pause_reason": result["reason_code"],
            }
    if job_ids:
        campaign.send_settings = {
            **(campaign.send_settings or {}),
            "pause_reason": None,
        }
    session.commit()
    if not job_ids:
        if result.get("exhausted"):
            raise HTTPException(
                status_code=409,
                detail=result.get("reason", "No email was queued"),
            )
        if mode == "autopilot" and campaign.status == "autopilot":
            return {
                "status": "autopilot",
                "queued": 0,
                "reason": result.get("reason"),
                "scheduled_at": campaign.scheduled_at.isoformat() if campaign.scheduled_at else None,
            }
        raise HTTPException(
            status_code=409,
            detail=result.get("reason", "No email was queued"),
        )
    return {
        "status": campaign.status,
        "queued": len(job_ids),
        "retried_failed": retried_failed,
    }

@router.post("/api/campaigns/{campaign_id}/stop")
def post_stop(
    campaign_id: int,
    session: Session = Depends(get_session),
    user_id: str = Depends(get_current_user_id),
):
    campaign = require_campaign(session, campaign_id, user_id)
    if campaign.status == "stopped":
        return {"status": "stopped", "cancelled": 0, "in_flight": 0}
    if campaign.status not in {"sending", "scheduled", "autopilot", "paused"}:
        raise HTTPException(status_code=409, detail=f"Cannot stop a campaign that is {campaign.status}")
    cancellable_jobs = list(
        session.scalars(
            select(SendJob).where(
                SendJob.campaign_id == campaign_id,
                SendJob.status.in_(("queued", "retry")),
            )
        )
    )
    for job in cancellable_jobs:
        recipient = session.get(
            CampaignRecipient,
            {"campaign_id": campaign_id, "contact_id": job.recipient_id},
        )
        if recipient and recipient.status == "queued":
            recipient.status = "approved"
        # Remove unclaimed attempts so a later start can reuse the recipient's
        # idempotency key. Claimed work remains for the worker to finalize.
        session.delete(job)
    in_flight = session.scalar(
        select(func.count()).select_from(SendJob).where(
            SendJob.campaign_id == campaign_id,
            SendJob.status == "running",
        )
    ) or 0
    campaign.status = "stopped"
    campaign.scheduled_at = None
    session.commit()
    return {"status": "stopped", "cancelled": len(cancellable_jobs), "in_flight": in_flight}


@router.get("/api/campaigns/{campaign_id}/send-progress")
def get_campaign_send_progress(
    campaign_id: int,
    session: Session = Depends(get_session),
    user_id: str = Depends(get_current_user_id),
):
    logger = logging.getLogger("outreach.send")
    campaign = require_campaign(session, campaign_id, user_id)
    total = session.scalar(
        select(func.count()).select_from(CampaignRecipient)
        .where(CampaignRecipient.campaign_id == campaign_id)
    ) or 0
    sent = session.scalar(
        select(func.count())
        .select_from(CampaignRecipient)
        .where(
            CampaignRecipient.campaign_id == campaign_id,
            CampaignRecipient.status == "sent",
        )
    ) or 0
    failed = session.scalar(
        select(func.count())
        .select_from(CampaignRecipient)
        .where(
            CampaignRecipient.campaign_id == campaign_id,
            CampaignRecipient.status == "failed",
        )
    ) or 0
    bounced = session.scalar(
        select(func.count())
        .select_from(CampaignRecipient)
        .where(
            CampaignRecipient.campaign_id == campaign_id,
            CampaignRecipient.status == "bounced",
        )
    ) or 0
    skipped = session.scalar(
        select(func.count())
        .select_from(CampaignRecipient)
        .where(
            CampaignRecipient.campaign_id == campaign_id,
            CampaignRecipient.status == "rejected",
        )
    ) or 0
    running_job = session.scalar(
        select(SendJob).where(SendJob.campaign_id == campaign_id, SendJob.status == "running").limit(1)
    )
    queued = session.scalar(
        select(func.count()).select_from(SendJob)
        .where(SendJob.campaign_id == campaign_id, SendJob.status.in_(("queued", "retry")))
    ) or 0
    response_counts = dict(
        session.execute(
            select(GmailActivityEvent.event_type, func.count(func.distinct(SendLog.recipient_id)))
            .join(SendLog, SendLog.id == GmailActivityEvent.send_log_id)
            .join(
                CampaignRecipient,
                (CampaignRecipient.campaign_id == SendLog.campaign_id)
                & (CampaignRecipient.contact_id == SendLog.recipient_id),
            )
            .where(
                SendLog.campaign_id == campaign_id,
                SendLog.user_id == user_id,
                GmailActivityEvent.user_id == user_id,
                GmailActivityEvent.event_type.in_(("replied", "automated_response")),
                (
                    CampaignRecipient.reset_at.is_(None)
                    | (GmailActivityEvent.occurred_at >= CampaignRecipient.reset_at)
                ),
            )
            .group_by(GmailActivityEvent.event_type)
        ).all()
    )
    engagement_counts = dict(
        session.execute(
            select(
                EmailTrackingEvent.event_type,
                func.count(func.distinct(EmailTrackingEvent.send_log_id)),
            )
            .join(SendLog, SendLog.id == EmailTrackingEvent.send_log_id)
            .where(
                EmailTrackingEvent.user_id == user_id,
                EmailTrackingEvent.campaign_id == campaign_id,
                EmailTrackingEvent.event_type.in_(("opened", "clicked")),
                SendLog.status.notin_(("bounced", "failed", "error")),
            )
            .group_by(EmailTrackingEvent.event_type)
        ).all()
    )
    current_recipient = None
    if running_job:
        contact = session.get(Contact, running_job.recipient_id)
        if contact:
            current_recipient = contact.email_normalized
    campaign_sender_counts = dict(
        session.execute(
            select(SendLog.sender_id, func.count(func.distinct(SendLog.recipient_id)).label("count"))
            .join(
                CampaignRecipient,
                (CampaignRecipient.campaign_id == SendLog.campaign_id)
                & (CampaignRecipient.contact_id == SendLog.recipient_id),
            )
            .where(
                SendLog.campaign_id == campaign_id,
                SendLog.user_id == user_id,
                SendLog.status.in_(("sent", "test_sent", "bounced")),
                (CampaignRecipient.reset_at.is_(None) | (SendLog.sent_at >= CampaignRecipient.reset_at)),
            )
            .group_by(SendLog.sender_id)
        ).all()
    )
    sender_details = []
    if campaign.selected_sender_group_id:
        try:
            group = require_group(session, user_id, campaign.selected_sender_group_id)
            group_payload = serialize_group(session, group)
            for sender in group_payload["senders"]:
                daily_cap = sender["daily_cap"]
                remaining = sender["daily_cap_remaining"]
                warning_threshold = max(2, int(daily_cap * 0.2))
                sender_details.append(
                    {
                        "id": sender["id"],
                        "email": sender["email"],
                        "status": sender["status"],
                        "campaign_sent": campaign_sender_counts.get(sender["id"], 0),
                        "sent_today": sender["sent_today"],
                        "daily_cap": daily_cap,
                        "remaining_today": remaining,
                        "capacity_state": "exhausted" if remaining == 0 else "low" if remaining <= warning_threshold else "available",
                        "last_error": sender["last_error"],
                        "gmail_tracking_enabled": sender["gmail_tracking_enabled"],
                        "gmail_tracking_permission": sender["gmail_tracking_permission"],
                        "gmail_tracking_status": sender["gmail_tracking_status"],
                        "gmail_watch_expiration_at": sender["gmail_watch_expiration_at"],
                        "gmail_sync_checked_at": sender["gmail_sync_checked_at"],
                        "gmail_sync_error": sender["gmail_sync_error"],
                    }
                )
        except LookupError:
            pass
    is_running = running_job is not None
    has_pending_work = sent + bounced + failed + skipped < total
    worker_managed = campaign.status in {"sending", "scheduled", "autopilot"}
    is_waiting = worker_managed and has_pending_work and not is_running and queued == 0
    resumable = campaign.status in RESUMABLE_STATUSES and (
        bool(queued_job_ids(session, campaign_id, user_id))
        or (
            campaign.status == "paused"
            and has_failed_recipients(session, campaign_id, user_id)
        )
        or has_unsent_approved_recipients(
            session,
            campaign,
            user_id=user_id,
            include_failed=campaign.status == "paused",
        )
    )
    pause_reason = (campaign.send_settings or {}).get("pause_reason")
    day_schedules = list(
        session.scalars(
            select(AutopilotDaySchedule).where(AutopilotDaySchedule.campaign_id == campaign.id)
        )
    )
    now = utcnow()
    today_schedule = next(
        (s for s in day_schedules if s.day_of_week == WEEKDAY_NAMES[now.astimezone(campaign_zone(session, campaign)).weekday()]),
        None,
    )
    daily_capacity = campaign_daily_capacity(
        session,
        campaign,
        now=now,
        schedule=today_schedule,
    )
    result = {
        "campaign_status": campaign.status,
        "timezone": campaign_zone(session, campaign).key,
        "total_recipients": total,
        "sent_count": sent,
        "replied_count": response_counts.get("replied", 0),
        "automated_response_count": response_counts.get("automated_response", 0),
        "opened_count": engagement_counts.get("opened", 0),
        "clicked_count": engagement_counts.get("clicked", 0),
        "bounced_count": bounced,
        "send_error_count": failed,
        # Kept as a compatibility alias for older clients.
        "failed_count": failed,
        "skipped_count": skipped,
        "queued_count": queued,
        # True when a paused or closed campaign still has pending jobs or
        # recipients to queue, so the workspace can offer "Resume sending".
        "resumable": resumable,
        "is_active": worker_managed and has_pending_work,
        "is_sending": is_running,
        "is_waiting": is_waiting,
        "current_recipient": current_recipient,
        "next_batch_at": campaign.scheduled_at.isoformat() if campaign.scheduled_at else None,
        "delay_minutes": int((campaign.send_settings or {}).get("delay_minutes", 0)),
        "pacing_mode": (campaign.send_settings or {}).get("pacing_mode", "fixed_delay"),
        "send_mode": (campaign.send_settings or {}).get("mode", "send_now"),
        "send_settings": dict(campaign.send_settings or {}),
        "pause_reason": pause_reason,
        "senders": sender_details,
        "gmail_tracking": {
            "enabled": any(sender["gmail_tracking_enabled"] for sender in sender_details),
            "reconnect_required": any(not sender["gmail_tracking_permission"] for sender in sender_details),
            "setup_required": any(
                sender["gmail_tracking_permission"] and sender["gmail_tracking_status"] != "active"
                for sender in sender_details
            ),
            "last_checked_at": max(
                (
                    sender["gmail_sync_checked_at"]
                    for sender in sender_details
                    if sender["gmail_sync_checked_at"]
                ),
                default=None,
            ),
            "error": next(
                (
                    sender["gmail_sync_error"]
                    for sender in sender_details
                    if sender["gmail_sync_error"]
                ),
                None,
            ),
        },
        "autopilot_schedule": [
            {"day": s.day_of_week, "cap": s.daily_cap, "start": s.start_time, "end": s.end_time}
            for s in day_schedules
        ],
        "campaign_sent_today": daily_capacity["sent"] if daily_capacity else None,
        "campaign_daily_cap": daily_capacity["cap"] if daily_capacity else None,
        "campaign_reserved_today": daily_capacity["reserved"] if daily_capacity else None,
        "campaign_remaining_today": max(daily_capacity["remaining"], 0) if daily_capacity else None,
        "dry_run": (campaign.send_settings or {}).get("dry_run", False),
        "recipient_validation": (campaign.send_settings or {}).get("recipient_validation"),
    }
    logger.info("send-progress campaign=%s %s", campaign_id, {k: v for k, v in result.items() if k != "senders"})
    return result


@router.post("/api/campaigns/{campaign_id}/sync-gmail-activity")
def post_campaign_gmail_activity_sync(
    campaign_id: int,
    session: Session = Depends(get_session),
    user_id: str = Depends(get_current_user_id),
):
    campaign = require_campaign(session, campaign_id, user_id)
    sender_ids: list[int] = []
    if campaign.selected_sender_group_id:
        sender_ids = list(
            session.scalars(
                select(Sender.id).where(
                    Sender.user_id == user_id,
                    Sender.group_id == campaign.selected_sender_group_id,
                    Sender.status == "connected",
                )
            )
        )
    return sync_selected_gmail_activity(session, sender_ids=sender_ids)


@router.get("/api/campaigns/{campaign_id}/send-logs")
def get_campaign_send_logs(
    campaign_id: int,
    page: int = 1,
    page_size: int = 10,
    engagement: Literal[
        "all", "opened", "clicked", "replied", "undelivered"
    ] = "all",
    session: Session = Depends(get_session),
    user_id: str = Depends(get_current_user_id),
):
    require_campaign(session, campaign_id, user_id)
    page = max(page, 1)
    page_size = min(max(page_size, 1), 50)
    filters = [SendLog.campaign_id == campaign_id, SendLog.user_id == user_id]
    if engagement == "opened":
        filters.extend((SendLog.open_count > 0, SendLog.status.notin_(("bounced", "failed", "error"))))
    elif engagement == "clicked":
        filters.extend((SendLog.click_count > 0, SendLog.status.notin_(("bounced", "failed", "error"))))
    elif engagement == "replied":
        filters.append(SendLog.response_status.in_(("replied", "automated_response")))
    elif engagement == "undelivered":
        filters.append(SendLog.status == "bounced")
    total = session.scalar(
        select(func.count()).select_from(SendLog)
        .where(*filters)
    ) or 0
    attempt_partition = (SendLog.campaign_id, SendLog.recipient_id, SendLog.status)
    ranked_logs = (
        select(
            SendLog.id.label("log_id"),
            func.row_number().over(
                partition_by=attempt_partition,
                order_by=(SendLog.created_at, SendLog.id),
            ).label("attempt_number"),
            func.count().over(partition_by=attempt_partition).label("attempt_count"),
        )
        .where(SendLog.campaign_id == campaign_id, SendLog.user_id == user_id)
        .subquery()
    )
    activity_at = func.coalesce(SendLog.responded_at, SendLog.bounced_at, SendLog.sent_at, SendLog.created_at)
    logs = list(
        session.execute(
            select(SendLog, ranked_logs.c.attempt_number, ranked_logs.c.attempt_count)
            .join(ranked_logs, ranked_logs.c.log_id == SendLog.id)
            .where(*filters)
            .order_by(activity_at.desc(), SendLog.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    )
    return {
        "items": [
            {
                "id": log.id,
                "recipient_email": log.recipient_email,
                "sender_email": log.sender_email,
                "subject": log.subject,
                "gmail_thread_id": log.gmail_thread_id,
                "status": log.status,
                "response_status": log.response_status,
                "open_count": log.open_count,
                "click_count": log.click_count,
                "first_opened_at": log.first_opened_at.isoformat() if log.first_opened_at else None,
                "last_opened_at": log.last_opened_at.isoformat() if log.last_opened_at else None,
                "first_clicked_at": log.first_clicked_at.isoformat() if log.first_clicked_at else None,
                "last_clicked_at": log.last_clicked_at.isoformat() if log.last_clicked_at else None,
                "error_message": log.error_message,
                "attempt_number": attempt_number,
                "attempt_count": attempt_count,
                "sent_at": log.sent_at.isoformat() if log.sent_at else None,
                "bounced_at": log.bounced_at.isoformat() if log.bounced_at else None,
                "responded_at": log.responded_at.isoformat() if log.responded_at else None,
                "created_at": log.created_at.isoformat() if log.created_at else None,
            }
            for log, attempt_number, attempt_count in logs
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": max(1, (total + page_size - 1) // page_size),
    }


@router.get("/api/campaigns/{campaign_id}/recipients")
def get_campaign_recipients(
    campaign_id: int,
    search: str = "",
    page: int = 1,
    page_size: int = 50,
    pending_only: bool = False,
    session: Session = Depends(get_session),
    user_id: str = Depends(get_current_user_id),
):
    require_campaign(session, campaign_id, user_id)
    page = max(page, 1)
    page_size = min(max(page_size, 1), 200)

    base = (
        select(CampaignRecipient, Contact)
        .join(Contact, CampaignRecipient.contact_id == Contact.id)
        .where(CampaignRecipient.campaign_id == campaign_id, Contact.user_id == user_id)
    )
    if search:
        base = base.where(Contact.email_normalized.ilike(f"%{search.strip()}%"))

    if pending_only:
        base = base.where(
            CampaignRecipient.status.in_(("approved", "pending", "queued")),
            Contact.status.in_(("approved", "sent")),
        )

    total = session.scalar(select(func.count()).select_from(base.subquery())) or 0
    rows = list(
        session.execute(
            base.order_by(CampaignRecipient.created_at, Contact.id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    )
    recipients_by_contact = {cr.contact_id: cr for cr, _ in rows}
    activity_by_contact: dict[int, SendLog] = {}
    if recipients_by_contact:
        activity_logs = session.scalars(
            select(SendLog)
            .where(
                SendLog.campaign_id == campaign_id,
                SendLog.user_id == user_id,
                SendLog.recipient_id.in_(recipients_by_contact),
                SendLog.status.in_(("sent", "success", "bounced", "failed", "error")),
            )
            .order_by(
                func.coalesce(
                    SendLog.responded_at,
                    SendLog.bounced_at,
                    SendLog.sent_at,
                    SendLog.created_at,
                ).desc(),
                SendLog.id.desc(),
            )
        )
        for log in activity_logs:
            recipient = recipients_by_contact.get(log.recipient_id or -1)
            if not recipient or log.recipient_id in activity_by_contact:
                continue
            log_activity_at = log.responded_at or log.bounced_at or log.sent_at or log.created_at
            if recipient.reset_at and (not log_activity_at or log_activity_at < recipient.reset_at):
                continue
            activity_by_contact[log.recipient_id] = log

    return {
        "items": [
            {
                "contact_id": cr.contact_id,
                "email": c.email_normalized,
                "custom_fields": c.custom_fields or {},
                "status": cr.status if c.status in {"approved", "sent"} else c.status,
                "response_status": activity_by_contact[cr.contact_id].response_status if cr.contact_id in activity_by_contact else None,
                "delivery_detail": activity_by_contact[cr.contact_id].error_message if cr.contact_id in activity_by_contact else None,
                "responded_at": (
                    activity_by_contact[cr.contact_id].responded_at.isoformat()
                    if cr.contact_id in activity_by_contact and activity_by_contact[cr.contact_id].responded_at
                    else None
                ),
                "bounced_at": (
                    activity_by_contact[cr.contact_id].bounced_at.isoformat()
                    if cr.contact_id in activity_by_contact and activity_by_contact[cr.contact_id].bounced_at
                    else None
                ),
                "source_type": c.source_type,
                "created_at": cr.created_at.isoformat() if cr.created_at else None,
            }
            for cr, c in rows
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": max(1, (total + page_size - 1) // page_size),
    }


@router.get("/api/campaigns/{campaign_id}/send-logs/export")
def export_campaign_send_logs(
    campaign_id: int,
    session: Session = Depends(get_session),
    user_id: str = Depends(get_current_user_id),
):
    require_campaign(session, campaign_id, user_id)
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Recipient",
        "Sender",
        "Subject",
        "Delivery status",
        "Response",
        "Open count",
        "Click count",
        "First opened at (UTC)",
        "First clicked at (UTC)",
        "Sent at (UTC)",
        "Responded at (UTC)",
        "Details",
    ])
    rows = session.scalars(
        select(SendLog)
        .where(SendLog.campaign_id == campaign_id, SendLog.user_id == user_id)
        .order_by(SendLog.created_at.desc(), SendLog.id.desc())
    )
    delivery_labels = {
        "sent": "Sent",
        "success": "Sent",
        "bounced": "Undelivered",
        "failed": "Send error",
        "error": "Send error",
        "test_sent": "Test sent",
    }
    response_labels = {"replied": "Human reply", "automated_response": "Automated response"}
    for row in rows:
        values = [
            row.recipient_email,
            row.sender_email,
            row.subject,
            delivery_labels.get(row.status, row.status),
            response_labels.get(row.response_status or "", ""),
            str(row.open_count),
            str(row.click_count),
            row.first_opened_at.isoformat() if row.first_opened_at else "",
            row.first_clicked_at.isoformat() if row.first_clicked_at else "",
            row.sent_at.isoformat() if row.sent_at else "",
            row.responded_at.isoformat() if row.responded_at else "",
            row.error_message or "",
        ]
        writer.writerow(["'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value for value in values])
    return Response(
        "\ufeff" + output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="campaign_{campaign_id}_activity.csv"'},
    )


@router.delete("/api/campaigns/{campaign_id}/recipients")
def clear_campaign_recipients(
    campaign_id: int,
    session: Session = Depends(get_session),
    user_id: str = Depends(get_current_user_id),
):
    # Coordinate with delivery workers and launches before changing membership.
    campaign = session.scalar(
        select(Campaign)
        .where(Campaign.id == campaign_id, Campaign.user_id == user_id)
        .with_for_update()
    )
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if campaign.status in EDIT_LOCKED_STATUSES:
        raise HTTPException(status_code=409, detail="End this campaign before clearing its audience.")
    if session.scalar(
        select(SendJob.id).where(SendJob.campaign_id == campaign_id, SendJob.status == "running").limit(1)
    ):
        raise HTTPException(status_code=409, detail="An email is still sending. Wait for it to finish before clearing the audience.")
    session.execute(delete(SendJob).where(SendJob.campaign_id == campaign_id))
    result = session.execute(delete(CampaignRecipient).where(CampaignRecipient.campaign_id == campaign_id))
    settings = dict(campaign.send_settings or {})
    settings.pop("recipient_validation", None)
    campaign.send_settings = settings
    session.commit()
    return {"status": "success", "removed": result.rowcount}


@router.patch("/api/campaigns/{campaign_id}/recipients/{contact_id}/reset")
def patch_recipient_reset(
    campaign_id: int,
    contact_id: int,
    session: Session = Depends(get_session),
    user_id: str = Depends(get_current_user_id),
):
    require_campaign_editable(session, campaign_id, user_id)
    recipient = session.get(CampaignRecipient, {"campaign_id": campaign_id, "contact_id": contact_id})
    if not recipient:
        raise HTTPException(status_code=404, detail="Recipient not found in this campaign")
    session.execute(
        delete(SendJob).where(
            SendJob.campaign_id == campaign_id,
            SendJob.recipient_id == contact_id,
        )
    )
    recipient.status = "approved"
    contact = session.get(Contact, contact_id)
    if contact and contact.user_id == user_id and contact.status in {"pending", "sent", "bounced"}:
        contact.status = "approved"
    recipient.reset_at = utcnow()
    session.commit()
    return {"status": "success", "reset_at": recipient.reset_at.isoformat()}


@router.delete("/api/campaigns/{campaign_id}/recipients/{contact_id}")
def delete_campaign_recipient(
    campaign_id: int,
    contact_id: int,
    session: Session = Depends(get_session),
    user_id: str = Depends(get_current_user_id),
):
    require_campaign_editable(session, campaign_id, user_id)
    recipient = session.get(CampaignRecipient, {"campaign_id": campaign_id, "contact_id": contact_id})
    if not recipient:
        raise HTTPException(status_code=404, detail="Recipient not found in this campaign")
    session.execute(
        delete(SendJob).where(
            SendJob.campaign_id == campaign_id,
            SendJob.recipient_id == contact_id,
        )
    )
    session.delete(recipient)
    session.commit()
    return {"status": "success"}
