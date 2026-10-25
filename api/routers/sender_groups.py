from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api.auth import get_current_user_id
from src.platform.db import get_session
from src.platform.models import Campaign, CampaignRecipient, Sender, SenderGroup, SendJob
from src.platform.oauth import create_sender_oauth_start
from src.platform.services import ensure_user, group_senders, mark_sender_removed, require_group, serialize_group, serialize_sender


router = APIRouter(prefix="/api/sender-groups", tags=["sender-groups"])
senders_router = APIRouter(prefix="/api/senders", tags=["senders"])


class SenderGroupCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)


class SenderGroupPatch(BaseModel):
    name: str = Field(min_length=1, max_length=160)


class SenderPatch(BaseModel):
    display_name: str | None = Field(default=None, max_length=200)
    daily_cap: int | None = Field(default=None, ge=1, le=500)
    group_id: int | None = None


class GroupSendersAdd(BaseModel):
    sender_ids: list[int] = Field(min_length=1, max_length=100)


class SenderOAuthStart(BaseModel):
    return_path: str = Field(default="/senders", max_length=2048)


def _owned_sender(session: Session, user_id: str, sender_id: int) -> Sender:
    sender = session.scalar(select(Sender).where(Sender.id == sender_id, Sender.user_id == user_id))
    if not sender or sender.status == "removed":
        raise HTTPException(status_code=404, detail="Saved account not found")
    return sender


def _detach_sender(session: Session, group: SenderGroup, sender: Sender) -> None:
    # Cancel only unstarted work for campaigns using this group. The same
    # account's jobs in other groups and its OAuth credentials are untouched.
    jobs = list(session.scalars(select(SendJob).join(Campaign, Campaign.id == SendJob.campaign_id).where(
        Campaign.user_id == group.user_id, Campaign.selected_sender_group_id == group.id,
        SendJob.user_id == group.user_id, SendJob.sender_id == sender.id,
        SendJob.status.in_(("queued", "retry")),
    )))
    for job in jobs:
        recipient = session.get(CampaignRecipient, (job.campaign_id, job.recipient_id))
        if recipient and recipient.status == "queued":
            recipient.status = "approved"
        session.delete(job)
    if sender in group.members:
        group.members.remove(sender)
    if sender.group_id == group.id:
        sender.group = None
        if sender in group.senders:
            group.senders.remove(sender)
    session.flush()


@router.get("")
def list_sender_groups(
    session: Session = Depends(get_session),
    user_id: str = Depends(get_current_user_id),
):
    ensure_user(session, user_id)
    groups = list(
        session.scalars(
            select(SenderGroup)
            .options(selectinload(SenderGroup.senders), selectinload(SenderGroup.members))
            .where(SenderGroup.user_id == user_id)
            .order_by(SenderGroup.created_at, SenderGroup.id)
        )
    )
    return [serialize_group(session, group) for group in groups]


@senders_router.get("")
def list_saved_senders(session: Session = Depends(get_session), user_id: str = Depends(get_current_user_id)):
    senders = session.scalars(select(Sender).options(selectinload(Sender.groups)).where(
        Sender.user_id == user_id, Sender.status != "removed",
    ).order_by(Sender.email, Sender.id))
    return [serialize_sender(session, sender) for sender in senders]


@router.post("")
def create_sender_group(
    req: SenderGroupCreate,
    session: Session = Depends(get_session),
    user_id: str = Depends(get_current_user_id),
):
    ensure_user(session, user_id)
    if not req.name.strip():
        raise HTTPException(status_code=422, detail="Enter a group name")
    group = SenderGroup(user_id=user_id, name=req.name.strip())
    session.add(group)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(status_code=400, detail="Sender group already exists")
    return serialize_group(session, group)


@router.patch("/{group_id}")
def patch_sender_group(
    group_id: int,
    req: SenderGroupPatch,
    session: Session = Depends(get_session),
    user_id: str = Depends(get_current_user_id),
):
    try:
        group = require_group(session, user_id, group_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="Sender group not found")
    if not req.name.strip():
        raise HTTPException(status_code=422, detail="Enter a group name")
    group.name = req.name.strip()
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise HTTPException(status_code=400, detail="Sender group already exists")
    return serialize_group(session, group)


