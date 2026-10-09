from __future__ import annotations

import logging
import json
import secrets
from collections import OrderedDict
from datetime import datetime, timedelta
from threading import Lock

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.gmail_sender import EmailAttachment, fake_send_email, send_email
from src.platform.db import SessionLocal
from src.platform.email_tracking import prepare_tracked_email
from src.platform.gmail import gmail_service_for_sender
from src.platform.models import Campaign, CampaignAttachment, CampaignRecipient, Contact, Sender, SendJob, SendLog
from src.platform.services import (
    SENDER_ERROR_COOLDOWN,
    WEEKDAY_NAMES,
    autopilot_window_state,
    campaign_batch_schedule,
    campaign_daily_capacity,
    connected_senders,
    delivery_policy_state,
    eligible_senders,
    require_group,
    sender_sent_count_today,
)
from src.platform.time import utcnow
from src.template_engine import MissingTemplateVariablesError, missing_template_variables, render_template


ATTACHMENT_CACHE_LIMIT = 32
_attachment_cache: OrderedDict[tuple[int, str], EmailAttachment] = OrderedDict()
_attachment_cache_lock = Lock()


def _exception_detail(exc: BaseException) -> str:
    detail = str(exc).strip()
    return detail or f"{type(exc).__name__}: {exc!r}"


def _render(template: str, context: dict) -> str:
    return render_template(template or "", context, strict=True)


def _campaign_email_attachments(session: Session, campaign: Campaign) -> tuple[EmailAttachment, ...]:
    stored_attachments = session.scalars(
        select(CampaignAttachment)
        .where(CampaignAttachment.campaign_id == campaign.id)
        .order_by(CampaignAttachment.id)
    ).all()
    attachments: list[EmailAttachment] = []
    for stored in stored_attachments:
        cache_key = (stored.id, stored.sha256)
        with _attachment_cache_lock:
            cached = _attachment_cache.get(cache_key)
            if cached:
                _attachment_cache.move_to_end(cache_key)
                attachments.append(cached)
                continue

        attachment = EmailAttachment(
            filename=stored.filename,
            content_type=stored.content_type,
            content=stored.content,
        )
        attachments.append(attachment)
        with _attachment_cache_lock:
            _attachment_cache[cache_key] = attachment
            _attachment_cache.move_to_end(cache_key)
            while len(_attachment_cache) > ATTACHMENT_CACHE_LIMIT:
                _attachment_cache.popitem(last=False)
    return tuple(attachments)


def has_unsent_approved_recipients(
    session: Session,
    campaign: Campaign,
    *,
    user_id: str | None = None,
    include_failed: bool = False,
) -> bool:
    """Report whether a batch could still queue new work for this campaign.

    This mirrors the eligibility rule in :func:`create_send_jobs_for_next_batch`,
    so callers can tell "nothing left to send" apart from "temporarily blocked".
    Set ``include_failed`` only for starting paths that reopen failed
    recipients first.
    """
    blocked_statuses = ("queued", "running", "retry", "sent")
    if include_failed:
        blocked_statuses = (*blocked_statuses, "failed")
    existing_job = (
        select(SendJob.id)
        .where(
            SendJob.campaign_id == campaign.id,
            SendJob.recipient_id == CampaignRecipient.contact_id,
            SendJob.status.in_(blocked_statuses),
        )
        .exists()
    )
    return session.scalar(
        select(CampaignRecipient.contact_id)
        .join(Contact, Contact.id == CampaignRecipient.contact_id)
        .where(
            CampaignRecipient.campaign_id == campaign.id,
            CampaignRecipient.status == "approved",
            Contact.user_id == (user_id or campaign.user_id),
            Contact.status.in_(("approved", "sent")),
            ~existing_job,
        )
        .limit(1)
    ) is not None


def _has_unsent_approved_recipients(session: Session, campaign: Campaign) -> bool:
    """Report whether the campaign could still queue work, including retries.

    A failed recipient keeps its job and can still be reopened, so it counts as
    work that is not finished yet.
    """
    return has_unsent_approved_recipients(session, campaign, include_failed=True)


