from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from . import db
from .dnc import is_blocked
from .models import AppConfig, ContactStatus


PAUSE_ERROR_MARKERS = (
    "rate limit",
    "ratelimit",
    "user ratelimit exceeded",
    "userRateLimitExceeded",
    "dailyLimitExceeded",
    "quota",
    "suspicious",
    "abuse",
    "429",
)


@dataclass(frozen=True)
class SafetyResult:
    allowed: bool
    reason: str = ""


def local_now(config: AppConfig, now: datetime | None = None) -> datetime:
    zone = ZoneInfo(config.timezone)
    if now is None:
        return datetime.now(zone)
    if now.tzinfo is None:
        return now.replace(tzinfo=zone)
    return now.astimezone(zone)


def parse_hhmm(value: str) -> time:
    hour, minute = value.split(":", 1)
    return time(hour=int(hour), minute=int(minute))


def is_allowed_send_time(config: AppConfig, now: datetime | None = None) -> bool:
    current = local_now(config, now=now)
    day = current.strftime("%A").lower()
    if day not in config.sending.days:
        return False
    start = parse_hhmm(config.sending.start_time)
    end = parse_hhmm(config.sending.end_time)
    return start <= current.time().replace(second=0, microsecond=0) <= end


def warmup_daily_limit(conn, now: datetime | None, config: AppConfig, user_id: str) -> int:
    current = local_now(config, now=now)
    first_sent = db.first_successful_send_date(conn, user_id)
    if not first_sent:
        campaign_day = 1
    else:
        first_dt = datetime.fromisoformat(first_sent)
        if first_dt.tzinfo is None:
            first_dt = first_dt.replace(tzinfo=ZoneInfo(config.timezone))
        first_local = first_dt.astimezone(ZoneInfo(config.timezone)).date()
        campaign_day = max((current.date() - first_local).days + 1, 1)
    if campaign_day <= 1:
        return 5
    if campaign_day == 2:
        return 10
    if campaign_day == 3:
        return 15
    if campaign_day == 4:
        return 20
    return 30


def effective_daily_cap(conn, config: AppConfig, user_id: str, now: datetime | None = None) -> int:
    return min(
        config.sending.daily_cap,
        config.sending.max_daily_cap_allowed_without_manual_override,
        warmup_daily_limit(conn, now, config, user_id),
    )


def sent_today_local(conn, config: AppConfig, user_id: str, now: datetime | None = None) -> int:
    current = local_now(config, now=now)
    rows = conn.execute("SELECT sent_at FROM send_log WHERE status = 'sent' AND user_id = ?", (user_id,)).fetchall()
    count = 0
    for row in rows:
        if not row["sent_at"]:
            continue
        sent = datetime.fromisoformat(str(row["sent_at"]))
        if sent.tzinfo is None:
            sent = sent.replace(tzinfo=ZoneInfo("UTC"))
        if sent.astimezone(ZoneInfo(config.timezone)).date() == current.date():
            count += 1
    return count


def sent_today_for_sender(conn, sender_id: int, config: AppConfig, user_id: str, now: datetime | None = None) -> int:
    current = local_now(config, now=now)
    rows = conn.execute(
        "SELECT sent_at FROM send_log WHERE status = 'sent' AND sender_id = ? AND user_id = ?",
        (sender_id, user_id),
    ).fetchall()
    count = 0
    for row in rows:
        if not row["sent_at"]:
            continue
        sent = datetime.fromisoformat(str(row["sent_at"]))
        if sent.tzinfo is None:
            sent = sent.replace(tzinfo=ZoneInfo("UTC"))
        if sent.astimezone(ZoneInfo(config.timezone)).date() == current.date():
            count += 1
    return count


def has_remaining_daily_capacity(conn, config: AppConfig, user_id: str, now: datetime | None = None) -> bool:
    return sent_today_local(conn, config, user_id, now=now) < effective_daily_cap(conn, config, user_id, now=now)