@senders_router.patch("/{sender_id}/default")
def set_sender_default(
    sender_id: int,
    session: Session = Depends(get_session),
    user_id: str = Depends(get_current_user_id),
):
    sender = session.scalar(select(Sender).where(Sender.id == sender_id, Sender.user_id == user_id))
    if not sender or sender.status == "removed":
        raise HTTPException(status_code=404, detail="Sender not found")
    for s in session.scalars(select(Sender).where(Sender.user_id == user_id)):
        s.is_default = 0
    sender.is_default = 1
    session.commit()
    return serialize_sender(session, sender)


@router.delete("/{group_id}")
def delete_sender_group(
    group_id: int,
    session: Session = Depends(get_session),
    user_id: str = Depends(get_current_user_id),
):
    try:
        group = require_group(session, user_id, group_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="Sender group not found")
    for sender in group_senders(group):
        _detach_sender(session, group, sender)
    for campaign in session.scalars(select(Campaign).where(Campaign.user_id == user_id, Campaign.selected_sender_group_id == group.id)):
        campaign.selected_sender_group_id = None
        if campaign.status in {"sending", "scheduled", "autopilot"}:
            campaign.status = "paused"
            campaign.send_settings = {**(campaign.send_settings or {}), "pause_reason": "sender_group_removed"}
    session.delete(group)
    session.commit()
    return {"status": "success"}


@router.get("/{group_id}/senders")
def list_group_senders(
    group_id: int,
    session: Session = Depends(get_session),
    user_id: str = Depends(get_current_user_id),
):
    try:
        group = require_group(session, user_id, group_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="Sender group not found")
    return [serialize_sender(session, sender) for sender in group_senders(group) if sender.status != "removed"]


@router.post("/{group_id}/senders")
def add_saved_senders(group_id: int, req: GroupSendersAdd, session: Session = Depends(get_session), user_id: str = Depends(get_current_user_id)):
    try:
        group = require_group(session, user_id, group_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="Sender group not found")
    # Resolve the whole batch before changing any memberships.
    senders = [_owned_sender(session, user_id, sender_id) for sender_id in dict.fromkeys(req.sender_ids)]
    for sender in senders:
        if sender not in group.members:
            group.members.append(sender)
    session.commit()
    return serialize_group(session, group)


@router.delete("/{group_id}/senders/{sender_id}")
def remove_group_sender(group_id: int, sender_id: int, session: Session = Depends(get_session), user_id: str = Depends(get_current_user_id)):
    try:
        group = require_group(session, user_id, group_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="Sender group not found")
    sender = _owned_sender(session, user_id, sender_id)
    if sender not in group_senders(group):
        raise HTTPException(status_code=404, detail="Account is not in this group")
    _detach_sender(session, group, sender)
    session.commit()
    session.expire(group, ["senders", "members"])
    return serialize_group(session, group)


@router.post("/{group_id}/senders/oauth/start")
def start_group_sender_oauth(
    group_id: int,
    req: SenderOAuthStart | None = None,
    session: Session = Depends(get_session),
    user_id: str = Depends(get_current_user_id),
):
    try:
        auth_url = create_sender_oauth_start(session, user_id=user_id, group_id=group_id, return_path=req.return_path if req else None)
        session.commit()
    except FileNotFoundError as exc:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(exc))
    except LookupError:
        session.rollback()
        raise HTTPException(status_code=404, detail="Sender group not found")
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=422, detail=str(exc))
    return {"auth_url": auth_url}


@senders_router.patch("/{sender_id}")
def patch_sender(
    sender_id: int,
    req: SenderPatch,
    session: Session = Depends(get_session),
    user_id: str = Depends(get_current_user_id),
):
    sender = session.scalar(select(Sender).where(Sender.id == sender_id, Sender.user_id == user_id))
    if not sender or sender.status == "removed":
        raise HTTPException(status_code=404, detail="Sender not found")
    if req.group_id is not None:
        try:
            group = require_group(session, user_id, req.group_id)
        except LookupError:
            raise HTTPException(status_code=404, detail="Sender group not found")
        if sender not in group.members:
            group.members.append(sender)
    if req.display_name is not None:
        sender.display_name = req.display_name
    if req.daily_cap is not None:
        sender.daily_cap = req.daily_cap
    session.commit()
    return serialize_sender(session, sender)


@senders_router.delete("/{sender_id}")
def delete_sender(
    sender_id: int,
    session: Session = Depends(get_session),
    user_id: str = Depends(get_current_user_id),
):
    sender = session.scalar(select(Sender).where(Sender.id == sender_id, Sender.user_id == user_id))
    if not sender or sender.status == "removed":
        raise HTTPException(status_code=404, detail="Sender not found")
    mark_sender_removed(sender)
    session.commit()
    return {"status": "success"}
