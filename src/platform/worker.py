from __future__ import annotations

import logging
import threading
from datetime import timedelta

from sqlalchemy import select, update

from src.platform.gmail_activity import process_pending_gmail_notifications, renew_due_gmail_watches
from src.platform.db import SessionLocal
from src.platform.delivery_safety import delivery_block_reason, delivery_enabled
from src.platform.jobs import perform_send_job
from src.platform.migrations import upgrade_database
from src.platform.models import Campaign, SendJob
from src.platform.scheduler import enqueue_due_campaign_batches
from src.platform.time import utcnow
from src.platform import shared_dispatch


logger = logging.getLogger("outreach.worker")


def recover_stale_jobs(*, stale_after_minutes: int = 10) -> int:
    session = SessionLocal()
    try:
        result = session.execute(
            update(SendJob)
            .where(
                SendJob.status == "running",
                SendJob.locked_at < utcnow() - timedelta(minutes=stale_after_minutes),
            )
            .values(status="retry", locked_at=None, error_message="Recovered after worker interruption")
        )
        session.commit()
        return int(result.rowcount or 0)
    finally:
        session.close()


def claim_next_due_job() -> int | None:
    session = SessionLocal()
    try:
        shared_job = shared_dispatch.claim_job(session, now=utcnow())
        if shared_job is not None:
            return shared_job
        legacy_query = (
            select(SendJob)
            .join(Campaign, Campaign.id == SendJob.campaign_id)
            .where(
                SendJob.status.in_(("queued", "retry")),
                SendJob.scheduled_for <= utcnow(),
                Campaign.status.in_(("sending", "scheduled", "autopilot")),
            )
            .order_by(SendJob.scheduled_for, SendJob.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if shared_dispatch.enabled(session, ""):
            legacy_query = legacy_query.where(Campaign.shared_dispatch.is_(False))
        job = session.scalar(legacy_query)
        if not job:
            return None
        campaign = session.get(Campaign, job.campaign_id)
        if shared_dispatch.enabled(session, campaign.user_id):
            shared_dispatch.sync_campaign(session, campaign, now=utcnow())
            session.commit()
            return None
        job.status = "running"
        job.locked_at = utcnow()
        session.commit()
        return job.id
    finally:
        session.close()


def run_worker_cycle(*, max_jobs: int = 25) -> int:
    session = SessionLocal()
    try:
        shared_dispatch.maintain(session, now=utcnow())
        enqueue_due_campaign_batches(session)
    except Exception:
        session.rollback()
        logger.exception("Campaign scheduling cycle failed")
    finally:
        session.close()

    gmail_session = SessionLocal()
    try:
        # These are indexed, bounded queue lookups. Gmail mailboxes are only
        # read when a push notification has marked that specific sender due.
        renew_due_gmail_watches(gmail_session)
        process_pending_gmail_notifications(gmail_session)
    except Exception:
        gmail_session.rollback()
        logger.exception("Gmail event-processing cycle failed")
    finally:
        gmail_session.close()

    processed = 0
    while processed < max_jobs and (job_id := claim_next_due_job()):
        perform_send_job(job_id, claimed=True)
        processed += 1
    return processed


def run_worker_loop(
    *,
    poll_seconds: int = 5,
    stale_after_minutes: int = 10,
    stop_event: threading.Event | None = None,
) -> None:
    logging.basicConfig(level=logging.INFO)
    if not delivery_enabled():
        logger.warning("Worker did not start: %s", delivery_block_reason())
        return
    upgrade_database()
    stop = stop_event or threading.Event()
    while not stop.is_set():
        try:
            recovered = recover_stale_jobs(stale_after_minutes=stale_after_minutes)
            if recovered:
                logger.warning("Recovered %s interrupted send jobs", recovered)
            run_worker_cycle()
        except Exception:
            logger.exception("Worker cycle failed")
        stop.wait(poll_seconds)


if __name__ == "__main__":
    run_worker_loop()
