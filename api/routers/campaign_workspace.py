"""Campaign workspace actions that never enqueue delivery work."""

from copy import deepcopy

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from api.auth import get_current_user_id
from src.platform.db import get_session
from src.platform.models import AutopilotDaySchedule, Campaign, CampaignAttachment, CampaignRecipient
from src.platform.campaign_capacity import daily_campaign_capacity

router = APIRouter(tags=["campaign-workspace"])


@router.get("/api/campaigns/daily-capacity")
def get_daily_capacity(session: Session = Depends(get_session), user_id: str = Depends(get_current_user_id)):
    return daily_campaign_capacity(session, user_id)


@router.post("/api/campaigns/{campaign_id}/duplicate")
def duplicate_campaign(
    campaign_id: int,
    session: Session = Depends(get_session),
    user_id: str = Depends(get_current_user_id),
):
    source = session.scalar(select(Campaign).where(Campaign.id == campaign_id, Campaign.user_id == user_id))
    if source is None:
        raise HTTPException(status_code=404, detail="Campaign not found")

    # Copy configuration only. Runtime counters, validation snapshots, and old
    # launch timestamps must never carry over into a new draft.
    settings = {
        key: deepcopy(value)
        for key, value in (source.send_settings or {}).items()
        if key in {"mode", "delay_minutes", "pacing_mode", "dry_run"}
    }
    duplicate = Campaign(
        user_id=user_id,
        name=f"{source.name[:233]} (copy)",
        status="draft",
        subject_template=source.subject_template,
        body_template=source.body_template,
        fallback_body_template=source.fallback_body_template,
        attachment_path=source.attachment_path,
        engagement_tracking_enabled=source.engagement_tracking_enabled,
        selected_sender_group_id=source.selected_sender_group_id,
        timezone=source.timezone,
        send_settings=settings,
    )
    session.add(duplicate)
    session.flush()

    for recipient in session.scalars(select(CampaignRecipient).where(CampaignRecipient.campaign_id == source.id)):
        session.add(CampaignRecipient(
            campaign_id=duplicate.id,
            contact_id=recipient.contact_id,
            status="rejected" if recipient.status == "rejected" else "approved",
        ))
    for day in session.scalars(select(AutopilotDaySchedule).where(AutopilotDaySchedule.campaign_id == source.id)):
        session.add(AutopilotDaySchedule(
            campaign_id=duplicate.id, day_of_week=day.day_of_week,
            daily_cap=day.daily_cap, start_time=day.start_time, end_time=day.end_time,
        ))
    copied_attachments = []
    for attachment in session.scalars(select(CampaignAttachment).where(CampaignAttachment.campaign_id == source.id)):
        copied = CampaignAttachment(
            campaign_id=duplicate.id, filename=attachment.filename,
            content_type=attachment.content_type, size_bytes=attachment.size_bytes,
            sha256=attachment.sha256, content=attachment.content,
        )
        session.add(copied)
        copied_attachments.append(copied)
    session.flush()
    if copied_attachments:
        duplicate.attachment_path = ""
        duplicate.attachment_metadata = {
            "storage": "database", "count": len(copied_attachments),
            "attachments": [{"id": item.id, "filename": item.filename, "content_type": item.content_type,
                             "size_bytes": item.size_bytes, "sha256": item.sha256} for item in copied_attachments],
        }

    # These legacy preferences live in the shared database; copying them in
    # this same transaction prevents a partially configured duplicate.
    for preference in ("require_attachment", "tracking_enabled", "unsubscribe_link"):
        session.execute(text(
            "INSERT INTO settings(key, user_id, value) "
            "SELECT :new_key, user_id, value FROM settings WHERE key = :old_key AND user_id = :user_id"
        ), {"new_key": f"campaign_{duplicate.id}_{preference}",
            "old_key": f"campaign_{source.id}_{preference}", "user_id": user_id})
    session.commit()
    return {"id": duplicate.id, "name": duplicate.name, "status": "draft"}
