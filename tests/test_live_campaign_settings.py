from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from api.routers import campaign_delivery as delivery
from src.platform import jobs, scheduler, services
from src.platform.models import AutopilotDaySchedule, Base, Campaign, CampaignRecipient, Contact, Sender, SenderGroup, SendJob, SendLog, UserSettings


USER = "live-settings-user"
NOW = datetime(2026, 10, 27, 16, 45, tzinfo=timezone.utc)


def aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'live.db'}")
    Base.metadata.create_all(engine)
    clock = [NOW]
    for module in (services, scheduler, jobs, delivery):
        monkeypatch.setattr(module, "utcnow", lambda: clock[0])
    with Session(engine, expire_on_commit=False) as session:
        services.ensure_user(session, USER)
        session.get(UserSettings, USER).timezone = "UTC"
        group = SenderGroup(user_id=USER, name="Original")
        session.add(group)
        session.flush()
        sender = Sender(user_id=USER, group_id=group.id, email="sender@example.test", status="connected",
                        encrypted_oauth_credentials="test-only", daily_cap=1000)
        campaign = Campaign(user_id=USER, name="Late change", timezone="UTC", status="autopilot",
                            selected_sender_group_id=group.id, subject_template="Hello", body_template="Hi",
                            send_settings={"mode": "autopilot", "pacing_mode": "spread_evenly", "delay_minutes": 5})
        session.add_all([sender, campaign])
        session.flush()
        for day in ("tuesday", "wednesday"):
            session.add(AutopilotDaySchedule(campaign_id=campaign.id, day_of_week=day, daily_cap=10, start_time="09:00", end_time="17:00"))
        for index in range(20):
            contact = Contact(user_id=USER, email_normalized=f"lead{index}@example.test", status="approved")
            session.add(contact)
            session.flush()
            session.add(CampaignRecipient(campaign_id=campaign.id, contact_id=contact.id, status="approved"))
        session.commit()
        yield session, campaign, sender, clock
    engine.dispose()


def sent(session, campaign, sender, count):
    for index in range(count):
        session.add(SendLog(user_id=USER, campaign_id=campaign.id, sender_id=sender.id,
                            recipient_email=f"delivered{index}@example.test", status="sent", sent_at=NOW - timedelta(hours=1)))
    session.commit()


def pending_job(session, campaign, sender, index=0, status="queued"):
    recipient = list(session.scalars(select(CampaignRecipient).where(CampaignRecipient.campaign_id == campaign.id).order_by(CampaignRecipient.contact_id)))[index]
    recipient.status = "queued"
    job = SendJob(user_id=USER, campaign_id=campaign.id, sender_id=sender.id, recipient_id=recipient.contact_id,
                  status=status, scheduled_for=NOW, batch_id="existing", idempotency_key=f"job-{index}")
    session.add(job)
    session.commit()
    return job, recipient


def edit(session, campaign, cap=100, **settings):
    return delivery.patch_send_settings(campaign.id, delivery.SendSettingsUpdate(
        schedule={day: {"cap": cap, "start": "09:00", "end": "17:00"} for day in ("tuesday", "wednesday")},
        **settings,
    ), session, USER)


def test_raising_exhausted_target_late_waits_full_rate_before_next_batch(workspace):
    session, campaign, sender, clock = workspace
    sent(session, campaign, sender, 10)
    campaign.scheduled_at = NOW + timedelta(days=1)
    campaign.send_settings = {**campaign.send_settings, "pause_reason": "campaign_daily_cap_reached"}
    session.commit()

    edit(session, campaign)
    expected = NOW + timedelta(minutes=4.8)
    assert campaign.status == "autopilot"
    assert aware(campaign.scheduled_at) == expected
    assert campaign.send_settings["pause_reason"] is None
    assert services.campaign_sent_today(session, campaign.id) == 10
    assert session.scalar(select(func.count()).select_from(SendJob)) == 0
    clock[0] = expected - timedelta(seconds=1)
    assert scheduler.enqueue_due_campaign_batches(session) == []
    clock[0] = expected
    result = scheduler.enqueue_due_campaign_batches(session)
    assert result[0]["created"] == 1
    assert aware(campaign.scheduled_at) == expected + timedelta(minutes=4.8)


@pytest.mark.parametrize("hour,minute", [(9, 0), (12, 30), (16, 45)])
def test_rate_uses_entire_window_regardless_of_time_or_daily_usage(workspace, hour, minute):
    session, campaign, sender, _ = workspace
    edit(session, campaign)
    sent(session, campaign, sender, 30)
    current = NOW.replace(hour=hour, minute=minute)
    state = services.campaign_batch_schedule(session, campaign, after=current, delay_minutes=0, batch_size=1)
    assert state["next_at"] == current + timedelta(minutes=4.8)


def test_lowering_target_defers_pending_work_but_preserves_claimed_email_and_history(workspace):
    session, campaign, sender, _ = workspace
    sent(session, campaign, sender, 10)
    queued, _ = pending_job(session, campaign, sender)
    running, _ = pending_job(session, campaign, sender, index=1, status="running")
    edit(session, campaign, cap=5)
    tomorrow = datetime(2026, 10, 28, 9, tzinfo=timezone.utc)
    assert campaign.status == "autopilot"
    assert aware(campaign.scheduled_at) == aware(queued.scheduled_for) == tomorrow
    assert aware(running.scheduled_for) == NOW and running.status == "running"
    assert services.delivery_policy_state(session, campaign, sender)["reason_code"] == "campaign_daily_cap_reached"
    assert services.campaign_sent_today(session, campaign.id) == 10


