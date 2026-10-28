from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.platform.jobs import create_send_jobs_for_next_batch
from src.platform.models import Campaign, SendJob
from src.platform.services import WEEKDAY_NAMES, next_autopilot_run
from src.platform.time import utcnow


def enqueue_due_campaign_batches(session: Session, *, limit: int = 25) -> list[dict]:
    now = utcnow()
    from .shared_dispatch import enabled, sync_campaign
    query = select(Campaign).where(
        Campaign.status.in_(("sending", "scheduled", "autopilot")),
        Campaign.scheduled_at.is_not(None), Campaign.scheduled_at <= now,
    ).order_by(Campaign.scheduled_at, Campaign.id).limit(limit)
    if enabled(session, ""):
        query = query.where(Campaign.shared_dispatch.is_(False))
    campaigns = list(
        session.scalars(query)
    )
    results: list[dict] = []
    for campaign in campaigns:
        # Serialize schedulers for the same campaign so concurrent ticks
        # cannot both create work beyond the campaign's daily cap.
        campaign = session.scalar(
            select(Campaign).where(Campaign.id == campaign.id).with_for_update()
        )
        if not campaign:
            continue
        if enabled(session, campaign.user_id):
            sync_campaign(session, campaign, now=now)
            results.append({"campaign_id": campaign.id, "created": 0, "reason": "Coordinated by sender account"})
            continue
        active_jobs = list(
            session.scalars(
                select(SendJob).where(
                    SendJob.campaign_id == campaign.id,
                    SendJob.status.in_(("queued", "running", "retry")),
                )
            )
        )
        if active_jobs:
            campaign.scheduled_at = now + timedelta(seconds=30)
            results.append({"campaign_id": campaign.id, "created": 0, "reason": "Current batch is still active"})
            continue

        delay_minutes = int((campaign.send_settings or {}).get("delay_minutes", 5))
        result = create_send_jobs_for_next_batch(
            session,
            user_id=campaign.user_id,
            campaign_id=campaign.id,
            delay_minutes=delay_minutes,
            scheduled_for=now,
        )
        if result.get("job_ids"):
            campaign.send_settings = {
                **(campaign.send_settings or {}),
                "pause_reason": None,
            }
        if result.get("exhausted"):
            campaign.status = "ended"
            campaign.scheduled_at = None
        elif result.get("reason_code") in ("daily_caps_reached", "campaign_daily_cap_reached"):
            if (campaign.send_settings or {}).get("mode") == "autopilot" or campaign.status == "autopilot":
                campaign.status = "autopilot"
                campaign.scheduled_at = next_autopilot_run(session, campaign, now=now, force_next_day=True)
                campaign.send_settings = {
                    **(campaign.send_settings or {}),
                    "pause_reason": "campaign_daily_cap_reached"
                    if result.get("reason_code") == "campaign_daily_cap_reached"
                    else "daily_caps_reached",
                }
            else:
                campaign.status = "paused"
                campaign.scheduled_at = None
                campaign.send_settings = {
                    **(campaign.send_settings or {}),
                    "pause_reason": "daily_caps_reached",
                }
        elif result.get("reason_code") in ("autopilot_day_disabled", "autopilot_outside_window"):
            campaign.status = "autopilot"
            campaign.scheduled_at = next_autopilot_run(session, campaign, now=now)
            campaign.send_settings = {
                **(campaign.send_settings or {}),
                "pause_reason": result["reason_code"],
            }
        elif not result.get("job_ids"):
            if (campaign.send_settings or {}).get("mode") == "autopilot":
                campaign.status = "autopilot"
                campaign.scheduled_at = now + timedelta(minutes=15)
            else:
                campaign.status = "paused"
                campaign.scheduled_at = None
            campaign.send_settings = {
                **(campaign.send_settings or {}),
                "pause_reason": result.get("reason_code", "no_eligible_work"),
            }
        results.append({"campaign_id": campaign.id, **result})
    session.commit()
    for result in results:
        result["queued"] = len(result.get("job_ids", []))
    return results