def effective_sender_daily_cap(
    conn,
    config: AppConfig,
    sender_daily_cap: int,
    user_id: str,
    now: datetime | None = None,
) -> int:
    return min(
        sender_daily_cap,
        config.sending.max_daily_cap_allowed_without_manual_override,
        warmup_daily_limit(conn, now, config, user_id),
    )


def has_remaining_sender_capacity(
    conn,
    config: AppConfig,
    sender_id: int,
    sender_daily_cap: int,
    user_id: str,
    now: datetime | None = None,
) -> bool:
    sent_today = sent_today_for_sender(conn, sender_id, config, user_id, now=now)
    return sent_today < effective_sender_daily_cap(conn, config, sender_daily_cap, user_id, now=now)


def delay_elapsed(conn, config: AppConfig, user_id: str, now: datetime | None = None) -> bool:
    last_sent = db.last_successful_send_at(conn, user_id)
    if not last_sent:
        return True
    last_dt = datetime.fromisoformat(last_sent)
    if last_dt.tzinfo is None:
        last_dt = last_dt.replace(tzinfo=ZoneInfo("UTC"))
    current = local_now(config, now=now)
    return current - last_dt.astimezone(current.tzinfo) >= timedelta(minutes=config.sending.delay_minutes)


def attachment_check(config: AppConfig, campaign) -> SafetyResult:
    # Filesystem paths were a legacy attachment mechanism. The delivery path
    # accepts only database-backed attachment bytes now, so this check must not
    # inspect a path supplied by a campaign record.
    return SafetyResult(True)


def pre_send_checks(
    conn,
    contact,
    campaign,
    config: AppConfig,
    user_id: str,
    now: datetime | None = None,
    enforce_time_window: bool = True,
    enforce_daily_cap: bool = True,
    sender_id: int | None = None,
    sender_daily_cap: int | None = None,
) -> SafetyResult:
    if contact is None:
        return SafetyResult(False, "Contact not found")

    email = str(contact["email"] or "").strip().lower()
    if not email:
        return SafetyResult(False, "Missing recipient email")
    if is_blocked(conn, email, user_id):
        return SafetyResult(False, "Recipient is on do-not-contact list")
    if str(contact["status"]) != ContactStatus.APPROVED.value:
        return SafetyResult(False, "Contact is not approved")
    if str(contact["status"]) in {
        ContactStatus.SENT.value,
        ContactStatus.REPLIED.value,
        ContactStatus.BOUNCED.value,
        ContactStatus.DO_NOT_CONTACT.value,
    }:
        return SafetyResult(False, f"Contact status is {contact['status']}")
    if not contact["preview_generated_at"] or not contact["last_preview_subject"] or not contact["last_preview_body"]:
        return SafetyResult(False, "Preview has not been generated")
    if "[missing " in str(contact["last_preview_subject"]) or "[missing " in str(contact["last_preview_body"]):
        return SafetyResult(False, "Recipient has missing template variables")
    duplicate_count = conn.execute(
        "SELECT COUNT(*) AS count FROM contacts WHERE email = ? AND user_id = ?",
        (email, user_id),
    ).fetchone()["count"]
    if int(duplicate_count) > 1:
        return SafetyResult(False, "Duplicate recipient email exists")
    if db.has_send_attempt(conn, int(contact["id"]), user_id):
        return SafetyResult(False, "Recipient already has a send attempt or sent log")
    attachment = attachment_check(config, campaign)
    if not attachment.allowed:
        return attachment
    if enforce_time_window and not is_allowed_send_time(config, now=now):
        return SafetyResult(False, "Outside allowed sending window")
    if enforce_daily_cap and not has_remaining_daily_capacity(conn, config, user_id, now=now):
        return SafetyResult(False, "Daily cap reached")
    if (
        enforce_daily_cap
        and sender_id is not None
        and sender_daily_cap is not None
        and not has_remaining_sender_capacity(conn, config, sender_id, sender_daily_cap, user_id, now=now)
    ):
        return SafetyResult(False, "Selected sender daily cap reached")
    if enforce_daily_cap and not delay_elapsed(conn, config, user_id, now=now):
        return SafetyResult(False, "Delay between emails has not elapsed")
    if too_many_consecutive_errors(conn, user_id, config.sending.max_consecutive_errors):
        return SafetyResult(False, "Too many consecutive errors")
    if high_bounce_rate(conn, user_id, config.sending.bounce_rate_pause_threshold):
        return SafetyResult(False, "Bounce rate threshold exceeded")
    return SafetyResult(True)


