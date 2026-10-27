"""Read-only estimates of today's capacity across campaigns sharing senders."""

from collections import Counter, defaultdict
from datetime import datetime, time
from math import ceil

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from src.template_engine import contact_context, missing_template_variables
from .models import AutopilotDaySchedule, Campaign, CampaignRecipient, Contact, Sender, SenderGroup, SenderGroupMembership, SendJob, SendLog
from .services import WEEKDAY_NAMES, _aware_utc, campaign_zone, day_bounds_for_zone, evenly_spaced_next_batch_at, user_zone
from .time import utcnow


def _share_capacity(demand: dict[int, int], capacity: int) -> dict[int, int]:
    """Water-fill a shared pool, redistributing unused shares from smaller campaigns."""
    allocation = dict.fromkeys(demand, 0)
    pending = sorted(key for key, value in demand.items() if value > 0)
    while capacity > 0 and pending:
        share = max(1, capacity // len(pending))
        for key in pending:
            grant = min(share, demand[key] - allocation[key], capacity)
            allocation[key] += grant
            capacity -= grant
        pending = [key for key in pending if allocation[key] < demand[key]]
    return allocation


def _share_sender_capacity(demand: dict[int, int], choices: dict[int, set[int]], capacity: dict[int, int]) -> dict[int, int]:
    """Share each canonical account once, even when sender groups overlap."""
    allocation = dict.fromkeys(demand, 0)
    free = dict(capacity)
    assigned: dict[int, Counter] = defaultdict(Counter)
    pending = [key for key in sorted(demand) if demand[key] > 0]
    contention = Counter(sender_id for key in pending for sender_id in choices.get(key, set()))

    def assign(key: int, visited: set[int]) -> bool:
        candidates = sorted(choices.get(key, set()), key=lambda item: (
            free.get(item, 0) <= 0,
            contention[item],
            -free.get(item, 0), item,
        ))
        for sender_id in candidates:
            if sender_id in visited or sender_id not in free:
                continue
            visited.add(sender_id)
            if free[sender_id] > 0:
                free[sender_id] -= 1
                assigned[sender_id][key] += 1
                return True
            # Move an earlier allocation to an alternate account when needed.
            # Its campaign keeps its share, and overlapping groups can still
            # use all available capacity instead of stranding a scarce account.
            for owner, amount in list(assigned[sender_id].items()):
                if amount > 0 and assign(owner, visited):
                    assigned[sender_id][owner] -= 1
                    assigned[sender_id][key] += 1
                    return True
        return False

    while pending:
        progressed = False
        for key in pending:
            if assign(key, set()):
                allocation[key] += 1
                progressed = True
        pending = [key for key in pending if allocation[key] < demand[key]]
        if not progressed:
            break
    return allocation


def daily_campaign_capacity(session: Session, user_id: str, *, now: datetime | None = None) -> dict:
    """Estimate totals, not additional sends; never reserve slots or enqueue work.

    Campaign counts and windows use each campaign's timezone. Sender quotas use
    the account timezone, matching delivery policy. Existing reservations take
    priority; remaining capacity is shared evenly among live campaign demands.
    This is a changing estimate, not a reservation or a delivery guarantee.
    """
    current = _aware_utc(now or utcnow())
    account_zone = user_zone(session, user_id)
    account_start, account_end = day_bounds_for_zone(account_zone, now=current)
    campaigns = list(session.scalars(select(Campaign).where(Campaign.user_id == user_id).order_by(Campaign.id)))
    if not campaigns:
        return {"items": [], "as_of": current.isoformat(), "account_timezone": account_zone.key}
    by_id = {campaign.id: campaign for campaign in campaigns}
    bounds = {campaign.id: day_bounds_for_zone(campaign_zone(session, campaign), now=current) for campaign in campaigns}
    lower = min(account_start, *(start for start, _ in bounds.values()))
    upper = max(account_end, *(end for _, end in bounds.values()))
    senders = list(session.scalars(select(Sender).where(Sender.user_id == user_id)))
    connected = {sender.id: sender for sender in senders if sender.status == "connected" and sender.encrypted_oauth_credentials}
    groups: dict[int, list[Sender]] = defaultdict(list)
    for sender in connected.values():
        if sender.group_id is not None:
            groups[sender.group_id].append(sender)
    for group_id, sender_id in session.execute(
        select(SenderGroupMembership.group_id, SenderGroupMembership.sender_id)
        .join(SenderGroup, SenderGroup.id == SenderGroupMembership.group_id)
        .where(SenderGroup.user_id == user_id)
    ):
        sender = connected.get(sender_id)
        if sender and sender not in groups[group_id]:
            groups[group_id].append(sender)

    actual = Counter()
    campaign_usage = Counter()
    sender_usage = Counter()
    for log in session.execute(select(SendLog.campaign_id, SendLog.sender_id, SendLog.status, SendLog.sent_at).where(
        SendLog.user_id == user_id,
        SendLog.status.in_(("sent", "bounced", "test_sent")),
        SendLog.sent_at >= lower, SendLog.sent_at < upper,
    )):
        sent_at = _aware_utc(log.sent_at)
        if log.campaign_id in bounds:
            start, end = bounds[log.campaign_id]
            if start <= sent_at < end:
                campaign_usage[log.campaign_id] += 1
                if log.status != "test_sent":
                    actual[log.campaign_id] += 1
        if log.status != "test_sent" and account_start <= sent_at < account_end:
            sender_usage[log.sender_id] += 1

    schedules = {(row.campaign_id, row.day_of_week): row for row in session.scalars(
        select(AutopilotDaySchedule).where(AutopilotDaySchedule.campaign_id.in_(by_id))
    )}
    jobs = list(session.scalars(select(SendJob).where(
        SendJob.user_id == user_id,
        SendJob.status.in_(("queued", "running", "retry")),
        SendJob.scheduled_for < upper,
    ).order_by(SendJob.scheduled_for, SendJob.id)))
    jobs_by_recipient = {(job.campaign_id, job.recipient_id): job for job in jobs}
    eligible = Counter()
    valid_recipients = set()
    for campaign_id, contact_id, status, fields, email in session.execute(
        select(CampaignRecipient.campaign_id, Contact.id, CampaignRecipient.status, Contact.custom_fields, Contact.email_normalized)
        .join(Contact, Contact.id == CampaignRecipient.contact_id)
        .where(CampaignRecipient.campaign_id.in_(by_id), Contact.user_id == user_id,
               Contact.status.in_(("approved", "sent")),
               or_(CampaignRecipient.status.in_(("approved", "queued")), CampaignRecipient.status.is_(None), CampaignRecipient.status == ""))
    ):
        campaign = by_id[campaign_id]
        if campaign.status not in {"sending", "scheduled", "autopilot"} or (campaign.send_settings or {}).get("dry_run"):
            continue
        job = jobs_by_recipient.get((campaign_id, contact_id))
        if status == "queued" and job is None:
            continue
        context = contact_context({"email": email, "custom_fields": fields})
        if any(missing_template_variables(template or "", context) for template in (
            campaign.subject_template, campaign.body_template or campaign.fallback_body_template,
        )):
            continue
        if job and _aware_utc(job.scheduled_for) >= bounds[campaign_id][1]:
            continue
        eligible[campaign_id] += 1
        valid_recipients.add((campaign_id, contact_id))

    items = {}
    demand = {}
    for campaign in campaigns:
        zone = campaign_zone(session, campaign)
        local = current.astimezone(zone)
        settings = campaign.send_settings or {}
        mode = settings.get("mode", "send_now")
        members = groups.get(campaign.selected_sender_group_id, [])
        sender_limit = sum(sender.daily_cap for sender in members)
        campaign_limit = None
        window_start = None
        start, end = current, bounds[campaign.id][1]
        state = "active"
        if mode == "autopilot":
            day = schedules.get((campaign.id, WEEKDAY_NAMES[local.weekday()]))
            if day:
                campaign_limit = day.daily_cap
                window_start = datetime.combine(local.date(), time.fromisoformat(day.start_time), tzinfo=zone)
                start = max(start, window_start)
                end = datetime.combine(local.date(), time.fromisoformat(day.end_time), tzinfo=zone)
            elif any(key[0] == campaign.id for key in schedules) or WEEKDAY_NAMES[local.weekday()] not in (settings.get("days") or WEEKDAY_NAMES[:5]):
                campaign_limit = 0
                state = "day_disabled"
            else:
                start = max(start, datetime.combine(local.date(), time.fromisoformat(settings.get("start_time", "09:00")), tzinfo=zone))
                end = datetime.combine(local.date(), time.fromisoformat(settings.get("end_time", "17:00")), tzinfo=zone)
        if campaign.scheduled_at:
            start = max(start, _aware_utc(campaign.scheduled_at))
        if end <= start and state == "active":
            state = "window_closed"
        if campaign.status not in {"sending", "scheduled", "autopilot"}:
            state = campaign.status
        elif settings.get("dry_run"):
            state = "test_mode"
        elif not members:
            state = "no_senders"
        # Keep the account ceiling visible even on a disabled campaign day.
        # The campaign's own cap still limits demand and the estimated total.
        limit = sender_limit
        remaining = min(
            max(0, (campaign_limit if campaign_limit is not None else sender_limit) - campaign_usage[campaign.id]),
            max(0, limit - actual[campaign.id]),
        )
        available_seconds = max(0, (end - start).total_seconds())
        # A partial day has only the slots left at the full-window rate.
        # The worker wakes once per minute and may reduce the actual throughput.
        if window_start is not None and campaign_limit and settings.get("pacing_mode") == "spread_evenly":
            paced_next = evenly_spaced_next_batch_at(
                current=current, start_at=window_start, end_at=end,
                daily_target=campaign_limit, batch_size=len(members),
            )
            interval = max(60, (paced_next - current).total_seconds())
            pacing_bound = ceil(available_seconds / interval) * min(len(members), campaign_limit)
        else:
            delay = max(1, int(settings.get("delay_minutes", 5)))
            pacing_bound = (int(available_seconds // (delay * 60)) + 1) * len(members)
        demand[campaign.id] = min(eligible[campaign.id], remaining, pacing_bound) if state == "active" else 0
        items[campaign.id] = {
            "campaign_id": campaign.id, "timezone": zone.key, "date": local.date().isoformat(),
            "sent_today": actual[campaign.id], "estimated_total_today": actual[campaign.id],
            "daily_limit": limit, "campaign_limit": campaign_limit, "sender_limit": sender_limit,
            "competing_campaigns": 0, "state": state,
        }

    # Match the worker's sender reservation accounting, including slots held
    # by paused campaigns. Do not count an existing job again as new capacity.
    free = {sender.id: max(0, sender.daily_cap - sender_usage[sender.id]) for sender in connected.values()}
    reserved = Counter()
    for job in jobs:
        if job.sender_id not in free or not account_start <= _aware_utc(job.scheduled_for) < account_end:
            continue
        if free[job.sender_id] <= 0:
            continue
        free[job.sender_id] -= 1
        if ((job.campaign_id, job.recipient_id) in valid_recipients
                and reserved[job.campaign_id] < demand[job.campaign_id]):
            reserved[job.campaign_id] += 1
    choices = {campaign.id: {sender.id for sender in groups.get(campaign.selected_sender_group_id, [])} for campaign in campaigns}
    allocation = _share_sender_capacity({key: max(0, value - reserved[key]) for key, value in demand.items()}, choices, free)
    for campaign_id, grant in allocation.items():
        items[campaign_id]["estimated_total_today"] += reserved[campaign_id] + grant
        items[campaign_id]["competing_campaigns"] = sum(
            other != campaign_id and demand[other] > 0 and bool(choices[campaign_id] & choices[other]) for other in demand
        )
    return {"items": list(items.values()), "as_of": current.isoformat(), "account_timezone": account_zone.key}