def _schedule_after_finished_batch(session: Session, job: SendJob, finished_at: datetime) -> None:
    session.flush()
    active_jobs = session.scalar(
        select(func.count())
        .select_from(SendJob)
        .where(
            SendJob.campaign_id == job.campaign_id,
            SendJob.status.in_(("queued", "running", "retry")),
        )
    ) or 0
    if active_jobs:
        return
    campaign = session.get(Campaign, job.campaign_id)
    if not campaign or campaign.status in {"paused", "stopped", "ended"}:
        return
    if not _has_unsent_approved_recipients(session, campaign):
        campaign.status = "ended"
        campaign.scheduled_at = None
        campaign.send_settings = {
            **(campaign.send_settings or {}),
            "pause_reason": None,
        }
        return
    batch_size = int(
        session.scalar(
            select(func.count())
            .select_from(SendJob)
            .where(
                SendJob.campaign_id == job.campaign_id,
                SendJob.batch_id == job.batch_id,
            )
        )
        or 1
    )
    state = campaign_batch_schedule(
        session,
        campaign,
        after=finished_at,
        delay_minutes=int((campaign.send_settings or {}).get("delay_minutes", 5)),
        batch_size=batch_size,
    )
    campaign.scheduled_at = state["next_at"]
    campaign.send_settings = {
        **(campaign.send_settings or {}),
        "pause_reason": state["pause_reason"],
    }


def _defer_disallowed_job(
    session: Session,
    *,
    job: SendJob,
    campaign: Campaign,
    recipient: CampaignRecipient,
    policy: dict,
    now: datetime,
) -> dict:
    reason_code = str(policy["reason_code"])
    next_at = policy.get("next_at")
    job.locked_at = None

    if reason_code in {"sender_daily_cap_reached", "campaign_daily_cap_reached"}:
        recipient.status = "approved"
        session.delete(job)
        if reason_code == "campaign_daily_cap_reached":
            campaign.scheduled_at = next_at
        else:
            campaign.scheduled_at = now
    else:
        job.status = "retry"
        job.scheduled_for = next_at or now + SENDER_ERROR_COOLDOWN
        campaign.scheduled_at = job.scheduled_for

    campaign.send_settings = {
        **(campaign.send_settings or {}),
        "pause_reason": reason_code,
    }
    session.commit()
    return {
        "status": "deferred",
        "job_id": job.id if reason_code not in {"sender_daily_cap_reached", "campaign_daily_cap_reached"} else None,
        "reason_code": reason_code,
        "reason": policy["reason"],
        "scheduled_for": next_at.isoformat() if next_at else None,
    }


