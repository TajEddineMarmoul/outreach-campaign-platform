"""Opt-in PostgreSQL checks in a newly created, isolated schema; no email calls."""
import importlib.util
import os
from pathlib import Path
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, select, text
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateSchema, DropSchema

from src.platform.db import engine
from src.platform import shared_dispatch, services
from src.platform.models import AutopilotDaySchedule, Base, Campaign, Contact, CampaignRecipient, Sender, SenderCampaignLane, SenderDispatch, SenderGroup
from src.platform.time import utcnow

pytestmark = pytest.mark.skipif(os.getenv("OUTREACH_TEST_POSTGRES") != "1", reason="Requires explicit opt-in to isolated PostgreSQL schema tests")


@pytest.fixture
def postgres_schema():
    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL connection is not configured")
    schema = "codex_dispatch_test_" + uuid4().hex[:12]
    assert re.fullmatch(r"codex_dispatch_test_[a-f0-9]{12}", schema)
    with engine.begin() as connection:
        connection.execute(CreateSchema(schema))
    isolated = engine.execution_options(schema_translate_map={None: schema})
    try:
        yield schema, isolated
    finally:
        assert re.fullmatch(r"codex_dispatch_test_[a-f0-9]{12}", schema)
        with engine.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))


def seed(isolated):
    Base.metadata.create_all(isolated)
    now = utcnow()
    with Session(isolated, expire_on_commit=False) as session:
        services.ensure_user(session, "pg-shared-test")
        group = SenderGroup(user_id="pg-shared-test", name="Test")
        session.add(group)
        session.flush()
        sender = Sender(user_id="pg-shared-test", group_id=group.id, email="test@example.test",
                        status="connected", encrypted_oauth_credentials="test-only", daily_cap=100)
        session.add(sender)
        session.commit()
        campaign = Campaign(user_id="pg-shared-test", name="Test", status="draft", timezone="UTC",
                            selected_sender_group_id=group.id, send_settings={"mode": "autopilot", "pacing_mode": "spread_evenly"})
        session.add(campaign)
        session.flush()
        for day in services.WEEKDAY_NAMES:
            session.add(AutopilotDaySchedule(campaign_id=campaign.id, day_of_week=day, daily_cap=10, start_time="00:00", end_time="23:59"))
        campaign.status = "autopilot"
        session.commit()
        return sender.id, campaign.id, now


def test_postgres_parallel_workers_reserve_one_sender_once(postgres_schema):
    _, isolated = postgres_schema
    sender_id, campaign_id, now = seed(isolated)
    barrier = threading.Barrier(2)

    def reserve():
        with Session(isolated, expire_on_commit=False) as session:
            barrier.wait(timeout=20)
            return shared_dispatch.reserve_slot(session, now=utcnow())

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: reserve(), range(2)))
    successful = [result for result in results if result is not None]
    assert len(successful) == 1
    assert successful[0][:2] == (sender_id, campaign_id)


def test_postgres_skip_locked_sender_does_not_block_other_accounts(postgres_schema):
    _, isolated = postgres_schema
    sender_id, _, _ = seed(isolated)
    with Session(isolated) as session:
        sender = session.get(Sender, sender_id)
        second = Sender(user_id=sender.user_id, group_id=sender.group_id, email="second@example.test",
                        status="connected", encrypted_oauth_credentials="test-only", daily_cap=100)
        session.add(second)
        session.commit()
        shared_dispatch.maintain(session, now=utcnow())
        second_id = second.id
    with Session(isolated) as owner, Session(isolated) as contender:
        owner.scalar(select(SenderDispatch).where(SenderDispatch.sender_id == sender_id).with_for_update())
        reservation = shared_dispatch.reserve_slot(contender, now=utcnow())
        assert reservation is not None and reservation[0] == second_id
        owner.rollback()
        reservation = shared_dispatch.reserve_slot(contender, now=utcnow())
        assert reservation is not None and reservation[0] == sender_id


