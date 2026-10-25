from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session

from api.routers import campaign_workspace
from src.platform.campaign_capacity import _share_capacity, daily_campaign_capacity
from src.platform.models import Base, Campaign, CampaignRecipient, Contact, Sender, SenderGroup, SendJob, SendLog, AutopilotDaySchedule, UserSettings
from src.platform.services import ensure_user


NOW = datetime(2026, 10, 26, 10, tzinfo=timezone.utc)
USER = "capacity-user"


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'capacity.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        ensure_user(session, USER)
        session.get(UserSettings, USER).timezone = "UTC"
        yield session
    engine.dispose()


def group(session, cap=40, name="Shared", user=USER):
    value = SenderGroup(user_id=user, name=name)
    session.add(value)
    session.flush()
    sender = Sender(user_id=user, group_id=value.id, email=f"{name.lower()}@example.test",
                    status="connected", encrypted_oauth_credentials="test-only", daily_cap=cap)
    session.add(sender)
    session.flush()
    return value, sender


def campaign(session, selected, count=40, cap=40, status="autopilot", zone="UTC", user=USER, fields=None):
    value = Campaign(user_id=user, selected_sender_group_id=selected.id, name="Daily capacity", status=status,
                     timezone=zone, subject_template="Hi {{first_name}}", body_template="Hello",
                     send_settings={"mode": "autopilot", "pacing_mode": "spread_evenly"})
    session.add(value)
    session.flush()
    for day in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"):
        session.add(AutopilotDaySchedule(campaign_id=value.id, day_of_week=day, daily_cap=cap, start_time="00:00", end_time="23:59"))
    for index in range(count):
        contact = Contact(user_id=user, email_normalized=f"campaign{value.id}-{index}@example.test", status="approved",
                          custom_fields=fields if fields is not None else {"first_name": "Alex"})
        session.add(contact)
        session.flush()
        session.add(CampaignRecipient(campaign_id=value.id, contact_id=contact.id, status="approved"))
    session.commit()
    return value


def sent(session, value, sender, count, at=NOW, status="sent"):
    for index in range(count):
        session.add(SendLog(user_id=value.user_id, campaign_id=value.id, sender_id=sender.id,
                            recipient_email=f"sent-{index}@example.test", status=status, sent_at=at))
    session.commit()


def items(session, now=NOW):
    return {item["campaign_id"]: item for item in daily_campaign_capacity(session, USER, now=now)["items"]}


def test_example_includes_sent_in_estimate_and_accounts_for_other_campaign_demand(session):
    shared, sender = group(session)
    first = campaign(session, shared)
    other = campaign(session, shared, count=30, cap=30)
    sent(session, first, sender, 5)
    result = items(session)
    assert result[first.id]["sent_today"] == 5
    assert result[first.id]["estimated_total_today"] == 23
    assert result[other.id]["estimated_total_today"] == 17
    assert sum(item["estimated_total_today"] for item in result.values()) == 40
    assert result[first.id]["daily_limit"] == 40
    assert result[first.id]["competing_campaigns"] == 1


def test_small_campaign_uses_its_demand_then_gives_unused_share_to_others(session):
    shared, sender = group(session)
    first = campaign(session, shared, count=5)
    other = campaign(session, shared, count=30, cap=30)
    sent(session, first, sender, 5)
    result = items(session)
    assert result[first.id]["estimated_total_today"] == 10
    assert result[other.id]["estimated_total_today"] == 30


def test_campaign_and_account_caps_both_bound_estimate(session):
    shared, sender = group(session, cap=10)
    first = campaign(session, shared, cap=40)
    result = items(session)[first.id]
    assert result["estimated_total_today"] == result["daily_limit"] == 10
    assert result["campaign_limit"] == 40 and result["sender_limit"] == 10
    sender.daily_cap = 100
    session.commit()
    assert items(session)[first.id]["estimated_total_today"] == 40


@pytest.mark.parametrize("status", ["paused", "draft", "ended", "stopped"])
def test_inactive_campaign_does_not_compete_for_new_slots(session, status):
    shared, sender = group(session)
    inactive = campaign(session, shared, status=status)
    active = campaign(session, shared)
    sent(session, inactive, sender, 5)
    result = items(session)
    assert result[inactive.id]["estimated_total_today"] == 5
    assert result[active.id]["estimated_total_today"] == 35