def create_send_jobs_for_next_batch(
    session: Session,
    *,
    user_id: str,
    campaign_id: int,
    delay_minutes: int = 5,
    scheduled_for: datetime | None = None,
) -> dict:
    campaign = session.scalar(
        select(Campaign)
        .where(Campaign.id == campaign_id, Campaign.user_id == user_id)
        .with_for_update()
    )
    if not campaign:
        raise LookupError("Campaign not found")
    if campaign.status in {"paused", "stopped", "ended"}:
        return {"created": 0, "queued": 0, "reason": f"Campaign is {campaign.status}"}
    if not campaign.selected_sender_group_id:
        raise ValueError("Campaign does not have a sender group selected")

    policy_now = utcnow()
    group = require_group(session, user_id, campaign.selected_sender_group_id)
    connected = connected_senders(group)
    senders = eligible_senders(session, group, lock=True, now=policy_now)
    if not senders:
        if not connected:
            reason_code = "no_connected_senders"
            reason = "No eligible connected senders in selected group"
        elif all(
            sender_sent_count_today(session, sender.id, now=policy_now) >= sender.daily_cap
            for sender in connected
        ):
            reason_code = "daily_caps_reached"
            reason = "All senders in the selected group reached their daily cap"
        else:
            reason_code = "senders_temporarily_unavailable"
            reason = "Connected senders are temporarily unavailable"
        return {"created": 0, "queued": 0, "reason_code": reason_code, "reason": reason}

    max_to_send = len(senders)
    is_autopilot = (campaign.send_settings or {}).get("mode") == "autopilot"
    if is_autopilot:
        window = autopilot_window_state(session, campaign, now=policy_now)
        if not window["allowed"]:
            return {
                "created": 0,
                "queued": 0,
                "reason_code": window["reason_code"],
                "reason": window["reason"],
                "next_at": window["next_at"].isoformat(),
            }
        capacity = campaign_daily_capacity(
            session,
            campaign,
            now=policy_now,
            schedule=window["schedule"],
        )
        if capacity is not None:
            remaining_campaign = capacity["remaining"]
            if remaining_campaign <= 0:
                return {
                    "created": 0,
                    "queued": 0,
                    "reason_code": "campaign_daily_cap_reached",
                    "reason": "Campaign reached its daily sending limit",
                }
            max_to_send = min(max_to_send, remaining_campaign)

    session.execute(
        update(CampaignRecipient)
        .where(
            CampaignRecipient.campaign_id == campaign_id,
            or_(
                CampaignRecipient.status.is_(None),
                func.trim(CampaignRecipient.status) == "",
            ),
        )
        .values(status="approved")
    )
    existing_job = (
        select(SendJob.id)
        .where(
            SendJob.campaign_id == campaign_id,
            SendJob.recipient_id == CampaignRecipient.contact_id,
            SendJob.status.in_(("queued", "running", "retry", "sent", "failed")),
        )
        .exists()
    )
    recipients = list(
        session.scalars(
            select(CampaignRecipient)
            .join(Contact, Contact.id == CampaignRecipient.contact_id)
            .where(
                CampaignRecipient.campaign_id == campaign_id,
                Contact.user_id == user_id,
                Contact.status.in_(("approved", "sent")),
                CampaignRecipient.status == "approved",
                ~existing_job,
            )
            .order_by(CampaignRecipient.created_at, CampaignRecipient.contact_id)
            .limit(max_to_send)
        )
    )
    if not recipients:
        # Nothing queued this pass. It is only "exhausted" when no eligible
        # recipient is left at all; a blocked day or cap must not end the
        # campaign, and a later recipient import must be able to resume it.
        return {
            "created": 0,
            "queued": 0,
            "exhausted": not has_unsent_approved_recipients(
                session,
                campaign,
                user_id=user_id,
            ),
            "reason": "No unsent approved recipients are ready",
        }

    scheduled = scheduled_for or utcnow()
    batch_id = secrets.token_urlsafe(16)
    created_ids: list[int] = []
    for recipient, sender in zip(recipients, senders):
        idempotency_key = f"campaign:{campaign_id}:recipient:{recipient.contact_id}"
        try:
            with session.begin_nested():
                job = SendJob(
                    user_id=user_id,
                    campaign_id=campaign_id,
                    recipient_id=recipient.contact_id,
                    sender_id=sender.id,
                    status="queued",
                    scheduled_for=scheduled,
                    batch_id=batch_id,
                    idempotency_key=idempotency_key,
                )
                session.add(job)
                recipient.status = "queued"
                session.flush()
                job_id = job.id
        except IntegrityError:
            continue
        created_ids.append(job_id)

    if not created_ids:
        return {
            "created": 0,
            "queued": 0,
            "exhausted": False,
            "reason": "Recipients were queued by another request",
        }

    if created_ids and campaign.status in {"draft", "scheduled"}:
        campaign.status = "sending"
    next_schedule = None
    if created_ids:
        next_schedule = campaign_batch_schedule(
            session,
            campaign,
            after=scheduled,
            delay_minutes=delay_minutes,
            batch_size=len(created_ids),
        )
        campaign.scheduled_at = next_schedule["next_at"]
        campaign.send_settings = {
            **(campaign.send_settings or {}),
            "pause_reason": next_schedule["pause_reason"],
        }

    return {
        "created": len(created_ids),
        "queued": 0,
        "job_ids": created_ids,
        "batch_id": batch_id,
        "exhausted": False,
        "next_batch_due_at": next_schedule["next_at"].isoformat() if next_schedule else None,
    }