def test_postgres_boundary_updates_extract_different_rates_from_json(postgres_schema):
    _, isolated = postgres_schema
    sender_id, campaign_id, _ = seed(isolated)
    with Session(isolated, expire_on_commit=False) as session:
        original = session.get(Campaign, campaign_id)
        other = Campaign(user_id=original.user_id, name="Different target", status="draft", timezone="UTC",
                         selected_sender_group_id=original.selected_sender_group_id,
                         send_settings=dict(original.send_settings))
        session.add(other)
        session.flush()
        for day in services.WEEKDAY_NAMES:
            session.add(AutopilotDaySchedule(campaign_id=other.id, day_of_week=day, daily_cap=12,
                                            start_time="00:00", end_time="23:59"))
        other.status = "autopilot"
        session.commit()
        now = utcnow()
        state = session.get(SenderDispatch, sender_id)
        state.rate, state.active_lanes = 0, 0
        for lane in session.scalars(select(SenderCampaignLane)):
            lane.active = lane.selectable = False
            lane.transition_at = now
        session.commit()
        result = shared_dispatch.maintain(session, limit=1, now=now)
        assert result["boundaries"] == 1 and result["lanes"] == 2
        session.expire_all()
        duration = 23 * 3600 + 59 * 60
        assert session.get(SenderDispatch, sender_id).rate == pytest.approx(22 / duration)
        assert list(session.scalars(select(SenderCampaignLane.rate).order_by(SenderCampaignLane.campaign_id))) == pytest.approx([10 / duration, 12 / duration])


def test_postgres_migration_up_and_down_in_isolated_0017_shape(postgres_schema):
    schema, _ = postgres_schema
    path = Path(__file__).resolve().parents[1] / "alembic/versions/0018_shared_sender_dispatch.py"
    spec = importlib.util.spec_from_file_location("shared_dispatch_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with engine.begin() as connection:
        # SET LOCAL is transaction-scoped. Unqualified Alembic DDL is confined
        # to the exact fresh test schema, including on a transaction pooler.
        quoted = connection.dialect.identifier_preparer.quote_schema(schema)
        connection.execute(text("SET LOCAL search_path TO " + quoted + ", pg_catalog"))
        assert connection.execute(text("SELECT current_schema()")).scalar() == schema
        connection.execute(text("CREATE TABLE users (id VARCHAR(255) PRIMARY KEY)"))
        connection.execute(text("CREATE TABLE senders (id INTEGER PRIMARY KEY)"))
        connection.execute(text("CREATE TABLE campaigns (id INTEGER PRIMARY KEY, status VARCHAR(40), scheduled_at TIMESTAMPTZ)"))
        connection.execute(text("CREATE TABLE send_jobs (id INTEGER PRIMARY KEY, sender_id INTEGER, campaign_id INTEGER, recipient_id INTEGER, status VARCHAR(40), scheduled_for TIMESTAMPTZ)"))
        connection.execute(text("CREATE TABLE send_log (sender_id INTEGER, campaign_id INTEGER, sent_at TIMESTAMPTZ, status VARCHAR(40))"))
        connection.execute(text("CREATE TABLE campaign_recipients (campaign_id INTEGER, contact_id INTEGER, status VARCHAR(40), created_at TIMESTAMPTZ)"))
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        inspector = inspect(connection)
        assert "shared_dispatch" in {item["name"] for item in inspector.get_columns("campaigns", schema=schema)}
        assert "sender_dispatch" in inspector.get_table_names(schema=schema)
        assert "ix_sender_lanes_fair" in {item["name"] for item in inspector.get_indexes("sender_campaign_lanes", schema=schema)}
        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
        assert "sender_dispatch" not in inspect(connection).get_table_names(schema=schema)
        assert "shared_dispatch" not in {item["name"] for item in inspect(connection).get_columns("campaigns", schema=schema)}
