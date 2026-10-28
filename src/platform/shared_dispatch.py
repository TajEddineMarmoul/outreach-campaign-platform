"""Durable per-account clocks and indexed weighted-fair campaign queues.

Hot-path work is one account row and one indexed queue entry. Campaign windows
are materialized at configuration time and advanced by an indexed boundary
queue; an email never recomputes a sum over all campaigns.
"""
from __future__ import annotations

import logging
import math
import hashlib
import json
import secrets
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import case, event, func, inspect, select, update
from sqlalchemy.orm import Session

from .models import AutopilotDaySchedule, Campaign, CampaignRecipient, SendJob, SendLog, Sender, SenderCampaignLane, SenderDispatch, SenderGroup
from .services import WEEKDAY_NAMES, _aware_utc, campaign_zone, connected_senders, require_group
from .time import utcnow

ACTIVE = ("sending", "scheduled", "autopilot")
LEASE_TIME = timedelta(minutes=15)
EPSILON = 1e-12
log = logging.getLogger("outreach.shared_dispatch")


def _calendar_key(calendar: dict) -> str:
    # Rates and target sizes may differ while the window boundaries match.
    # Coalesce by window shape, not by campaign count or target value.
    shape = {key: value for key, value in calendar.items() if key not in {"fixed_rate", "senders", "pacing", "days"}}
    if "days" in calendar:
        shape["days"] = {day: {"start": item["start"], "end": item["end"]}
                         for day, item in calendar["days"].items()}
    return hashlib.sha256(json.dumps(shape, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _locked_state(session: Session, sender_id: int, *, skip_locked: bool = False) -> SenderDispatch | None:
    cached = session.identity_map.get(session.identity_key(SenderDispatch, sender_id))
    query = select(SenderDispatch).where(SenderDispatch.sender_id == sender_id).with_for_update(skip_locked=skip_locked)
    if cached is None or cached not in session.dirty:
        query = query.execution_options(populate_existing=True)
    return session.scalar(query)


def enabled(session: Session, user_id: str) -> bool:
    """A single rollback switch; all accounts coordinate by default."""
    import os
    return os.getenv("OUTREACH_SHARED_DISPATCH", "true").strip().lower() not in {"false", "0", "off"}


def _advance(state: SenderDispatch, at: datetime) -> None:
    at = _aware_utc(at)
    elapsed = max(0.0, (at - _aware_utc(state.clock_at)).total_seconds())
    # At most one due slot can survive an outage. Missed slots never become a burst.
    state.credit = min(1.0, state.credit + elapsed * max(state.rate, 0.0))
    state.clock_at = max(at, _aware_utc(state.clock_at))


def _next(state: SenderDispatch, at: datetime) -> None:
    if state.lease_token:
        state.next_at = state.lease_until
    elif state.active_lanes > 0 and state.rate > 0:
        debt = max(0.0, 1.0 - state.credit)
        # Round up to the database timestamp's microsecond resolution. Rounding
        # down repeatedly can put the next hour's slot just before its boundary.
        wait_microseconds = math.ceil(debt / state.rate * 1_000_000)
        state.next_at = at + timedelta(microseconds=wait_microseconds)
    else:
        state.rate = 0.0
        state.next_at = None
    if state.next_at and state.blocked_until:
        state.next_at = max(state.next_at, _aware_utc(state.blocked_until))


def _state(session: Session, sender_id: int, user_id: str, now: datetime) -> SenderDispatch:
    state = _locked_state(session, sender_id)
    if state is None:
        # The sender row serializes initial state creation with another campaign.
        session.scalar(select(Sender.id).where(Sender.id == sender_id).with_for_update())
        state = _locked_state(session, sender_id)
        if state is None:
            last = session.scalar(select(SendLog.sent_at).where(SendLog.sender_id == sender_id,
                SendLog.status.in_(("sent", "bounced"))).order_by(SendLog.sent_at.desc()).limit(1))
            state = SenderDispatch(sender_id=sender_id, user_id=user_id, rate=0.0, active_lanes=0,
                                   virtual_time=0.0, credit=1.0, clock_at=now, idle_since=now, last_started_at=last)
            session.add(state)
            session.flush()
    return state


def _window(calendar: dict, now: datetime) -> tuple[datetime, datetime, float, bool] | None:
    """Resolve the current or next window without loading a campaign or its group."""
    now = _aware_utc(now)
    zone = ZoneInfo(calendar["timezone"])
    not_before = datetime.fromisoformat(calendar["not_before"]) if calendar.get("not_before") else now
    reference = max(now, _aware_utc(not_before))
    local = reference.astimezone(zone)
    if calendar["mode"] != "autopilot":
        end = datetime.combine(local.date() + timedelta(days=1), time.min, tzinfo=zone).astimezone(timezone.utc)
        return reference, end, calendar["fixed_rate"], reference <= now
    for offset in range(9):
        date = local.date() + timedelta(days=offset)
        day = calendar["days"].get(WEEKDAY_NAMES[date.weekday()])
        if not day:
            continue
        start = datetime.combine(date, time.fromisoformat(day["start"]), tzinfo=zone).astimezone(timezone.utc)
        end = datetime.combine(date, time.fromisoformat(day["end"]), tzinfo=zone).astimezone(timezone.utc)
        if end <= reference:
            continue
        rate = (day["cap"] / (end - start).total_seconds() / calendar["senders"]
                if calendar["pacing"] == "spread_evenly" else calendar["fixed_rate"])
        start = max(start, _aware_utc(not_before))
        return start, end, rate, start <= now < end
    return None


def _set_lane(session: Session, state: SenderDispatch, lane: SenderCampaignLane,
              calendar: dict | None, now: datetime) -> None:
    # Remove an expired contribution at its actual boundary before advancing
    # the clock to now. A delayed boundary cannot count closed-window time.
    cutoff = min(now, _aware_utc(lane.window_end)) if lane.active and lane.window_end else now
    _advance(state, cutoff)
    previous = lane.rate if lane.active else 0.0
    if lane.active:
        state.active_lanes = max(0, state.active_lanes - 1)
        if state.active_lanes == 0:
            state.idle_since = cutoff
    state.rate = max(0.0, state.rate - previous)
    if state.active_lanes == 0:
        state.rate = 0.0
    lane.active, lane.selectable, lane.rate = False, False, 0.0
    _advance(state, now)
    window = _window(calendar, now) if calendar else None
    lane.calendar = calendar or {}
    lane.calendar_key = _calendar_key(lane.calendar)
    if window is None:
        lane.window_start = lane.window_end = lane.transition_at = None
    else:
        start, end, rate, active = window
        lane.window_start, lane.window_end, lane.rate = start, end, rate
        lane.transition_at = end if active else start
        lane.active = active
        lane.selectable = active
        if active:
            if state.active_lanes == 0 and (not state.idle_since or start > _aware_utc(state.idle_since)):
                state.credit = 1.0
            state.active_lanes += 1
            state.rate += rate
            if state.virtual_time == 0 and state.last_started_at:
                elapsed = max(0.0, (now - _aware_utc(state.last_started_at)).total_seconds())
                state.credit = min(1.0, elapsed * state.rate)
            lane.finish_tag = state.virtual_time + 1.0 / rate
    _next(state, now)


def sync_campaign(session: Session, campaign: Campaign, *, now: datetime | None = None) -> None:
    """Replan only the changed campaign's account edges, preserving sent history."""
    if not enabled(session, campaign.user_id):
        return
    now = _aware_utc(now or utcnow())
    session.flush()
    if not campaign.shared_dispatch:
        session.execute(update(CampaignRecipient).where(CampaignRecipient.campaign_id == campaign.id,
            (CampaignRecipient.status.is_(None)) | (func.trim(CampaignRecipient.status) == "")
        ).values(status="approved"))
    existing = {lane.sender_id: lane for lane in session.scalars(
        select(SenderCampaignLane).where(SenderCampaignLane.campaign_id == campaign.id)
    )}
    if campaign.status == "ended":
        # Keep the already planned density until this window's boundary. Early
        # completion must not slow a neighbouring campaign's last few slots.
        from .services import campaign_daily_capacity
        capacity = campaign_daily_capacity(session, campaign, now=now)
        preserve = ((campaign.send_settings or {}).get("mode") == "autopilot"
                    and (campaign.send_settings or {}).get("pacing_mode") == "spread_evenly"
                    and capacity and capacity["sent"] >= capacity["cap"])
        for lane in existing.values():
            if preserve and lane.active:
                lane.selectable = False
                lane.calendar = {}
                lane.calendar_key = _calendar_key({})
            else:
                state = _state(session, lane.sender_id, campaign.user_id, now)
                _set_lane(session, state, lane, None, now)
        return
    senders = []
    if campaign.status in ACTIVE and campaign.selected_sender_group_id:
        try:
            senders = connected_senders(require_group(session, campaign.user_id, campaign.selected_sender_group_id))
        except LookupError:
            pass
    settings = campaign.send_settings or {}
    days = {day.day_of_week: {"cap": day.daily_cap, "start": day.start_time, "end": day.end_time}
            for day in session.scalars(select(AutopilotDaySchedule).where(AutopilotDaySchedule.campaign_id == campaign.id))}
    if not days:
        days = {day: {"cap": 40, "start": settings.get("start_time", "09:00"), "end": settings.get("end_time", "17:00")}
                for day in settings.get("days", WEEKDAY_NAMES[:5])}
    not_before = None
    if (campaign.status == "scheduled" or (not campaign.shared_dispatch and campaign.status == "autopilot")) and campaign.scheduled_at and _aware_utc(campaign.scheduled_at) > now:
        # A launch date is meaningful on first registration. A later ordinary
        # next-batch timestamp must never become a new launch date.
        not_before = _aware_utc(campaign.scheduled_at).isoformat()
    elif existing:
        saved = next(iter(existing.values())).calendar.get("not_before")
        if saved and _aware_utc(datetime.fromisoformat(saved)) > now:
            not_before = saved
    base = {"timezone": campaign_zone(session, campaign).key, "mode": settings.get("mode", "send_now"),
            "pacing": settings.get("pacing_mode", "fixed_delay"), "days": days,
            "fixed_rate": 1.0 / max(float(settings.get("delay_minutes", 5)) * 60.0, 0.001),
            "senders": max(1, len(senders)), "not_before": not_before}
    member_ids = {sender.id for sender in senders}
    for sender_id in sorted(set(existing) | member_ids):
        state = _state(session, sender_id, campaign.user_id, now)
        lane = existing.get(sender_id)
        if lane is None:
            lane = SenderCampaignLane(sender_id=sender_id, campaign_id=campaign.id,
                                      active=False, selectable=False, rate=0.0, finish_tag=0.0, calendar={})
            session.add(lane)
        _set_lane(session, state, lane, dict(base) if sender_id in member_ids else None, now)
    campaign.shared_dispatch = True
    session.flush()


def maintain(session: Session, *, limit: int = 200, now: datetime | None = None) -> dict:
    """Bounded bootstrap and boundary maintenance, independent of table size."""
    if not enabled(session, ""):
        return {"registered": 0, "boundaries": 0, "lanes": 0, "expired": 0}
    now = _aware_utc(now or utcnow())
    campaigns = list(session.scalars(select(Campaign).where(
        Campaign.shared_dispatch.is_(False), Campaign.status.in_(ACTIVE),
    ).order_by(Campaign.id).limit(limit).with_for_update(skip_locked=True)))
    for campaign in campaigns:
        sync_campaign(session, campaign, now=now)
    session.commit()
    # Coalesce identical calendar boundaries. A million campaigns with the same
    # window are advanced by one set-based database update, not a million Python
    # iterations or a million network round trips.
    boundaries, transitioned, busy = 0, 0, set()
    for _ in range(limit):
        query = select(SenderCampaignLane.sender_id, SenderCampaignLane.campaign_id,
                       SenderCampaignLane.calendar_key, SenderCampaignLane.transition_at).where(
            SenderCampaignLane.transition_at <= now)
        if busy:
            query = query.where(SenderCampaignLane.sender_id.not_in(busy))
        key = session.execute(query.order_by(SenderCampaignLane.transition_at,
            SenderCampaignLane.sender_id, SenderCampaignLane.campaign_id).limit(1)).first()
        if key is None:
            break
        sender_id, campaign_id, calendar_key, boundary = key
        state = _locked_state(session, sender_id, skip_locked=True)
        if state is None:
            busy.add(sender_id)
            continue
        lane = session.get(SenderCampaignLane, (sender_id, campaign_id), populate_existing=True)
        if not lane or lane.calendar_key != calendar_key or lane.transition_at != boundary:
            session.commit()
            continue
        condition = (SenderCampaignLane.sender_id == sender_id,
                     SenderCampaignLane.calendar_key == calendar_key,
                     SenderCampaignLane.transition_at <= now)
        count, old_rate, old_active = session.execute(select(func.count(),
            func.coalesce(func.sum(case((SenderCampaignLane.active.is_(True), SenderCampaignLane.rate), else_=0)), 0),
            func.coalesce(func.sum(case((SenderCampaignLane.active.is_(True), 1), else_=0)), 0),
        ).where(*condition)).one()
        cutoff = min(now, _aware_utc(boundary))
        _advance(state, cutoff)
        state.active_lanes = max(0, state.active_lanes - old_active)
        state.rate = max(0.0, state.rate - old_rate) if state.active_lanes else 0.0
        if old_active and state.active_lanes == 0:
            state.idle_since = cutoff
        _advance(state, now)
        window = _window(lane.calendar, now) if lane.calendar else None
        start, end, _, active = window if window else (None, None, 0.0, False)
        rate_expression = 0.0
        if window:
            if lane.calendar["mode"] == "autopilot":
                zone = ZoneInfo(lane.calendar["timezone"])
                local_date = start.astimezone(zone).date()
                day = WEEKDAY_NAMES[local_date.weekday()]
                full_start = datetime.combine(local_date,
                    time.fromisoformat(lane.calendar["days"][day]["start"]), tzinfo=zone).astimezone(timezone.utc)
                duration = (end - full_start).total_seconds()
                rate_expression = case(
                    (SenderCampaignLane.calendar["pacing"].as_string() == "spread_evenly",
                     SenderCampaignLane.calendar["days"][day]["cap"].as_integer()
                     / duration / SenderCampaignLane.calendar["senders"].as_integer()),
                    else_=SenderCampaignLane.calendar["fixed_rate"].as_float(),
                )
            else:
                rate_expression = SenderCampaignLane.calendar["fixed_rate"].as_float()
        if active:
            if state.active_lanes == 0 and (not state.idle_since or start > _aware_utc(state.idle_since)):
                state.credit = 1.0
            new_rate = session.scalar(select(func.sum(rate_expression)).where(*condition)) or 0.0
            state.rate += new_rate
            state.active_lanes += count
        values = {"window_start": start, "window_end": end, "rate": rate_expression,
                  "active": active, "selectable": active,
                  "transition_at": (end if active else start),
                  "finish_tag": state.virtual_time + 1.0 / rate_expression if active else 0.0}
        session.execute(update(SenderCampaignLane).where(*condition).values(**values)
                        .execution_options(synchronize_session=False))
        _next(state, now)
        session.commit()
        session.expire(lane)
        boundaries += 1
        transitioned += count
    # Expired reservations are fenced: an old worker cannot release a new lease.
    expired = list(session.scalars(select(SenderDispatch).where(
        SenderDispatch.lease_until <= now,
    ).order_by(SenderDispatch.lease_until, SenderDispatch.sender_id).limit(limit).with_for_update(skip_locked=True)
      .execution_options(populate_existing=True)))
    for state in expired:
        state.lease_token = state.lease_until = state.leased_campaign_id = None
        _advance(state, now)
        _next(state, now)
    session.commit()
    return {"registered": len(campaigns), "boundaries": boundaries, "lanes": transitioned, "expired": len(expired)}


def _reserve_one(session: Session, *, now: datetime) -> tuple[int, int, str] | None | bool:
    """Reserve one due account and pick one indexed virtual-finish queue entry.

    This transaction never locks a campaign. It commits the account lease before
    recipient/job creation, avoiding account->campaign / campaign->account cycles.
    """
    now = _aware_utc(now or utcnow())
    session.flush()
    pending_boundary = select(SenderCampaignLane.campaign_id).where(
        SenderCampaignLane.sender_id == SenderDispatch.sender_id,
        SenderCampaignLane.transition_at <= now,
    ).exists()
    in_flight = select(SendJob.id).where(SendJob.sender_id == SenderDispatch.sender_id,
        SendJob.status == "running").exists()
    state = session.scalar(select(SenderDispatch).join(Sender).where(
        SenderDispatch.next_at <= now, SenderDispatch.lease_token.is_(None), ~pending_boundary, ~in_flight,
        Sender.status == "connected", Sender.encrypted_oauth_credentials.is_not(None),
    ).order_by(SenderDispatch.next_at, SenderDispatch.sender_id).limit(1).with_for_update(of=SenderDispatch, skip_locked=True)
      .execution_options(populate_existing=True))
    if state is None:
        return None
    _advance(state, now)
    if state.credit < 1.0 - EPSILON:
        _next(state, now)
        session.commit()
        return False
    lane = session.scalar(select(SenderCampaignLane).where(
        SenderCampaignLane.sender_id == state.sender_id, SenderCampaignLane.active.is_(True),
        SenderCampaignLane.selectable.is_(True),
        SenderCampaignLane.window_start <= now, SenderCampaignLane.window_end > now,
    ).order_by(SenderCampaignLane.finish_tag, SenderCampaignLane.window_end,
               SenderCampaignLane.campaign_id).limit(1).with_for_update(of=SenderCampaignLane, skip_locked=True)
      .execution_options(populate_existing=True))
    if lane is None:
        # A stale/deleted/inactive campaign must not create a busy loop.
        state.next_at = None
        session.commit()
        return False
    token = secrets.token_hex(24)
    state.credit = max(0.0, state.credit - 1.0)
    state.virtual_time += 1.0 / state.rate
    lane.finish_tag = max(lane.finish_tag, state.virtual_time) + 1.0 / lane.rate
    state.lease_token, state.lease_until = token, now + LEASE_TIME
    state.last_started_at = now
    state.leased_campaign_id = lane.campaign_id
    _next(state, now)
    sender_id, campaign_id = state.sender_id, lane.campaign_id
    session.commit()
    return sender_id, campaign_id, token


def reserve_slot(session: Session, *, now: datetime | None = None) -> tuple[int, int, str] | None:
    if not enabled(session, ""):
        return None
    now = _aware_utc(now or utcnow())
    for _ in range(32):
        result = _reserve_one(session, now=now)
        if result is False:
            continue
        return result
    return None


def release_slot(session: Session, sender_id: int, token: str | None,
                 *, now: datetime | None = None, blocked_until: datetime | None = None) -> None:
    if not token:
        return
    session.flush()
    state = _locked_state(session, sender_id)
    if not state or state.lease_token != token:
        return
    now = _aware_utc(now or utcnow())
    _advance(state, now)
    state.lease_token = state.lease_until = state.leased_campaign_id = None
    _next(state, now)
    if blocked_until and state.next_at:
        state.blocked_until = _aware_utc(blocked_until)
        state.next_at = max(state.next_at, state.blocked_until)


def begin_attempt(session: Session, sender_id: int, token: str, *, now: datetime) -> bool:
    """Fence the actual Gmail call and anchor the next slot to its real start."""
    session.flush()
    state = _locked_state(session, sender_id)
    if (not state or state.lease_token != token or not state.lease_until
            or _aware_utc(state.lease_until) <= now):
        return False
    state.credit, state.clock_at, state.last_started_at = 0.0, now, now
    state.lease_until = now + LEASE_TIME
    _next(state, now)
    return True


def _materialize_job(session: Session, reservation: tuple[int, int, str], now: datetime) -> int | None:
    from .jobs import create_send_jobs_for_next_batch
    sender_id, campaign_id, token = reservation
    try:
        campaign = session.scalar(select(Campaign).where(Campaign.id == campaign_id).with_for_update())
        state = session.get(SenderDispatch, sender_id, populate_existing=True)
        if (not campaign or campaign.status not in ACTIVE or not state or state.lease_token != token
                or campaign.user_id != state.user_id):
            release_slot(session, sender_id, token, now=now)
            session.commit()
            return None
        job = session.scalar(select(SendJob).where(
            SendJob.sender_id == sender_id, SendJob.campaign_id == campaign_id,
            SendJob.status.in_(("queued", "retry")), SendJob.scheduled_for <= now,
        ).order_by(SendJob.scheduled_for, SendJob.id).limit(1).with_for_update(skip_locked=True))
        if job is None:
            result = create_send_jobs_for_next_batch(session, user_id=campaign.user_id,
                campaign_id=campaign_id, scheduled_for=now, sender_id=sender_id, shared=True)
            ids = result.get("job_ids", [])
            job = session.get(SendJob, ids[0]) if ids else None
            if job is None:
                active = session.scalar(select(SendJob.id).where(
                    SendJob.campaign_id == campaign_id, SendJob.status.in_(("queued", "running", "retry")),
                ).limit(1))
                if result.get("exhausted") and not active:
                    campaign.status = "ended"
                    campaign.scheduled_at = None
                    sync_campaign(session, campaign, now=now)
                elif result.get("reason_code") == "campaign_daily_cap_reached":
                    # Let tomorrow's window reactivate the lanes normally.
                    from .services import campaign_daily_capacity
                    capacity = campaign_daily_capacity(session, campaign, now=now)
                    if capacity and capacity["sent"] >= capacity["cap"]:
                        for lane in session.scalars(select(SenderCampaignLane).where(SenderCampaignLane.campaign_id == campaign_id)):
                            lane.selectable = False
                elif result.get("reason_code") in {"no_connected_senders", "senders_temporarily_unavailable"}:
                    sync_campaign(session, campaign, now=now)
                block = None
                if result.get("reason_code") == "daily_caps_reached":
                    from .services import local_day_bounds
                    block = local_day_bounds(session, campaign.user_id, now=now)[1]
                assigned_sender = session.get(Sender, sender_id)
                if assigned_sender and assigned_sender.recent_error_at:
                    from .services import SENDER_ERROR_COOLDOWN
                    cooldown = _aware_utc(assigned_sender.recent_error_at) + SENDER_ERROR_COOLDOWN
                    if cooldown > now:
                        block = max(block, cooldown) if block else cooldown
                release_slot(session, sender_id, token, now=now, blocked_until=block)
                session.commit()
                return None
        job.dispatch_token = token
        job.status, job.locked_at = "running", now
        session.commit()
        return job.id
    except Exception:
        session.rollback()
        release_slot(session, sender_id, token, now=now)
        session.commit()
        raise


def claim_job(session: Session, *, now: datetime | None = None) -> int | None:
    now = _aware_utc(now or utcnow())
    for _ in range(32):
        reservation = reserve_slot(session, now=now)
        if reservation is None:
            return None
        job_id = _materialize_job(session, reservation, now)
        if job_id is not None:
            return job_id
    return None


_CONFIG_KEYS = ("mode", "delay_minutes", "pacing_mode", "dry_run", "draft_scheduled_at", "days", "start_time", "end_time")


def _invalidate_groups(session: Session, group_ids: set[int], now: datetime) -> None:
    """Group edits invalidate their edges once; workers rebuild them in pages.

    This work is proportional to the changed group, never to unrelated campaigns
    and never repeated on the per-email hot path.
    """
    if not group_ids or not enabled(session, ""):
        return
    affected = select(Campaign.id).where(Campaign.selected_sender_group_id.in_(group_ids))
    totals = list(session.execute(select(SenderCampaignLane.sender_id,
        func.sum(SenderCampaignLane.rate), func.count()).where(
        SenderCampaignLane.campaign_id.in_(affected), SenderCampaignLane.active.is_(True),
    ).group_by(SenderCampaignLane.sender_id).order_by(SenderCampaignLane.sender_id)))
    for sender_id, rate, count in totals:
        state = _locked_state(session, sender_id)
        if state:
            _advance(state, now)
            state.active_lanes = max(0, state.active_lanes - count)
            state.rate = max(0.0, state.rate - rate) if state.active_lanes else 0.0
            _next(state, now)
    session.execute(update(SenderCampaignLane).where(SenderCampaignLane.campaign_id.in_(affected)).values(
        active=False, selectable=False, rate=0.0, calendar={}, calendar_key=_calendar_key({}), transition_at=None,
    ).execution_options(synchronize_session=False))
    session.execute(update(Campaign).where(Campaign.selected_sender_group_id.in_(group_ids),
        Campaign.status.in_(ACTIVE)).values(shared_dispatch=False).execution_options(synchronize_session=False))
    session.flush()
    session.expire_all()


def _remove_deleted_campaign(session: Session, campaign: Campaign) -> None:
    if not enabled(session, campaign.user_id):
        return
    now = _aware_utc(utcnow())
    for lane in session.scalars(select(SenderCampaignLane).where(SenderCampaignLane.campaign_id == campaign.id)):
        state = _locked_state(session, lane.sender_id)
        if state:
            _set_lane(session, state, lane, None, now)
        session.delete(lane)


def _remember_changes(session: Session) -> None:
    if session.info.get("shared_dispatch_syncing"):
        return
    campaigns = session.info.setdefault("dispatch_campaign_changes", set())
    schedule_ids = session.info.setdefault("dispatch_schedule_changes", set())
    groups = session.info.setdefault("dispatch_group_changes", set())
    for obj in list(session.new) + list(session.dirty):
        if isinstance(obj, Campaign):
            attrs = inspect(obj).attrs
            changed = any(attrs[key].history.has_changes() for key in ("status", "timezone", "selected_sender_group_id"))
            history = attrs.send_settings.history
            if history.has_changes():
                previous = history.deleted[0] if history.deleted else {}
                current = obj.send_settings or {}
                changed = changed or any(previous.get(key) != current.get(key) for key in _CONFIG_KEYS)
            if changed and (obj.status in ACTIVE or obj.shared_dispatch):
                campaigns.add(obj)
        elif isinstance(obj, AutopilotDaySchedule):
            schedule_ids.add(obj.campaign_id)
        elif isinstance(obj, Sender):
            attrs = inspect(obj).attrs
            credentials = attrs.encrypted_oauth_credentials.history
            old_credentials = credentials.deleted[0] if credentials.deleted else None
            availability_changed = (attrs.status.history.has_changes() or attrs.group_id.history.has_changes()
                                    or (credentials.has_changes() and bool(old_credentials) != bool(obj.encrypted_oauth_credentials)))
            if availability_changed:
                groups.update(group.id for group in obj.groups if group.id is not None)
                if obj.group_id is not None:
                    groups.add(obj.group_id)
                groups.update(value for value in attrs.group_id.history.deleted if value is not None)
        elif isinstance(obj, SenderGroup) and inspect(obj).attrs.members.history.has_changes():
            if obj.id is not None:
                groups.add(obj.id)
    for obj in session.deleted:
        if isinstance(obj, AutopilotDaySchedule):
            schedule_ids.add(obj.campaign_id)
        elif isinstance(obj, Sender):
            groups.update(group.id for group in obj.groups if group.id is not None)
            if obj.group_id is not None:
                groups.add(obj.group_id)


@event.listens_for(Session, "before_flush")
def _before_flush(session: Session, _context, _instances) -> None:
    if not session.info.get("shared_dispatch_syncing"):
        for obj in list(session.deleted):
            if isinstance(obj, Campaign):
                _remove_deleted_campaign(session, obj)
    _remember_changes(session)


@event.listens_for(Session, "after_rollback")
def _after_rollback(session: Session) -> None:
    session.info.pop("dispatch_campaign_changes", None)
    session.info.pop("dispatch_schedule_changes", None)
    session.info.pop("dispatch_group_changes", None)


@event.listens_for(Session, "before_commit")
def _sync_changed_campaigns(session: Session) -> None:
    """Replan changed campaigns in the same transaction as their settings."""
    if session.info.get("shared_dispatch_syncing"):
        return
    _remember_changes(session)
    campaigns = session.info.pop("dispatch_campaign_changes", set())
    schedule_ids = session.info.pop("dispatch_schedule_changes", set())
    groups = session.info.pop("dispatch_group_changes", set())
    if not campaigns and not schedule_ids and not groups:
        return
    session.info["shared_dispatch_syncing"] = True
    try:
        session.flush()
        _invalidate_groups(session, groups, _aware_utc(utcnow()))
        for campaign_id in schedule_ids:
            campaign = session.get(Campaign, campaign_id)
            if campaign and (campaign.status in ACTIVE or campaign.shared_dispatch):
                campaigns.add(campaign)
        for campaign in sorted(campaigns, key=lambda value: value.id):
            sync_campaign(session, campaign)
    finally:
        session.info.pop("shared_dispatch_syncing", None)
