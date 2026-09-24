from __future__ import annotations

import sqlite3
import time
from datetime import datetime
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler

from . import db
from .gmail_sender import gmail_connection_status, send_email
from .models import AppConfig, ContactStatus, load_config
from .safety import (
    attachment_name,
    is_allowed_send_time,
    pre_send_checks,
    should_pause_campaign,
)


_BACKGROUND_SCHEDULER: BackgroundScheduler | None = None


def next_approved_contact(conn, campaign_id: int | None = None):
    if campaign_id is None:
        return conn.execute(
            """
            SELECT *
            FROM contacts
            WHERE status = 'approved'
            ORDER BY id
            LIMIT 1
            """
        ).fetchone()
    return conn.execute(
        """
        SELECT c.*
        FROM contacts c
        INNER JOIN campaign_recipients cr ON cr.contact_id = c.id
        WHERE cr.campaign_id = ? AND c.status = 'approved'
        ORDER BY c.id
        LIMIT 1
        """,
        (campaign_id,),
    ).fetchone()


def attachment_path_for_send(config: AppConfig, campaign) -> str | None:
    # Legacy campaigns stored a server filesystem path here. Never resolve or
    # attach caller-controlled server paths; delivery uses database-backed
    # CampaignAttachment records only.
    return None


def send_contact(
    conn,
    contact_id: int,
    config: AppConfig,
    sender_email: str = "",
    campaign_id: int | None = None,
    now: datetime | None = None,
    enforce_time_window: bool = True,
    enforce_daily_cap: bool = True,
    service=None,
    user_id: str = "default_user",
    sender: sqlite3.Row | None = None,
) -> tuple[bool, str]:
    if sender is None:
        sender = db.get_campaign_sender(conn, campaign_id) if campaign_id else db.get_default_sender(conn)
    if sender is None and not sender_email:
        return False, "No Gmail sender selected"
    final_sender_email = str(sender["email"]) if sender else sender_email
    sender_token_path = str(sender["token_path"]) if sender else None
    sender_id = int(sender["id"]) if sender else None
    if service is None:
        gmail_status = gmail_connection_status(token_path=sender_token_path)
        if not gmail_status.connected:
            return False, f"Gmail is not connected: {gmail_status.status}"
    contact = db.fetch_contact(conn, contact_id)
    campaign = db.get_campaign(conn, campaign_id) if campaign_id else db.get_default_campaign(conn)
    if campaign is None:
        return False, "Campaign not found"
    safety = pre_send_checks(
        conn,
        contact,
        campaign,
        config,
        user_id,
        now=now,
        enforce_time_window=enforce_time_window,
        enforce_daily_cap=enforce_daily_cap,
        sender_id=sender_id,
        sender_daily_cap=int(sender["daily_cap"]) if sender else None,
    )
    if not safety.allowed:
        if campaign_id and safety.reason in {"Selected sender daily cap reached", "Daily cap reached"}:
            db.set_campaign_status(conn, "paused", campaign_id)
        return False, safety.reason

    subject = str(contact["last_preview_subject"])
    body = str(contact["last_preview_body"])
    attachment = attachment_path_for_send(config, campaign)
    log_id = db.create_send_attempt(
        conn,
        int(contact["id"]),
        int(campaign["id"]),
        str(contact["email"]),
        subject,
        body,
        attachment_name(config, campaign),
        sender_id=sender_id,
        sender_email=final_sender_email,
    )

    try:
        result = send_email(
            sender=final_sender_email,
            recipient=str(contact["email"]),
            subject=subject,
            body=body,
            attachment_path=attachment,
            token_path=sender_token_path,
            service=service,
        )
        sent_at = db.utcnow_iso()
        db.update_send_log(
            conn,
            log_id,
            "sent",
            gmail_message_id=result.message_id,
            gmail_thread_id=result.thread_id,
            sent_at=sent_at,
        )
        db.set_contact_status(conn, int(contact["id"]), ContactStatus.SENT.value)
        return True, f"Sent to {contact['email']}"
    except Exception as exc:
        error = str(exc)
        db.update_send_log(conn, log_id, "failed", error_message=error)
        db.set_contact_status(conn, int(contact["id"]), ContactStatus.FAILED.value)
        pause = should_pause_campaign(conn, config, user_id, last_error=error)
        if pause.allowed:
            db.set_campaign_status(conn, "paused", int(campaign["id"]))
            return False, f"{error}. Campaign paused: {pause.reason}"
        return False, error


def send_next_approved(
    conn,
    config: AppConfig,
    sender_email: str = "",
    campaign_id: int | None = None,
    now: datetime | None = None,
    service=None,
    user_id: str = "default_user",
) -> tuple[bool, str]:
    contact = next_approved_contact(conn, campaign_id=campaign_id)
    if not contact:
        return False, "No approved contacts are ready to send"
    return send_contact(
        conn,
        int(contact["id"]),
        config,
        sender_email=sender_email,
        campaign_id=campaign_id,
        now=now,
        service=service,
        user_id=user_id,
    )


