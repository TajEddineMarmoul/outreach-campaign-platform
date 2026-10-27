from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import Iterable

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from src.gmail_sender import GMAIL_READONLY_SCOPE
from src.platform.models import (
    Campaign,
    AutopilotDaySchedule,
    Contact,
    OAuthState,
    Sender,
    SenderGroup,
    SendLog,
    SendJob,
    User,
    UserSettings,
)
from src.platform.time import utcnow


CONNECTED_SENDER_STATUSES = {"connected"}
WEEKDAY_NAMES = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
SENDER_ERROR_COOLDOWN = timedelta(minutes=15)
AUTOPILOT_PACING_FIXED = "fixed_delay"
AUTOPILOT_PACING_SPREAD = "spread_evenly"


def _aware_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def user_zone(session: Session, user_id: str) -> ZoneInfo:
    settings = session.get(UserSettings, user_id)
    try:
        return ZoneInfo(settings.timezone if settings else "UTC")
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def campaign_zone(session: Session, campaign: Campaign) -> ZoneInfo:
    """Return the campaign's fixed schedule zone, falling back for legacy rows."""
    if campaign.timezone:
        try:
            return ZoneInfo(campaign.timezone)
        except ZoneInfoNotFoundError:
            pass
    return user_zone(session, campaign.user_id)