def test_pending_reservation_does_not_exhaust_a_day_with_no_sent_emails(workspace):
    session, campaign, sender, _ = workspace
    queued, _ = pending_job(session, campaign, sender)
    edit(session, campaign, cap=2)
    # The new interval would fall after 5 PM, so use a shorter fixed delay.
    delivery.patch_send_settings(campaign.id, delivery.SendSettingsUpdate(pacing_mode="fixed_delay", delay_minutes=2), session, USER)
    assert aware(queued.scheduled_for) == NOW + timedelta(minutes=2)
    assert services.delivery_policy_state(session, campaign, sender)["allowed"]


def test_shrinking_window_moves_pending_work_to_next_enabled_day(workspace):
    session, campaign, sender, _ = workspace
    queued, _ = pending_job(session, campaign, sender)
    delivery.patch_send_settings(campaign.id, delivery.SendSettingsUpdate(schedule={
        "tuesday": {"cap": 100, "start": "09:00", "end": "16:00"},
        "wednesday": {"cap": 100, "start": "09:00", "end": "17:00"},
    }), session, USER)
    assert aware(queued.scheduled_for) == datetime(2026, 10, 28, 9, tzinfo=timezone.utc)
    assert not services.delivery_policy_state(session, campaign, sender)["allowed"]


@pytest.mark.parametrize("payload", [{"mode": "send_now"}, {"timezone": "Asia/Dubai"}, {"dry_run": True}, {"scheduled_at": None}])
def test_live_edits_reject_changes_that_require_a_pause(workspace, payload):
    session, campaign, _, _ = workspace
    with pytest.raises(HTTPException) as error:
        delivery.patch_send_settings(campaign.id, delivery.SendSettingsUpdate(**payload), session, USER)
    assert error.value.status_code == 409
    assert campaign.status == "autopilot" and campaign.timezone == "UTC"


def test_empty_schedule_and_other_users_edits_are_rejected(workspace):
    session, campaign, _, _ = workspace
    with pytest.raises(ValidationError):
        delivery.SendSettingsUpdate(schedule={})
    with pytest.raises(HTTPException) as error:
        delivery.patch_send_settings(campaign.id, delivery.SendSettingsUpdate(delay_minutes=2), session, "someone-else")
    assert error.value.status_code == 404


def test_paused_edits_save_without_starting_or_retiming_queued_jobs(workspace):
    session, campaign, sender, _ = workspace
    queued, _ = pending_job(session, campaign, sender)
    campaign.status = "paused"
    session.commit()
    edit(session, campaign, timezone="Asia/Dubai", dry_run=True)
    assert campaign.status == "paused" and campaign.timezone == "Asia/Dubai"
    assert aware(queued.scheduled_for) == NOW


def test_rescheduling_paused_campaign_keeps_queued_work_at_the_new_date(workspace, monkeypatch):
    session, campaign, sender, _ = workspace
    queued, _ = pending_job(session, campaign, sender)
    campaign.status = "paused"
    session.commit()
    start = NOW + timedelta(days=1)
    delivery.patch_send_settings(campaign.id, delivery.SendSettingsUpdate(mode="schedule", scheduled_at=start), session, USER)
    assert campaign.status == "paused"
    monkeypatch.setattr(delivery, "require_delivery_enabled", lambda: None)
    response = delivery.post_resume(campaign.id, session, USER)
    assert response["status"] == "scheduled" and response["queued"] == 1
    assert aware(queued.scheduled_for) == aware(campaign.scheduled_at) == start


def test_sender_group_change_reassigns_only_unclaimed_work(workspace):
    session, campaign, sender, _ = workspace
    queued, recipient = pending_job(session, campaign, sender)
    running, _ = pending_job(session, campaign, sender, index=1, status="running")
    new_group = SenderGroup(user_id=USER, name="New group")
    session.add(new_group)
    session.flush()
    session.add(Sender(user_id=USER, group_id=new_group.id, email="new@example.test", status="connected",
                       encrypted_oauth_credentials="test-only", daily_cap=1000))
    session.commit()
    delivery.patch_campaign_sender_group(campaign.id, delivery.CampaignSenderGroupSelect(sender_group_id=new_group.id), session, USER)
    assert campaign.selected_sender_group_id == new_group.id and campaign.status == "autopilot"
    assert session.get(SendJob, queued.id) is None and recipient.status == "approved"
    assert session.get(SendJob, running.id).status == "running"


def test_live_group_must_have_a_connected_sender(workspace):
    session, campaign, sender, _ = workspace
    group = SenderGroup(user_id=USER, name="Empty")
    session.add(group)
    session.commit()
    with pytest.raises(HTTPException) as error:
        delivery.patch_campaign_sender_group(campaign.id, delivery.CampaignSenderGroupSelect(sender_group_id=group.id), session, USER)
    assert error.value.status_code == 422
    assert campaign.selected_sender_group_id == sender.group_id


def test_repeated_save_does_not_postpone_the_next_send(workspace):
    session, campaign, _, clock = workspace
    edit(session, campaign)
    original = campaign.scheduled_at
    clock[0] += timedelta(minutes=1)
    edit(session, campaign)
    assert campaign.scheduled_at == original


def test_daily_target_compatibility_endpoint_uses_same_steady_rate(workspace):
    session, campaign, _, _ = workspace
    delivery.patch_autopilot_daily_limits(campaign.id, delivery.AutopilotDailyLimitsUpdate(daily_limits={"tuesday": 100}), session, USER)
    assert aware(campaign.scheduled_at) == NOW + timedelta(minutes=4.8)


def test_window_end_is_exclusive(workspace):
    session, campaign, _, _ = workspace
    end = NOW.replace(hour=17, minute=0)
    state = services.autopilot_window_state(session, campaign, now=end)
    assert not state["allowed"]
    assert state["next_at"] == datetime(2026, 10, 28, 9, tzinfo=timezone.utc)