def send_test_email(
    conn,
    contact_id: int,
    to_email: str,
    config: AppConfig,
    sender_email: str = "",
    campaign_id: int | None = None,
    service=None,
    user_id: str = "default_user",
) -> tuple[bool, str]:
    from .preview import generate_preview

    sender = db.get_campaign_sender(conn, campaign_id) if campaign_id else db.get_default_sender(conn)
    if sender is None and not sender_email:
        return False, "No Gmail sender selected"
    final_sender_email = str(sender["email"]) if sender else sender_email
    sender_token_path = str(sender["token_path"]) if sender else None
    sender_id = int(sender["id"]) if sender else None
    if service is None:
        gmail_status = gmail_connection_status(token_path=sender_token_path)
        if not gmail_status.connected:
            return False, f"Gmail is not connected: {gmail_status.status}"
    if not to_email:
        return False, "Test recipient email is required"
    campaign = db.get_campaign(conn, campaign_id) if campaign_id else db.get_default_campaign(conn)
    if campaign is None:
        return False, "Campaign not found"
    attachment = pre_send_attachment_only(config, campaign)
    if not attachment[0]:
        return attachment
    rendered = generate_preview(conn, contact_id, user_id, campaign_id=campaign_id, mark=False)
    attachment_path = attachment_path_for_send(config, campaign)
    log_id = db.create_send_attempt(
        conn,
        None,
        int(campaign["id"]),
        to_email,
        rendered.subject,
        rendered.body,
        attachment_name(config, campaign),
        status="test_attempting",
        sender_id=sender_id,
        sender_email=final_sender_email,
    )
    try:
        result = send_email(
            sender=final_sender_email,
            recipient=to_email,
            subject=rendered.subject,
            body=rendered.body,
            attachment_path=attachment_path,
            token_path=sender_token_path,
            service=service,
        )
        db.update_send_log(
            conn,
            log_id,
            "test_sent",
            gmail_message_id=result.message_id,
            gmail_thread_id=result.thread_id,
            sent_at=db.utcnow_iso(),
        )
        return True, f"Test email sent to {to_email}"
    except Exception as exc:
        db.update_send_log(conn, log_id, "test_failed", error_message=str(exc))
        return False, str(exc)


def pre_send_attachment_only(config: AppConfig, campaign) -> tuple[bool, str]:
    from .safety import attachment_check

    check = attachment_check(config, campaign)
    if not check.allowed:
        return False, check.reason
    return True, ""


def bulk_send_approved(
    conn,
    campaign_id: int,
    config: AppConfig,
    delay_minutes: int,
    service=None,
    user_id: str = "default_user",
) -> tuple[int, str]:
    senders = db.list_senders(conn, user_id)
    senders = [s for s in senders if s["status"] == "connected"]
    if not senders:
        return 0, "No connected senders available"
    campaign = db.get_campaign(conn, campaign_id)
    if not campaign:
        return 0, "Campaign not found"

    sent = 0
    failed = 0
    sender_index = 0
    while True:
        contact = next_approved_contact(conn, campaign_id=campaign_id)
        if not contact:
            break
        current_sender = senders[sender_index % len(senders)]
        sender_index += 1
        ok, msg = send_contact(
            conn, int(contact["id"]), config,
            campaign_id=campaign_id,
            enforce_time_window=False,
            enforce_daily_cap=False,
            service=None,
            user_id=user_id,
            sender=current_sender,
        )
        if ok:
            sent += 1
        else:
            failed += 1
            if "Campaign paused" in msg or "Too many" in msg:
                break
        if delay_minutes > 0:
            time.sleep(delay_minutes * 60)

    db.set_campaign_status(conn, "ended", campaign_id)
    return sent, f"Bulk send complete: {sent} sent, {failed} failed"


def start_autopilot(conn) -> None:
    db.set_campaign_status(conn, "active")


def pause_autopilot(conn) -> None:
    db.set_campaign_status(conn, "paused")


def resume_autopilot(conn) -> None:
    db.set_campaign_status(conn, "active")


def stop_autopilot(conn) -> None:
    db.set_campaign_status(conn, "ended")


def autopilot_tick(db_path: str | Path, config_path: str | Path, user_id: str = "default_user") -> tuple[bool, str]:
    config = load_config(config_path)
    conn = db.init_db(db_path)
    try:
        campaign = conn.execute(
            "SELECT * FROM campaigns WHERE status = 'active' ORDER BY updated_at LIMIT 1"
        ).fetchone()
        if campaign is None:
            return False, "No active campaign"
        if not is_allowed_send_time(config):
            return False, "Outside allowed sending window"
        sent, message = send_next_approved(
            conn,
            config,
            campaign_id=int(campaign["id"]),
            user_id=user_id,
        )
        return sent, message
    finally:
        conn.close()


def start_background_autopilot(db_path: str | Path, config_path: str | Path) -> BackgroundScheduler:
    global _BACKGROUND_SCHEDULER
    if _BACKGROUND_SCHEDULER and _BACKGROUND_SCHEDULER.running:
        return _BACKGROUND_SCHEDULER

    scheduler = BackgroundScheduler(timezone=load_config(config_path).timezone)
    scheduler.add_job(
        autopilot_tick,
        "interval",
        seconds=60,
        args=[str(db_path), str(config_path)],
        id="autopilot_tick",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    _BACKGROUND_SCHEDULER = scheduler
    return scheduler


def run_autopilot_loop(
    db_path: str | Path,
    config_path: str | Path,
    poll_seconds: int = 30,
) -> None:
    conn = db.init_db(db_path)
    db.set_campaign_status(conn, "active")
    conn.close()
    while True:
        config = load_config(config_path)
        conn = db.init_db(db_path)
        status = db.get_campaign_status(conn)
        conn.close()
        if status in {"ended", "stopped"}:
            break
        if status == "paused":
            time.sleep(poll_seconds)
            continue
        sent, message = autopilot_tick(db_path, config_path)
        if sent:
            time.sleep(config.sending.delay_minutes * 60)
        else:
            if "Daily cap reached" in message:
                time.sleep(15 * 60)
            else:
                time.sleep(poll_seconds)