def test_existing_reservations_are_prioritized_and_not_double_counted(session):
    shared, sender = group(session, cap=4)
    first = campaign(session, shared, count=4)
    second = campaign(session, shared, count=4)
    recipient = session.scalar(select(CampaignRecipient).where(CampaignRecipient.campaign_id == first.id))
    recipient.status = "queued"
    session.add(SendJob(user_id=USER, campaign_id=first.id, recipient_id=recipient.contact_id,
                        sender_id=sender.id, status="queued", scheduled_for=NOW, batch_id="reserved", idempotency_key="reserved"))
    session.commit()
    result = items(session)
    assert result[first.id]["estimated_total_today"] == 3
    assert result[second.id]["estimated_total_today"] == 1
    assert session.scalar(select(func.count()).select_from(SendJob)) == 1
    assert recipient.status == "queued"


def test_separate_sender_groups_have_independent_capacity(session):
    a, _ = group(session, name="A", cap=10)
    b, _ = group(session, name="B", cap=20)
    first, second = campaign(session, a), campaign(session, b)
    result = items(session)
    assert result[first.id]["estimated_total_today"] == 10
    assert result[second.id]["estimated_total_today"] == 20


def test_missing_personalization_and_dry_runs_do_not_consume_projected_capacity(session):
    shared, _ = group(session)
    invalid = campaign(session, shared, fields={})
    dry = campaign(session, shared)
    dry.send_settings = {**dry.send_settings, "dry_run": True}
    valid = campaign(session, shared)
    result = items(session)
    assert result[invalid.id]["estimated_total_today"] == 0
    assert result[dry.id]["estimated_total_today"] == 0
    assert result[valid.id]["estimated_total_today"] == 40


def test_campaign_today_uses_its_timezone_and_bounces_count_but_tests_and_errors_do_not(session):
    shared, sender = group(session)
    value = campaign(session, shared, zone="Asia/Dubai")
    boundary = datetime(2026, 10, 25, 20, tzinfo=timezone.utc)  # Dubai Monday midnight.
    sent(session, value, sender, 2, at=boundary - timedelta(seconds=1))
    sent(session, value, sender, 3, at=boundary, status="bounced")
    sent(session, value, sender, 4, at=boundary, status="test_sent")
    sent(session, value, sender, 5, at=boundary, status="failed")
    result = items(session, now=boundary + timedelta(minutes=30))[value.id]
    assert result["date"] == "2026-10-26"
    assert result["sent_today"] == 3
    assert result["estimated_total_today"] <= 36  # Four tests already used campaign quota.


def test_future_and_closed_windows_do_not_claim_today_capacity(session):
    shared, _ = group(session)
    later = campaign(session, shared)
    later.scheduled_at = NOW + timedelta(days=1)
    closed = campaign(session, shared)
    day = session.scalar(select(AutopilotDaySchedule).where(AutopilotDaySchedule.campaign_id == closed.id, AutopilotDaySchedule.day_of_week == "monday"))
    day.end_time = "09:00"
    session.commit()
    result = items(session)
    assert result[later.id]["estimated_total_today"] == 0
    assert result[closed.id]["estimated_total_today"] == 0


def test_account_reset_during_campaign_day_does_not_project_beyond_displayed_limit(session):
    shared, sender = group(session, cap=10)
    value = campaign(session, shared, zone="Asia/Dubai")
    sent(session, value, sender, 5, at=datetime(2026, 10, 25, 21, tzinfo=timezone.utc))
    result = items(session)[value.id]
    assert result["sent_today"] == 5
    assert result["estimated_total_today"] == result["daily_limit"] == 10


def test_capacity_endpoint_is_tenant_scoped_read_only_and_route_is_not_a_campaign_id(session):
    shared, _ = group(session)
    mine = campaign(session, shared)
    ensure_user(session, "other-user")
    other_group, _ = group(session, name="Other", user="other-user")
    campaign(session, other_group, user="other-user")
    app = FastAPI()
    app.include_router(campaign_workspace.router)
    app.dependency_overrides[campaign_workspace.get_session] = lambda: session
    app.dependency_overrides[campaign_workspace.get_current_user_id] = lambda: USER
    writes = []
    def on_execute(_conn, _cursor, statement, *_args):
        if statement.lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE"}:
            writes.append(statement)
    event.listen(session.get_bind(), "before_cursor_execute", on_execute)
    response = TestClient(app).get("/api/campaigns/daily-capacity")
    assert response.status_code == 200
    assert [row["campaign_id"] for row in response.json()["items"]] == [mine.id]
    assert writes == []


def test_water_filling_never_exceeds_budget_or_demand():
    for capacity in (0, 1, 5, 40, 100, 1000000):
        demand = {1: 5, 2: 30, 3: 40}
        allocation = _share_capacity(demand, capacity)
        assert sum(allocation.values()) == min(capacity, sum(demand.values()))
        assert all(0 <= allocation[key] <= demand[key] for key in demand)