def too_many_consecutive_errors(conn, user_id: str, max_errors: int) -> bool:
    rows = db.recent_send_errors(conn, max_errors, user_id)
    if len(rows) < max_errors:
        return False
    return all(str(row["status"]) == "failed" for row in rows)


def high_bounce_rate(conn, user_id: str, threshold_percent: float) -> bool:
    return db.bounce_rate_percent(conn, user_id) > threshold_percent


def should_pause_for_error(error_message: str) -> bool:
    lowered = error_message.lower()
    return any(marker.lower() in lowered for marker in PAUSE_ERROR_MARKERS)


def should_pause_campaign(conn, config: AppConfig, user_id: str, last_error: str | None = None) -> SafetyResult:
    if last_error and should_pause_for_error(last_error):
        return SafetyResult(True, "Gmail returned a rate-limit, quota, or suspicious activity error")
    if too_many_consecutive_errors(conn, user_id, config.sending.max_consecutive_errors):
        return SafetyResult(True, f"{config.sending.max_consecutive_errors} consecutive send errors")
    if high_bounce_rate(conn, user_id, config.sending.bounce_rate_pause_threshold):
        return SafetyResult(True, "Bounce rate threshold exceeded")
    return SafetyResult(False)


def next_send_time(config: AppConfig, now: datetime | None = None) -> str:
    current = local_now(config, now=now).replace(second=0, microsecond=0)
    start_time = parse_hhmm(config.sending.start_time)
    end_time = parse_hhmm(config.sending.end_time)

    for offset in range(0, 14):
        candidate_date = current.date() + timedelta(days=offset)
        candidate_day = candidate_date.strftime("%A").lower()
        if candidate_day not in config.sending.days:
            continue
        start_dt = datetime.combine(candidate_date, start_time, tzinfo=current.tzinfo)
        end_dt = datetime.combine(candidate_date, end_time, tzinfo=current.tzinfo)
        if offset == 0 and current <= end_dt:
            return max(current, start_dt).isoformat(timespec="minutes")
        if offset > 0:
            return start_dt.isoformat(timespec="minutes")
    return ""


def attachment_name(config: AppConfig, campaign) -> str:
    return ""


def campaign_checklist(conn, config: AppConfig, campaign, gmail_status, user_id: str) -> dict[str, bool]:
    campaign_id = int(campaign["id"])
    recipient_count = db.campaign_contact_count(conn, campaign_id)
    preview_count = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM contacts c
        INNER JOIN campaign_recipients cr ON cr.contact_id = c.id
        WHERE cr.campaign_id = ? AND c.user_id = ? AND c.preview_generated_at IS NOT NULL
        """,
        (campaign_id, user_id),
    ).fetchone()["count"]
    approved_count = len(db.campaign_contacts(conn, campaign_id, user_id, statuses=(ContactStatus.APPROVED.value,)))
    attachment = attachment_check(config, campaign)
    test_sent = bool(db.get_setting(conn, f"campaign_{campaign_id}_test_sent", False, user_id))
    return {
        "Gmail connected": gmail_status.connected,
        "Recipients selected": recipient_count > 0,
        "Attachment added": attachment.allowed,
        "Preview generated": int(preview_count) > 0,
        "Test sent": test_sent,
        "Approved recipients": approved_count > 0,
    }