def validate_timezone_name(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("Unknown IANA timezone") from exc
    return value


def local_day_bounds(session: Session, user_id: str, *, now: datetime | None = None) -> tuple[datetime, datetime]:
    zone = user_zone(session, user_id)
    return day_bounds_for_zone(zone, now=now)


def day_bounds_for_zone(zone: ZoneInfo, *, now: datetime | None = None) -> tuple[datetime, datetime]:
    local_midnight = _aware_utc(now or utcnow()).astimezone(zone).replace(hour=0, minute=0, second=0, microsecond=0)
    return local_midnight.astimezone(timezone.utc), (local_midnight + timedelta(days=1)).astimezone(timezone.utc)


def _clock(value: str, fallback: time) -> time:
    try:
        return time.fromisoformat(value)
    except (TypeError, ValueError):
        return fallback


def _day_schedules(session: Session, campaign_id: int) -> dict[str, AutopilotDaySchedule]:
    schedules = session.scalars(
        select(AutopilotDaySchedule).where(AutopilotDaySchedule.campaign_id == campaign_id)
    )
    return {schedule.day_of_week: schedule for schedule in schedules}


def next_autopilot_run(
    session: Session,
    campaign: Campaign,
    *,
    now: datetime | None = None,
    force_next_day: bool = False,
) -> datetime:
    current = _aware_utc(now or utcnow())
    zone = campaign_zone(session, campaign)
    local_now = current.astimezone(zone)
    schedules = _day_schedules(session, campaign.id)
    settings = campaign.send_settings or {}
    allowed_days = set(schedules) if schedules else set(settings.get("days") or WEEKDAY_NAMES[:5])
    first_offset = 1 if force_next_day else 0
    for offset in range(first_offset, 9):
        candidate_date = local_now.date() + timedelta(days=offset)
        day_name = WEEKDAY_NAMES[candidate_date.weekday()]
        if day_name not in allowed_days:
            continue
        schedule = schedules.get(day_name)
        start = _clock(schedule.start_time if schedule else str(settings.get("start_time", "09:00")), time(9, 0))
        end = _clock(schedule.end_time if schedule else str(settings.get("end_time", "17:00")), time(17, 0))
        start_at = datetime.combine(candidate_date, start, tzinfo=zone)
        end_at = datetime.combine(candidate_date, end, tzinfo=zone)
        if offset == 0 and start_at <= local_now < end_at:
            return current
        if start_at > local_now:
            return start_at.astimezone(timezone.utc)
    return (local_now + timedelta(days=1)).astimezone(timezone.utc)


def autopilot_window_state(session: Session, campaign: Campaign, *, now: datetime | None = None) -> dict:
    current = _aware_utc(now or utcnow())
    zone = campaign_zone(session, campaign)
    local_now = current.astimezone(zone)
    schedules = _day_schedules(session, campaign.id)
    settings = campaign.send_settings or {}
    day_name = WEEKDAY_NAMES[local_now.weekday()]
    allowed_days = set(schedules) if schedules else set(settings.get("days") or WEEKDAY_NAMES[:5])
    if day_name not in allowed_days:
        return {
            "allowed": False,
            "reason_code": "autopilot_day_disabled",
            "reason": "Autopilot is disabled today",
            "next_at": next_autopilot_run(session, campaign, now=current),
            "schedule": None,
        }
    schedule = schedules.get(day_name)
    start = _clock(schedule.start_time if schedule else str(settings.get("start_time", "09:00")), time(9, 0))
    end = _clock(schedule.end_time if schedule else str(settings.get("end_time", "17:00")), time(17, 0))
    start_at = datetime.combine(local_now.date(), start, tzinfo=zone)
    end_at = datetime.combine(local_now.date(), end, tzinfo=zone)
    if local_now < start_at or local_now >= end_at:
        return {
            "allowed": False,
            "reason_code": "autopilot_outside_window",
            "reason": "Autopilot is outside today's sending window",
            "next_at": next_autopilot_run(session, campaign, now=current),
            "schedule": schedule,
        }
    return {
        "allowed": True,
        "reason_code": None,
        "reason": None,
        "next_at": current,
        "schedule": schedule,
        "start_at": start_at.astimezone(timezone.utc),
        "end_at": end_at.astimezone(timezone.utc),
    }


def ensure_user(session: Session, user_id: str, email: str | None = None) -> User:
    user = session.get(User, user_id)
    if user:
        if email and user.email != email:
            user.email = email
        if session.get(UserSettings, user_id) is None:
            session.add(UserSettings(user_id=user_id))
            session.flush()
        return user
    user = User(id=user_id, email=email)
    session.add(user)
    session.add(UserSettings(user_id=user_id))
    session.flush()
    return user


def set_user_timezone(session: Session, user_id: str, timezone_name: str) -> bool:
    validate_timezone_name(timezone_name)
    ensure_user(session, user_id)
    settings = session.get(UserSettings, user_id)
    changed = settings.timezone != timezone_name
    settings.timezone = timezone_name
    session.flush()
    return changed


def require_group(session: Session, user_id: str, group_id: int) -> SenderGroup:
    group = session.scalar(
        select(SenderGroup)
        .options(selectinload(SenderGroup.senders), selectinload(SenderGroup.members))
        .where(SenderGroup.id == group_id, SenderGroup.user_id == user_id)
    )
    if not group:
        raise LookupError("Sender group not found")
    return group


def group_senders(group: SenderGroup) -> list[Sender]:
    """Return each saved account once, including its original legacy membership."""
    return list({sender.id: sender for sender in [*group.senders, *getattr(group, "members", [])]}.values())


def sender_group_filter(group_id: int):
    return or_(Sender.group_id == group_id, Sender.groups.any(SenderGroup.id == group_id))


def connected_senders(group: SenderGroup) -> list[Sender]:
    return sorted(
        [sender for sender in group_senders(group) if sender.status == "connected" and sender.encrypted_oauth_credentials],
        key=lambda sender: sender.id,
    )


def sender_sent_count_today(
    session: Session,
    sender_id: int,
    *,
    now: datetime | None = None,
) -> int:
    sender = session.get(Sender, sender_id)
    if not sender:
        return 0
    start, end = local_day_bounds(session, sender.user_id, now=now)
    return int(
        session.scalar(
            select(func.count())
            .select_from(SendLog)
            .where(
                SendLog.sender_id == sender_id,
                SendLog.status.in_(("sent", "bounced")),
                SendLog.sent_at >= start,
                SendLog.sent_at < end,
            )
        )
        or 0
    )


def campaign_sent_today(
    session: Session,
    campaign_id: int,
    *,
    now: datetime | None = None,
) -> int:
    campaign = session.get(Campaign, campaign_id)
    if not campaign:
        return 0
    start, end = day_bounds_for_zone(campaign_zone(session, campaign), now=now)
    return int(
        session.scalar(
            select(func.count())
            .select_from(SendLog)
            .where(
                SendLog.campaign_id == campaign_id,
                SendLog.status.in_(("sent", "test_sent", "bounced")),
                SendLog.sent_at >= start,
                SendLog.sent_at < end,
            )
        )
        or 0
    )


def campaign_reserved_today(
    session: Session,
    campaign: Campaign,
    *,
    now: datetime | None = None,
) -> int:
    """Count queued work already reserved against today's campaign cap."""
    start, end = day_bounds_for_zone(campaign_zone(session, campaign), now=now)
    return int(
        session.scalar(
            select(func.count())
            .select_from(SendJob)
            .where(
                SendJob.campaign_id == campaign.id,
                SendJob.status.in_(("queued", "running", "retry")),
                SendJob.scheduled_for >= start,
                SendJob.scheduled_for < end,
            )
        )
        or 0
    )


def campaign_daily_capacity(
    session: Session,
    campaign: Campaign,
    *,
    now: datetime | None = None,
    schedule: AutopilotDaySchedule | None = None,
) -> dict | None:
    """Return the campaign's cap usage for one consistent local-day snapshot."""
    current = _aware_utc(now or utcnow())
    day_schedule = schedule
    if day_schedule is None:
        day_schedule = autopilot_window_state(session, campaign, now=current).get("schedule")
    if day_schedule is None:
        return None

    sent = campaign_sent_today(session, campaign.id, now=current)
    reserved = campaign_reserved_today(session, campaign, now=current)
    return {
        "cap": day_schedule.daily_cap,
        "sent": sent,
        "reserved": reserved,
        "remaining": day_schedule.daily_cap - sent - reserved,
    }


def autopilot_reschedule_state(
    session: Session,
    campaign: Campaign,
    *,
    now: datetime | None = None,
    next_candidate: datetime | None = None,
) -> dict:
    """Choose the next wakeup without briefly reactivating an exhausted day."""
    current = _aware_utc(now or utcnow())
    window = autopilot_window_state(session, campaign, now=current)
    capacity = campaign_daily_capacity(
        session,
        campaign,
        now=current,
        schedule=window.get("schedule"),
    )
    if capacity is not None and capacity["remaining"] <= 0:
        return {
            "next_at": next_autopilot_run(session, campaign, now=current, force_next_day=True),
            "pause_reason": "campaign_daily_cap_reached",
            "capacity": capacity,
        }

    return {
        "next_at": next_autopilot_run(session, campaign, now=next_candidate or current),
        "pause_reason": window.get("reason_code") if not window["allowed"] else None,
        "capacity": capacity,
    }


def evenly_spaced_next_batch_at(
    *,
    current: datetime,
    start_at: datetime,
    end_at: datetime,
    daily_target: int,
    batch_size: int,
) -> datetime:
    """Use the full window to set a steady rate, without catching up missed sends."""
    current_utc = _aware_utc(current)
    duration = _aware_utc(end_at) - _aware_utc(start_at)
    interval = duration * min(max(batch_size, 1), max(daily_target, 1)) / max(daily_target, 1)
    return current_utc + max(interval, timedelta(seconds=1))


def campaign_batch_schedule(
    session: Session,
    campaign: Campaign,
    *,
    after: datetime,
    delay_minutes: int,
    batch_size: int,
) -> dict:
    """Return one canonical next-batch decision for API and worker delivery paths."""
    current = _aware_utc(after)
    settings = campaign.send_settings or {}
    is_autopilot = settings.get("mode") == "autopilot"
    pacing_mode = settings.get("pacing_mode", AUTOPILOT_PACING_FIXED)
    candidate = current + timedelta(minutes=max(delay_minutes, 0))

    if is_autopilot and pacing_mode == AUTOPILOT_PACING_SPREAD:
        window = autopilot_window_state(session, campaign, now=current)
        if not window["allowed"]:
            return {
                "next_at": window["next_at"],
                "pause_reason": window["reason_code"],
                "capacity": None,
            }
        capacity = campaign_daily_capacity(
            session,
            campaign,
            now=current,
            schedule=window["schedule"],
        )
        if capacity is not None and capacity["remaining"] <= 0:
            return {
                "next_at": next_autopilot_run(session, campaign, now=current, force_next_day=True),
                "pause_reason": "campaign_daily_cap_reached",
                "capacity": capacity,
            }
        if capacity is not None:
            candidate = evenly_spaced_next_batch_at(
                current=current,
                start_at=window["start_at"],
                end_at=window["end_at"],
                daily_target=capacity["cap"],
                batch_size=batch_size,
            )

    if not is_autopilot:
        return {"next_at": candidate, "pause_reason": None, "capacity": None}
    return autopilot_reschedule_state(
        session,
        campaign,
        now=current,
        next_candidate=candidate,
    )


def reschedule_after_campaign_edit(
    session: Session, campaign: Campaign, *, now: datetime | None = None,
) -> None:
    """Apply a live edit at the new pace, retaining in-flight work and daily usage."""
    if campaign.status not in {"autopilot", "sending"}:
        return
    current = _aware_utc(now or utcnow())
    settings = dict(campaign.send_settings or {})
    next_at = current + timedelta(minutes=max(int(settings.get("delay_minutes", 5)), 0))
    reason = None
    if settings.get("mode") == "autopilot":
        window = autopilot_window_state(session, campaign, now=current)
        capacity = campaign_daily_capacity(session, campaign, now=current, schedule=window.get("schedule"))
        # Queued reservations may still send today. Only delivered emails can
        # exhaust the day when moving those reservations to the new schedule.
        if capacity is not None and capacity["sent"] >= capacity["cap"]:
            next_at = next_autopilot_run(session, campaign, now=current, force_next_day=True)
            reason = "campaign_daily_cap_reached"
        elif not window["allowed"]:
            next_at, reason = window["next_at"], window["reason_code"]
        else:
            if capacity is not None and settings.get("pacing_mode") == AUTOPILOT_PACING_SPREAD:
                batch_size = 1
                if campaign.selected_sender_group_id:
                    group = require_group(session, campaign.user_id, campaign.selected_sender_group_id)
                    batch_size = max(len(connected_senders(group)), 1)
                next_at = evenly_spaced_next_batch_at(
                    current=current, start_at=window["start_at"], end_at=window["end_at"],
                    daily_target=capacity["cap"], batch_size=batch_size,
                )
            next_at = next_autopilot_run(session, campaign, now=next_at)
    campaign.scheduled_at = next_at
    campaign.send_settings = {**settings, "pause_reason": reason}
    # Never bring an error retry forward, and never alter a claimed send.
    for job in session.scalars(select(SendJob).where(
        SendJob.campaign_id == campaign.id, SendJob.status.in_(("queued", "retry")),
    )):
        job.scheduled_for = max(_aware_utc(job.scheduled_for), next_at) if job.status == "retry" else next_at


def sender_reserved_today(
    session: Session,
    sender: Sender,
    *,
    now: datetime | None = None,
) -> int:
    """Count active jobs holding one of the sender's slots for the local day."""
    start, end = local_day_bounds(session, sender.user_id, now=now)
    return int(
        session.scalar(
            select(func.count())
            .select_from(SendJob)
            .where(
                SendJob.sender_id == sender.id,
                SendJob.status.in_(("queued", "running", "retry")),
                SendJob.scheduled_for >= start,
                SendJob.scheduled_for < end,
            )
        )
        or 0
    )


def delivery_policy_state(
    session: Session,
    campaign: Campaign,
    sender: Sender,
    *,
    now: datetime | None = None,
) -> dict:
    """Evaluate mutable send constraints immediately before external delivery."""
    current = _aware_utc(now or utcnow())
    if (campaign.send_settings or {}).get("mode") == "autopilot":
        window = autopilot_window_state(session, campaign, now=current)
        if not window["allowed"]:
            return window

        capacity = campaign_daily_capacity(
            session,
            campaign,
            now=current,
            schedule=window["schedule"],
        )
        if capacity is not None:
            # The running job already owns one reservation. Zero remaining is
            # valid for that job; a negative value means the cap was exceeded.
            if capacity["remaining"] < 0:
                return {
                    "allowed": False,
                    "reason_code": "campaign_daily_cap_reached",
                    "reason": "Campaign reached its daily sending limit",
                    "next_at": next_autopilot_run(
                        session,
                        campaign,
                        now=current,
                        force_next_day=True,
                    ),
                }

    sent_by_sender = sender_sent_count_today(session, sender.id, now=current)
    sender_reservations = sender_reserved_today(session, sender, now=current)
    if sent_by_sender + sender_reservations > sender.daily_cap:
        return {
            "allowed": False,
            "reason_code": "sender_daily_cap_reached",
            "reason": f"{sender.email} reached its daily sending limit",
            "next_at": None,
        }

    if sender.recent_error_at:
        cooldown_ends_at = _aware_utc(sender.recent_error_at) + SENDER_ERROR_COOLDOWN
        if current < cooldown_ends_at:
            return {
                "allowed": False,
                "reason_code": "sender_cooldown",
                "reason": f"{sender.email} is cooling down after a delivery error",
                "next_at": cooldown_ends_at,
            }

    return {"allowed": True, "reason_code": None, "reason": None, "next_at": current}


def eligible_senders(
    session: Session,
    group: SenderGroup,
    *,
    lock: bool = False,
    now: datetime | None = None,
) -> list[Sender]:
    eligible: list[Sender] = []
    candidates = connected_senders(group)
    if lock:
        candidate_ids = [sender.id for sender in candidates]
        candidates = list(
            session.scalars(
                select(Sender)
                .where(Sender.id.in_(candidate_ids))
                .order_by(Sender.id)
                .with_for_update()
            )
        ) if candidate_ids else []
    for sender in candidates:
        sender_reserved = sender_reserved_today(session, sender, now=now)
        if sender_sent_count_today(session, sender.id, now=now) + sender_reserved >= sender.daily_cap:
            continue
        current = _aware_utc(now or utcnow())
        if (
            sender.recent_error_at
            and current - _aware_utc(sender.recent_error_at) < SENDER_ERROR_COOLDOWN
        ):
            continue
        eligible.append(sender)
    return eligible


def serialize_sender(session: Session, sender: Sender) -> dict:
    sent_today = sender_sent_count_today(session, sender.id)
    gmail_tracking_permission = GMAIL_READONLY_SCOPE in set(sender.scopes or [])
    gmail_tracking_enabled = gmail_tracking_permission and sender.gmail_tracking_status == "active"
    return {
        "id": sender.id,
        "group_id": sender.group_id,
        "group_ids": sorted({group.id for group in sender.groups} | ({sender.group_id} if sender.group_id is not None else set())),
        "email": sender.email,
        "display_name": sender.display_name,
        "daily_cap": sender.daily_cap,
        "is_default": bool(sender.is_default),
        "status": sender.status,
        "connected_at": sender.connected_at.isoformat() if sender.connected_at else None,
        "revoked_at": sender.revoked_at.isoformat() if sender.revoked_at else None,
        "removed_at": sender.removed_at.isoformat() if sender.removed_at else None,
        "last_error": sender.last_error,
        "gmail_tracking_enabled": gmail_tracking_enabled,
        "gmail_tracking_permission": gmail_tracking_permission,
        "gmail_tracking_status": sender.gmail_tracking_status,
        "gmail_watch_expiration_at": sender.gmail_watch_expiration_at.isoformat() if sender.gmail_watch_expiration_at else None,
        "gmail_sync_checked_at": sender.gmail_sync_checked_at.isoformat() if sender.gmail_sync_checked_at else None,
        "gmail_sync_error": sender.gmail_sync_error,
        "sent_today": sent_today,
        "daily_cap_remaining": max(sender.daily_cap - sent_today, 0),
    }


def serialize_group(session: Session, group: SenderGroup) -> dict:
    active_senders = [s for s in group_senders(group) if s.status != "removed"]
    connected = [s for s in active_senders if s.status == "connected" and s.encrypted_oauth_credentials]
    return {
        "id": group.id,
        "name": group.name,
        "created_at": group.created_at.isoformat(),
        "updated_at": group.updated_at.isoformat(),
        "senders": [serialize_sender(session, sender) for sender in active_senders],
        "connected_sender_count": len(connected),
        "sender_count": len(active_senders),
        "total_daily_cap": sum(sender.daily_cap for sender in connected),
        "error_sender_count": len([s for s in active_senders if s.status == "error"]),
    }


def upsert_connected_sender(
    session: Session,
    *,
    user_id: str,
    group_id: int,
    email: str,
    display_name: str,
    encrypted_credentials: str,
    scopes: Iterable[str],
) -> Sender:
    normalized = email.strip().lower()
    granted_scopes = list(scopes)
    tracking_status = "pending" if GMAIL_READONLY_SCOPE in set(granted_scopes) else "needs_reconnect"
    sender = session.scalar(select(Sender).where(Sender.user_id == user_id, Sender.email == normalized))
    now = utcnow()
    existing_default = session.scalar(
        select(Sender).where(Sender.user_id == user_id, Sender.is_default == 1, Sender.status != "removed")
    )
    if sender:
        sender.display_name = display_name or sender.display_name or ""
        sender.encrypted_oauth_credentials = encrypted_credentials
        sender.scopes = granted_scopes
        sender.status = "connected"
        sender.connected_at = now
        sender.revoked_at = None
        sender.removed_at = None
        sender.last_error = None
        sender.recent_error_at = None
        sender.gmail_tracking_status = tracking_status
        sender.gmail_history_id = None
        sender.gmail_pending_history_id = None
        sender.gmail_watch_expiration_at = None
        sender.gmail_watch_refresh_at = None
        sender.gmail_backfill_completed_at = None
        sender.gmail_push_pending = 0
        sender.gmail_push_locked_at = None
        sender.gmail_push_retry_at = None
        sender.gmail_sync_checked_at = None
        sender.gmail_sync_error = None
        if not existing_default:
            sender.is_default = 1
    else:
        sender = Sender(
            user_id=user_id,
            group_id=group_id,
            email=normalized,
            display_name=display_name,
            encrypted_oauth_credentials=encrypted_credentials,
            scopes=granted_scopes,
            status="connected",
            gmail_tracking_status=tracking_status,
            daily_cap=10,
            connected_at=now,
            is_default=1 if existing_default is None else 0,
        )
        session.add(sender)
    session.flush()
    group = require_group(session, user_id, group_id)
    if sender not in group.members:
        group.members.append(sender)
    session.flush()
    return sender


def mark_sender_removed(sender: Sender) -> None:
    now = utcnow()
    sender.status = "removed"
    sender.encrypted_oauth_credentials = None
    sender.revoked_at = now
    sender.removed_at = now
    sender.gmail_tracking_status = "disabled"
    sender.gmail_history_id = None
    sender.gmail_pending_history_id = None
    sender.gmail_watch_expiration_at = None
    sender.gmail_watch_refresh_at = None
    sender.gmail_backfill_completed_at = None
    sender.gmail_push_pending = 0
    sender.gmail_push_locked_at = None
    sender.gmail_push_retry_at = None
    sender.gmail_sync_checked_at = None
    sender.gmail_sync_error = None


def mark_oauth_state_used(state: OAuthState) -> None:
    state.used_at = utcnow()