def perform_send_job(job_id: int, *, claimed: bool = False) -> dict:
    logger = logging.getLogger("outreach.send")
    logger.info("perform_send_job start job_id=%s", job_id)
    session = SessionLocal()
    attempt_log_id: int | None = None
    try:
        initial_job = session.get(SendJob, job_id)
        if not initial_job:
            logger.warning("job %s not found", job_id)
            return {"status": "missing"}
        campaign = session.scalar(
            select(Campaign).where(Campaign.id == initial_job.campaign_id).with_for_update()
        )
        job = session.scalar(select(SendJob).where(SendJob.id == job_id).with_for_update())
        if not job:
            logger.warning("job %s not found", job_id)
        else:
            logger.info("job %s status=%s campaign=%s sender=%s recipient=%s", job_id, job.status, job.campaign_id, job.sender_id, job.recipient_id)
        if not job:
            return {"status": "missing"}
        if job.status == "sent":
            return {"status": "already_sent"}
        allowed_statuses = {"queued", "retry", "running"} if claimed else {"queued", "retry"}
        if job.status not in allowed_statuses:
            return {"status": job.status}

        contact = session.get(Contact, job.recipient_id)
        sender = session.scalar(select(Sender).where(Sender.id == job.sender_id).with_for_update())
        recipient = session.get(CampaignRecipient, {"campaign_id": job.campaign_id, "contact_id": job.recipient_id})
        if not campaign or not contact or not sender or not recipient:
            msg = "Campaign, contact, sender, or campaign recipient no longer exists."
            logger.error("job %s missing entities: campaign=%s contact=%s sender=%s recipient=%s", job_id, campaign is not None, contact is not None, sender is not None, recipient is not None)
            job.status = "failed"
            job.error_message = msg
            session.commit()
            return {"status": "failed", "error": msg}
        logger.info("job %s campaign_status=%s recipient_status=%s sender_status=%s has_creds=%s", job_id, campaign.status, recipient.status, sender.status, bool(sender.encrypted_oauth_credentials))
        if campaign.status == "paused":
            logger.info("job %s paused with campaign", job_id)
            job.status = "queued"
            job.locked_at = None
            session.commit()
            return {"status": "paused"}
        if campaign.status in {"stopped", "ended"}:
            logger.info("job %s cancelled because campaign is %s", job_id, campaign.status)
            if recipient.status == "queued":
                recipient.status = "approved"
            session.delete(job)
            session.commit()
            return {"status": "cancelled", "reason_code": f"campaign_{campaign.status}"}
        if recipient.status not in {"queued", "approved"} or contact.status not in {"approved", "sent"}:
            logger.info(
                "job %s cancelled because recipient=%s contact=%s",
                job_id,
                recipient.status,
                contact.status,
            )
            if contact.status == "do_not_contact":
                recipient.status = "rejected"
            session.delete(job)
            _schedule_after_finished_batch(session, job, utcnow())
            session.commit()
            return {"status": "cancelled", "reason_code": "recipient_ineligible"}
        if sender.status != "connected" or not sender.encrypted_oauth_credentials:
            logger.error("job %s sender not connected: status=%s has_creds=%s", job_id, sender.status, bool(sender.encrypted_oauth_credentials))
            recipient.status = "approved"
            session.delete(job)
            campaign.scheduled_at = utcnow()
            campaign.send_settings = {
                **(campaign.send_settings or {}),
                "pause_reason": "sender_unavailable",
            }
            session.commit()
            return {
                "status": "deferred",
                "job_id": None,
                "reason_code": "sender_unavailable",
                "reason": "The assigned sender is no longer connected",
            }

        custom_fields = contact.custom_fields or {}
        if isinstance(custom_fields, str):
            custom_fields = json.loads(custom_fields)
        context = dict(custom_fields)
        context.setdefault("email", contact.email_normalized)
        missing_variables: list[str] = []
        for template in (campaign.subject_template, campaign.body_template or campaign.fallback_body_template):
            for variable in missing_template_variables(template, context):
                if variable not in missing_variables:
                    missing_variables.append(variable)
        if missing_variables:
            # Skip this recipient — their CSV is missing required template fields.
            # The campaign must NOT stop; the next recipient should still be sent.
            error_detail = str(MissingTemplateVariablesError(missing_variables))
            logger.warning(
                "job %s skipping recipient %s: %s",
                job_id,
                contact.email_normalized,
                error_detail,
            )
            sender.last_error = None
            sender.recent_error_at = None
            recipient.status = "skipped"
            session.add(
                SendLog(
                    user_id=job.user_id,
                    campaign_id=job.campaign_id,
                    contact_id=job.recipient_id,
                    recipient_id=job.recipient_id,
                    sender_id=job.sender_id,
                    recipient_email=contact.email_normalized,
                    sender_email=sender.email,
                    subject=campaign.subject_template,
                    body_snapshot=campaign.body_template,
                    status="skipped",
                    error_message=error_detail,
                )
            )
            session.delete(job)
            _schedule_after_finished_batch(session, job, utcnow())
            session.commit()
            return {"status": "skipped", "job_id": job_id, "reason": error_detail}

        now = utcnow()
        job.scheduled_for = now
        session.flush()
        policy = delivery_policy_state(session, campaign, sender, now=now)
        if not policy["allowed"]:
            logger.info(
                "job %s deferred by delivery policy: %s",
                job_id,
                policy["reason_code"],
            )
            return _defer_disallowed_job(
                session,
                job=job,
                campaign=campaign,
                recipient=recipient,
                policy=policy,
                now=now,
            )

        if not claimed:
            job.status = "running"
            job.locked_at = now
        job.attempts += 1
        # Persist the attempt before rendering or calling Gmail. A rollback must
        # not turn a permanent pre-send failure into an infinite retry loop.
        session.commit()
        subject = _render(campaign.subject_template, context)
        body = _render(campaign.body_template or campaign.fallback_body_template, context)
        log = SendLog(
            user_id=job.user_id,
            campaign_id=job.campaign_id,
            contact_id=job.recipient_id,
            recipient_id=job.recipient_id,
            sender_id=job.sender_id,
            recipient_email=contact.email_normalized,
            sender_email=sender.email,
            subject=subject,
            body_snapshot=body,
            status="attempting",
        )
        session.add(log)
        session.flush()
        attempt_log_id = log.id
        session.commit()

        dry_run = (campaign.send_settings or {}).get("dry_run", False)
        send_fn = fake_send_email if dry_run else send_email
        email_attachments = _campaign_email_attachments(session, campaign)
        delivery_body = body
        if campaign.engagement_tracking_enabled and not dry_run:
            delivery_body = prepare_tracked_email(session, log, body).body
            session.commit()
        result = send_fn(
            sender=sender.email,
            recipient=contact.email_normalized,
            subject=subject,
            body=delivery_body,
            attachments=email_attachments,
            service=None if dry_run else gmail_service_for_sender(session, sender),
        )
        now = utcnow()
        log.status = "test_sent" if dry_run else "sent"
        log.sent_at = now
        log.gmail_message_id = result.message_id
        log.gmail_thread_id = result.thread_id
        job.status = "sent"
        recipient.status = "sent"
        sender.status = "connected"
        sender.last_error = None
        sender.recent_error_at = None
        session.refresh(campaign, attribute_names=["status"])
        _schedule_after_finished_batch(session, job, now)
        session.commit()
        logger.info("perform_send_job success job_id=%s gmail_id=%s", job_id, result.message_id)
        return {"status": "sent", "job_id": job_id}
    except Exception as exc:
        error_detail = _exception_detail(exc)
        logger.exception(
            "perform_send_job failed job_id=%s error_type=%s error=%s",
            job_id,
            type(exc).__name__,
            error_detail,
        )
        session.rollback()
        job = session.get(SendJob, job_id)
        if job:
            final_failure = job.attempts >= job.max_attempts
            job.status = "failed" if final_failure else "retry"
            job.error_message = error_detail
            job.locked_at = None
            if not final_failure:
                backoff = timedelta(minutes=min(2 ** max(job.attempts - 1, 0), 15))
                job.scheduled_for = utcnow() + max(backoff, SENDER_ERROR_COOLDOWN)
            if attempt_log_id:
                attempt_log = session.get(SendLog, attempt_log_id)
                if attempt_log:
                    attempt_log.status = "failed"
                    attempt_log.error_message = error_detail
            sender = session.get(Sender, job.sender_id)
            if sender:
                sender.last_error = error_detail
                sender.recent_error_at = utcnow()
            if final_failure:
                recipient = session.get(
                    CampaignRecipient,
                    {"campaign_id": job.campaign_id, "contact_id": job.recipient_id},
                )
                if recipient:
                    recipient.status = "failed"
            _schedule_after_finished_batch(session, job, utcnow())
            session.commit()
        return {"status": "failed", "job_id": job_id, "error": str(exc)}
    finally:
        session.close()
