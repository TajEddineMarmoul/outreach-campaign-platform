from collections import Counter
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from src.platform import jobs, scheduler, services, shared_dispatch, worker
from src.platform.models import AutopilotDaySchedule, Base, Campaign, CampaignRecipient, Contact, Sender, SenderCampaignLane, SenderDispatch, SenderGroup, SendJob, SendLog, UserSettings

NOW = datetime(2026, 10, 26, 10, tzinfo=timezone.utc)
USER = "shared-test"


def aware(value):
    return services._aware_utc(value)


@pytest.fixture
def world(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'shared.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, autoflush=False, expire_on_commit=False)
    clock, delivered = [NOW], []
    for module in (jobs, scheduler, services, shared_dispatch, worker):
        monkeypatch.setattr(module, "utcnow", lambda: clock[0])
    monkeypatch.setenv("OUTREACH_SHARED_DISPATCH", "true")
    monkeypatch.setattr(jobs, "SessionLocal", factory)
    monkeypatch.setattr(worker, "SessionLocal", factory)
    monkeypatch.setattr(jobs, "gmail_service_for_sender", lambda *_: object())

    def send(**kwargs):
        delivered.append({"at": clock[0], **kwargs})
        return SimpleNamespace(message_id=f"message-{len(delivered)}", thread_id=f"thread-{len(delivered)}")

    monkeypatch.setattr(jobs, "send_email", send)
    monkeypatch.setattr(jobs, "fake_send_email", send)
    with factory() as session:
        services.ensure_user(session, USER)
        session.get(UserSettings, USER).timezone = "UTC"
        session.commit()

    def group(name="Default", count=1, cap=1000):
        with factory() as session:
            group = SenderGroup(user_id=USER, name=name)
            session.add(group)
            session.flush()
            for index in range(count):
                session.add(Sender(user_id=USER, group_id=group.id, email=f"{name}-{index}@example.test",
                                   status="connected", encrypted_oauth_credentials="test-only", daily_cap=cap))
            session.commit()
            return group.id

    def campaign(group_id, name, target, start, end, *, recipients=None):
        with factory() as session:
            campaign = Campaign(user_id=USER, name=name, timezone="UTC", status="draft",
                                selected_sender_group_id=group_id, subject_template=name, body_template="Hello",
                                send_settings={"mode": "autopilot", "pacing_mode": "spread_evenly", "delay_minutes": 5})
            session.add(campaign)
            session.flush()
            session.add(AutopilotDaySchedule(campaign_id=campaign.id, day_of_week="monday", daily_cap=target,
                                            start_time=start, end_time=end))
            for index in range(recipients if recipients is not None else target):
                contact = Contact(user_id=USER, email_normalized=f"c{campaign.id}-{index}@example.test", status="approved")
                session.add(contact)
                session.flush()
                session.add(CampaignRecipient(campaign_id=campaign.id, contact_id=contact.id, status="approved"))
            session.flush()
            campaign.status = "autopilot"
            campaign.scheduled_at = NOW.replace(hour=int(start[:2]), minute=int(start[3:]))
            session.commit()
            assert campaign.shared_dispatch
            return campaign.id

    def run_until(end):
        steps = 0
        while clock[0] < end:
            worker.run_worker_cycle(max_jobs=100)
            with factory() as session:
                wakeups = [aware(value) for value in session.scalars(select(SenderDispatch.next_at).where(SenderDispatch.next_at.is_not(None)))]
                wakeups += [aware(value) for value in session.scalars(select(SenderCampaignLane.transition_at).where(SenderCampaignLane.transition_at.is_not(None)))]
            future = [value for value in wakeups if value > clock[0]]
            if not future:
                break
            clock[0] = min(min(future), end)
            steps += 1
            assert steps < 10000, "The queue did not advance"

    yield SimpleNamespace(engine=engine, factory=factory, clock=clock, delivered=delivered,
                          group=group, campaign=campaign, run_until=run_until)
    engine.dispose()


def test_demonstrated_10_and_12_example_is_one_even_22_slot_timeline(world):
    group = world.group()
    world.campaign(group, "A", 10, "10:00", "12:00")
    world.campaign(group, "B", 12, "11:00", "13:00")
    world.run_until(NOW.replace(hour=13))
    assert Counter(item["subject"] for item in world.delivered) == {"A": 10, "B": 12}
    assert [sum(start <= item["at"] < start + timedelta(hours=1) for item in world.delivered)
            for start in [NOW, NOW + timedelta(hours=1), NOW + timedelta(hours=2)]] == [5, 11, 6]
    overlap = [item for item in world.delivered if item["at"].hour == 11]
    for previous, following in zip(overlap, overlap[1:]):
        assert (following["at"] - previous["at"]).total_seconds() == pytest.approx(3600 / 11, abs=0.001)
    assert len({item["at"] for item in world.delivered}) == 22


def test_three_campaigns_get_proportional_shares_without_a_campaign_count_limit(world):
    group = world.group()
    for name, target in [("A", 5), ("B", 6), ("C", 3)]:
        world.campaign(group, name, target, "10:00", "11:00")
    world.run_until(NOW + timedelta(hours=1))
    assert Counter(item["subject"] for item in world.delivered) == {"A": 5, "B": 6, "C": 3}
    gaps = [(b["at"] - a["at"]).total_seconds() for a, b in zip(world.delivered, world.delivered[1:])]
    assert gaps == pytest.approx([3600 / 14] * 13, abs=0.001)


def test_distinct_accounts_are_independent_and_have_no_global_parallel_limit(world):
    group = world.group(count=4)
    world.campaign(group, "Four accounts", 8, "10:00", "11:00")
    world.run_until(NOW + timedelta(hours=1))
    assert len(world.delivered) == 8
    assert sum(item["at"] == NOW for item in world.delivered) == 4
    assert set(Counter(item["sender"] for item in world.delivered).values()) == {2}


def test_no_fixed_30_second_gap_is_imposed(world):
    group = world.group()
    world.campaign(group, "Fast", 4, "10:00", "10:01")
    world.run_until(NOW + timedelta(minutes=1))
    assert len(world.delivered) == 4
    assert [(item["at"] - NOW).total_seconds() for item in world.delivered] == [0, 15, 30, 45]


def test_late_start_does_not_backfill_earlier_slots(world):
    world.clock[0] = NOW + timedelta(hours=1)
    group = world.group()
    world.campaign(group, "Late", 10, "10:00", "12:00")
    world.run_until(NOW + timedelta(hours=2))
    assert len(world.delivered) == 5
    assert all(item["at"] >= NOW + timedelta(hours=1) for item in world.delivered)


def test_pause_removes_rate_and_resume_does_not_reset_other_campaigns(world):
    group = world.group()
    a = world.campaign(group, "A", 10, "10:00", "12:00")
    world.campaign(group, "B", 12, "10:00", "12:00")
    with world.factory() as session:
        campaign = session.get(Campaign, a)
        campaign.status = "paused"
        session.flush()  # Explicit flush must not hide the change from the hook.
        session.commit()
        state = session.scalar(select(SenderDispatch))
        assert state.rate == pytest.approx(6 / 3600)
    world.run_until(NOW + timedelta(hours=1))
    assert all(item["subject"] == "B" for item in world.delivered)


def test_a_pending_sender_reservation_cannot_be_claimed_twice(world):
    group = world.group()
    world.campaign(group, "A", 10, "10:00", "12:00")
    with world.factory() as first, world.factory() as second:
        reservation = shared_dispatch.reserve_slot(first, now=NOW)
        assert reservation is not None
        assert shared_dispatch.reserve_slot(second, now=NOW) is None
        sender_id, _, token = reservation
        shared_dispatch.release_slot(second, sender_id, "wrong-token", now=NOW)
        second.commit()
        assert second.get(SenderDispatch, sender_id).lease_token == token


def test_a_delayed_worker_never_sends_a_backlog_in_one_cycle(world):
    group = world.group()
    world.campaign(group, "A", 20, "10:00", "12:00")
    world.clock[0] += timedelta(hours=1)
    assert worker.run_worker_cycle(max_jobs=100) == 1
    assert len(world.delivered) == 1
    assert worker.run_worker_cycle(max_jobs=100) == 0


def test_sender_daily_cap_still_applies_across_campaigns(world):
    group = world.group(cap=3)
    world.campaign(group, "A", 10, "10:00", "12:00")
    world.campaign(group, "B", 12, "10:00", "12:00")
    world.run_until(NOW + timedelta(hours=2))
    assert len(world.delivered) == 3


def test_unfinished_campaign_is_reactivated_at_its_next_weekly_window(world):
    group = world.group()
    campaign_id = world.campaign(group, "Weekly", 2, "10:00", "11:00", recipients=4)
    world.run_until(NOW + timedelta(hours=1))
    assert len(world.delivered) == 2
    world.clock[0] = NOW + timedelta(days=7)
    world.run_until(NOW + timedelta(days=7, hours=1))
    assert len(world.delivered) == 4
    with world.factory() as session:
        assert session.get(Campaign, campaign_id).status == "ended"


def test_a_live_target_edit_preserves_progress_instead_of_sending_immediately(world):
    group = world.group()
    campaign_id = world.campaign(group, "A", 10, "10:00", "12:00", recipients=30)
    assert worker.run_worker_cycle() == 1
    world.clock[0] = NOW + timedelta(minutes=1)
    with world.factory() as session:
        day = session.scalar(select(AutopilotDaySchedule).where(AutopilotDaySchedule.campaign_id == campaign_id))
        day.daily_cap = 20
        session.flush()
        session.commit()
        state = session.scalar(select(SenderDispatch))
        assert state.rate == pytest.approx(20 / 7200)
        assert aware(state.next_at) == NOW + timedelta(minutes=6, seconds=30)
    assert worker.run_worker_cycle() == 0


def test_insufficient_audience_does_not_leave_phantom_rate_for_other_campaigns(world):
    group = world.group()
    world.campaign(group, "Short", 100, "10:00", "12:00", recipients=1)
    world.campaign(group, "B", 12, "10:00", "12:00")
    assert worker.run_worker_cycle() == 1
    assert world.delivered[0]["subject"] == "Short"
    with world.factory() as session:
        state = session.scalar(select(SenderDispatch))
        assert state.rate == pytest.approx(6 / 3600)
        assert state.active_lanes == 1


def test_canonical_accounts_in_overlapping_groups_share_one_clock(world):
    group_a = world.group("A")
    group_b = world.group("B")
    with world.factory() as session:
        senders = list(session.scalars(select(Sender).order_by(Sender.id)))
        combined = SenderGroup(user_id=USER, name="Both", members=senders)
        session.add(combined)
        session.commit()
        both = combined.id
    world.campaign(group_a, "Ten", 10, "10:00", "18:00")
    world.campaign(group_a, "Twenty", 20, "10:00", "18:00")
    world.campaign(both, "Hundred", 100, "10:00", "18:00")
    with world.factory() as session:
        states = list(session.scalars(select(SenderDispatch).order_by(SenderDispatch.sender_id)))
        assert [state.rate for state in states] == pytest.approx([80 / 28800, 50 / 28800])
    world.run_until(NOW.replace(hour=18))
    assert Counter(item["subject"] for item in world.delivered) == {"Ten": 10, "Twenty": 20, "Hundred": 100}
    assert sorted(Counter(item["sender"] for item in world.delivered).values()) == [50, 80]


def test_group_membership_changes_are_replanned_without_duplicate_accounts(world):
    group_id = world.group(count=2)
    world.campaign(group_id, "A", 10, "10:00", "12:00")
    with world.factory() as session:
        sender = session.scalar(select(Sender).order_by(Sender.id))
        sender.status = "removed"
        session.commit()
        states = list(session.scalars(select(SenderDispatch)))
        assert all(state.rate == 0 for state in states)
        shared_dispatch.maintain(session, now=NOW)
        rates = list(session.scalars(select(SenderDispatch.rate).order_by(SenderDispatch.sender_id)))
        assert rates == pytest.approx([0, 10 / 7200])
    world.run_until(NOW.replace(hour=12))
    assert len(world.delivered) == 10
    assert len({item["sender"] for item in world.delivered}) == 1


def test_deleting_a_finished_campaign_removes_its_retained_contribution(world):
    group = world.group()
    campaign_id = world.campaign(group, "A", 1, "10:00", "12:00")
    assert worker.run_worker_cycle() == 1
    with world.factory() as session:
        assert session.scalar(select(SenderDispatch.rate)) == pytest.approx(1 / 7200)
        session.delete(session.get(Campaign, campaign_id))
        session.commit()
        assert session.scalar(select(SenderDispatch.rate)) == 0


def test_matching_windows_with_different_targets_are_activated_in_one_group(world):
    group = world.group()
    world.campaign(group, "A", 10, "11:00", "13:00")
    world.campaign(group, "B", 12, "11:00", "13:00")
    world.clock[0] = NOW.replace(hour=11)
    with world.factory() as session:
        result = shared_dispatch.maintain(session, limit=1, now=world.clock[0])
        assert result["boundaries"] == 1 and result["lanes"] == 2
        assert session.scalar(select(SenderDispatch.rate)) == pytest.approx(11 / 3600)
    world.run_until(NOW.replace(hour=13))
    assert Counter(item["subject"] for item in world.delivered) == {"A": 10, "B": 12}


def test_a_late_boundary_uses_full_window_duration(world):
    group = world.group()
    world.campaign(group, "B", 12, "11:00", "13:00")
    world.clock[0] = NOW.replace(hour=11, minute=30)
    with world.factory() as session:
        shared_dispatch.maintain(session, now=world.clock[0])
        assert session.scalar(select(SenderDispatch.rate)) == pytest.approx(6 / 3600)
    world.run_until(NOW.replace(hour=13))
    assert len(world.delivered) == 9


def test_account_state_is_refreshed_between_sessions(world):
    group = world.group()
    world.campaign(group, "A", 10, "10:00", "12:00")
    with world.factory() as first, world.factory() as second:
        for session in [first, second, first]:
            reservation = shared_dispatch.reserve_slot(session, now=world.clock[0])
            assert reservation is not None
            shared_dispatch.release_slot(session, reservation[0], reservation[2], now=world.clock[0])
            session.commit()
            world.clock[0] = aware(session.get(SenderDispatch, reservation[0]).next_at)
        first.expire_all()
        assert first.scalar(select(SenderDispatch.virtual_time)) == pytest.approx(3 * 720)


def test_expired_reservation_cannot_start_an_external_send(world, monkeypatch):
    group = world.group()
    world.campaign(group, "A", 10, "10:00", "12:00")
    original = jobs._render
    shifted = [False]
    def delayed_render(*args, **kwargs):
        if not shifted[0]:
            world.clock[0] += timedelta(minutes=16)
            shifted[0] = True
        return original(*args, **kwargs)
    monkeypatch.setattr(jobs, "_render", delayed_render)
    job_id = worker.claim_next_due_job()
    assert jobs.perform_send_job(job_id, claimed=True)["status"] == "deferred"
    assert world.delivered == []


def test_pause_during_render_is_rechecked_before_gmail(world, monkeypatch):
    group = world.group()
    campaign_id = world.campaign(group, "A", 10, "10:00", "12:00")
    original = jobs._render
    paused = [False]
    def pause(*args, **kwargs):
        if not paused[0]:
            with world.factory() as session:
                session.get(Campaign, campaign_id).status = "paused"
                session.commit()
            paused[0] = True
        return original(*args, **kwargs)
    monkeypatch.setattr(jobs, "_render", pause)
    job_id = worker.claim_next_due_job()
    assert jobs.perform_send_job(job_id, claimed=True)["status"] == "paused"
    assert world.delivered == []


def test_gmail_error_cools_the_account_across_all_its_campaigns(world, monkeypatch):
    group = world.group()
    world.campaign(group, "A", 10, "10:00", "12:00")
    world.campaign(group, "B", 12, "10:00", "12:00")
    def fail(**kwargs):
        raise RuntimeError("Temporary Gmail failure")
    monkeypatch.setattr(jobs, "send_email", fail)
    assert worker.run_worker_cycle() == 1
    with world.factory() as session:
        state = session.scalar(select(SenderDispatch))
        assert aware(state.blocked_until) == NOW + services.SENDER_ERROR_COOLDOWN
    assert worker.run_worker_cycle() == 0


def test_window_closing_during_preparation_defers_delivery(world, monkeypatch):
    group = world.group()
    world.campaign(group, "A", 10, "10:00", "12:00")
    original = jobs._render

    def delayed_render(*args, **kwargs):
        world.clock[0] = NOW.replace(hour=12)
        return original(*args, **kwargs)

    monkeypatch.setattr(jobs, "_render", delayed_render)
    job_id = worker.claim_next_due_job()
    result = jobs.perform_send_job(job_id, claimed=True)
    assert result["status"] == "deferred"
    assert result["reason_code"] == "autopilot_outside_window"
    assert world.delivered == []


def test_interrupted_job_is_recovered_without_a_backlog(world):
    group = world.group()
    world.campaign(group, "A", 20, "10:00", "12:00", recipients=30)
    first_job = worker.claim_next_due_job()
    assert first_job is not None
    world.clock[0] += timedelta(minutes=16)
    assert worker.recover_stale_jobs() == 1
    assert worker.run_worker_cycle(max_jobs=100) == 1
    assert worker.run_worker_cycle(max_jobs=100) == 0
    assert len(world.delivered) == 1
    with world.factory() as session:
        assert session.get(SendJob, first_job).status == "sent"


def test_rollback_switch_returns_registered_campaigns_to_the_legacy_path(world, monkeypatch):
    group = world.group()
    campaign_id = world.campaign(group, "A", 10, "10:00", "12:00")
    monkeypatch.setenv("OUTREACH_SHARED_DISPATCH", "false")
    with world.factory() as session:
        assert shared_dispatch.reserve_slot(session, now=NOW) is None
        assert shared_dispatch.maintain(session, now=NOW)["registered"] == 0
    assert worker.run_worker_cycle() == 1
    assert len(world.delivered) == 1
    with world.factory() as session:
        campaign = session.get(Campaign, campaign_id)
        assert aware(campaign.scheduled_at) > NOW


def test_live_api_edits_update_the_account_clock_in_the_same_commit(world, monkeypatch):
    from api.routers import campaign_delivery as delivery

    monkeypatch.setattr(delivery, "utcnow", lambda: world.clock[0])
    group = world.group()
    campaign_id = world.campaign(group, "A", 10, "10:00", "12:00", recipients=30)
    assert worker.run_worker_cycle() == 1
    world.clock[0] = NOW + timedelta(minutes=1)
    with world.factory() as session:
        result = delivery.patch_send_settings(campaign_id, delivery.SendSettingsUpdate(
            schedule={"monday": {"cap": 20, "start": "10:00", "end": "12:00"}},
        ), session, USER)
        assert result["status"] == "success"
        assert session.get(Campaign, campaign_id).status == "autopilot"
        state = session.scalar(select(SenderDispatch))
        assert state.rate == pytest.approx(20 / 7200)
        assert aware(state.next_at) == NOW + timedelta(minutes=6, seconds=30)
    assert worker.run_worker_cycle() == 0


def test_live_api_group_change_reassigns_only_unsent_work(world, monkeypatch):
    from api.routers import campaign_delivery as delivery

    monkeypatch.setattr(delivery, "utcnow", lambda: world.clock[0])
    original_group = world.group("Original")
    new_group = world.group("Replacement")
    campaign_id = world.campaign(original_group, "A", 10, "10:00", "12:00", recipients=30)
    assert worker.run_worker_cycle() == 1
    world.clock[0] += timedelta(minutes=1)
    with world.factory() as session:
        delivery.patch_campaign_sender_group(campaign_id,
            delivery.CampaignSenderGroupSelect(sender_group_id=new_group), session, USER)
        states = list(session.scalars(select(SenderDispatch).order_by(SenderDispatch.sender_id)))
        assert [state.rate for state in states] == pytest.approx([0, 10 / 7200])
        assert services.campaign_sent_today(session, campaign_id, now=world.clock[0]) == 1
    world.run_until(NOW.replace(hour=11))
    assert world.delivered[0]["sender"].startswith("Original")
    assert all(item["sender"].startswith("Replacement") for item in world.delivered[1:])
    assert len({item["recipient"] for item in world.delivered}) == len(world.delivered)
