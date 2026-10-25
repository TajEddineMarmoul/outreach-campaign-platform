"""Shared Gmail connections, membership isolation, and safe schema backfill."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, inspect, select, text
from sqlalchemy.orm import Session, sessionmaker

from api.auth import get_current_user_id
from api.routers.sender_groups import router, senders_router
from src.platform.db import get_session
from src.platform.jobs import create_send_jobs_for_next_batch
from src.platform import jobs
from src.platform.models import Base, Campaign, CampaignRecipient, Contact, Sender, SenderGroup, SenderGroupMembership, SendJob, SendLog
from src.platform.services import connected_senders, ensure_user, require_group, sender_group_filter, upsert_connected_sender
from src.platform.time import utcnow

USER = "shared-owner"


@pytest.fixture
def setup(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'shared.db'}", connect_args={"check_same_thread": False})
    event.listen(engine, "connect", lambda connection, _: connection.execute("PRAGMA foreign_keys=ON"))
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        ensure_user(session, USER)
        ensure_user(session, "another-owner")
        a, b = SenderGroup(user_id=USER, name="Group 1"), SenderGroup(user_id=USER, name="Group 2")
        private = SenderGroup(user_id="another-owner", name="Private")
        session.add_all([a, b, private])
        session.flush()
        first = Sender(user_id=USER, group_id=a.id, email="first@example.test", daily_cap=2,
                       encrypted_oauth_credentials="unchanged-encrypted-session", status="connected")
        second = Sender(user_id=USER, group_id=a.id, email="second@example.test", daily_cap=20,
                        encrypted_oauth_credentials="another-encrypted-session", status="connected")
        foreign = Sender(user_id="another-owner", group_id=private.id, email="private@example.test",
                         encrypted_oauth_credentials="private-encrypted-session", status="connected")
        session.add_all([first, second, foreign])
        session.commit()
        api = FastAPI()
        api.include_router(router)
        api.include_router(senders_router)
        api.dependency_overrides[get_current_user_id] = lambda: USER
        api.dependency_overrides[get_session] = lambda: session
        with TestClient(api) as client:
            yield client, session, a, b, first, second, foreign
    engine.dispose()


def attach(client, group, sender):
    result = client.post(f"/api/sender-groups/{group.id}/senders", json={"sender_ids": [sender.id]})
    assert result.status_code == 200, result.text
    return result.json()


def groups(client):
    return {item["id"]: item for item in client.get("/api/sender-groups").json()}


def test_saved_connection_can_be_in_two_groups_without_copying_or_moving(setup):
    client, session, a, b, first, second, _ = setup
    attach(client, b, first)
    attach(client, b, first)  # Repeated attachment is idempotent.
    result = groups(client)
    assert [s["id"] for s in result[a.id]["senders"]] == [first.id, second.id]
    assert [s["id"] for s in result[b.id]["senders"]] == [first.id]
    assert result[a.id]["sender_count"] == 2 and result[b.id]["sender_count"] == 1
    assert result[b.id]["senders"][0]["group_ids"] == [a.id, b.id]
    assert session.scalar(select(func.count()).select_from(Sender).where(Sender.user_id == USER)) == 2
    assert first.encrypted_oauth_credentials == "unchanged-encrypted-session" and first.group_id == a.id
    assert second.encrypted_oauth_credentials == "another-encrypted-session"
    assert session.scalar(select(func.count()).select_from(SenderGroupMembership)) == 1
    assert "encrypted" not in client.get("/api/senders").text
    assert "credentials" not in client.get("/api/sender-groups").text
    assert len(client.get("/api/senders").json()) == 2


def test_legacy_patch_adds_membership_and_shared_account_edit_updates_both_groups(setup):
    client, _, a, b, first, second, _ = setup
    response = client.patch(f"/api/senders/{first.id}", json={"group_id": b.id, "daily_cap": 40, "display_name": "First"})
    assert response.status_code == 200
    result = groups(client)
    for group in (a, b):
        account = next(sender for sender in result[group.id]["senders"] if sender["id"] == first.id)
        assert account["daily_cap"] == 40 and account["display_name"] == "First"
    assert second.daily_cap == 20


def test_backfilled_original_membership_detaches_from_both_storage_paths(setup):
    client, session, a, b, first, _, _ = setup
    attach(client, a, first)
    attach(client, b, first)
    assert len(groups(client)[a.id]["senders"]) == 2
    assert client.delete(f"/api/sender-groups/{a.id}/senders/{first.id}").status_code == 200
    assert first.group_id is None
    assert [group.id for group in first.groups] == [b.id]
    assert len(groups(client)[a.id]["senders"]) == 1
    assert session.scalar(select(func.count()).select_from(SenderGroupMembership)) == 1
    assert first.encrypted_oauth_credentials == "unchanged-encrypted-session"


def test_removing_original_and_last_group_keeps_saved_credentials_and_history(setup):
    client, session, a, b, first, _, _ = setup
    attach(client, b, first)
    session.add(SendLog(user_id=USER, sender_id=first.id, recipient_email="sent@example.test", status="sent", sent_at=utcnow()))
    session.commit()
    result = client.delete(f"/api/sender-groups/{a.id}/senders/{first.id}")
    assert result.status_code == 200
    assert first.group_id is None and first.status == "connected"
    assert groups(client)[b.id]["senders"][0]["id"] == first.id
    assert groups(client)[b.id]["senders"][0]["sent_today"] == 1
    assert client.delete(f"/api/sender-groups/{b.id}/senders/{first.id}").status_code == 200
    saved = next(sender for sender in client.get("/api/senders").json() if sender["id"] == first.id)
    assert saved["group_ids"] == [] and saved["sent_today"] == 1
    assert first.encrypted_oauth_credentials == "unchanged-encrypted-session"
    assert session.scalar(select(func.count()).select_from(SendLog)) == 1
    attach(client, a, first)
    assert groups(client)[a.id]["senders"][0]["status"] == "connected"


def delivery(session, group, sender, status="queued"):
    campaign = Campaign(user_id=USER, selected_sender_group_id=group.id, name=f"Campaign {group.id}", status="sending",
                        subject_template="Hello", body_template="Hi", send_settings={"mode": "send_now"})
    session.add(campaign)
    session.flush()
    contact = Contact(user_id=USER, email_normalized=f"recipient-{campaign.id}@example.test", status="approved")
    session.add(contact)
    session.flush()
    recipient = CampaignRecipient(campaign_id=campaign.id, contact_id=contact.id, status="queued")
    job = SendJob(user_id=USER, campaign_id=campaign.id, recipient_id=contact.id, sender_id=sender.id, status=status,
                  scheduled_for=utcnow(), batch_id=str(campaign.id), idempotency_key=str(campaign.id))
    session.add_all([recipient, job])
    session.commit()
    return campaign, recipient, job


@pytest.mark.parametrize("operation", ["detach", "delete_group"])
def test_removal_cancels_only_unstarted_work_in_the_affected_group(setup, operation):
    client, session, a, b, first, second, _ = setup
    attach(client, b, first)
    affected, recipient, pending = delivery(session, a, first)
    other_campaign, other_recipient, other_job = delivery(session, b, first)
    _, running_recipient, running = delivery(session, a, first, "running")
    _, retry_recipient, retry = delivery(session, a, first, "retry")
    _, second_recipient, second_job = delivery(session, a, second)
    pending_id, retry_id = pending.id, retry.id
    url = f"/api/sender-groups/{a.id}" + (f"/senders/{first.id}" if operation == "detach" else "")
    assert client.delete(url).status_code == 200
    assert session.get(SendJob, pending_id) is None and session.get(SendJob, retry_id) is None
    assert recipient.status == retry_recipient.status == "approved"
    assert session.get(SendJob, other_job.id).status == "queued" and other_recipient.status == "queued"
    assert session.get(SendJob, running.id).status == "running" and running_recipient.status == "queued"
    assert first.encrypted_oauth_credentials == "unchanged-encrypted-session" and first.status == "connected"
    assert other_campaign.status == "sending" and other_campaign.selected_sender_group_id == b.id
    if operation == "delete_group":
        assert affected.status == "paused" and affected.selected_sender_group_id is None
        assert session.get(SendJob, second_job.id) is None and second_recipient.status == "approved"
        assert second.encrypted_oauth_credentials == "another-encrypted-session" and second.group_id is None
    else:
        assert session.get(SendJob, second_job.id).status == "queued" and second_recipient.status == "queued"


def test_memberships_are_tenant_scoped_and_batch_is_atomic(setup):
    client, session, a, b, first, _, foreign = setup
    response = client.post(f"/api/sender-groups/{b.id}/senders", json={"sender_ids": [first.id, foreign.id]})
    assert response.status_code == 404
    assert groups(client)[b.id]["senders"] == []
    assert client.post(f"/api/sender-groups/{foreign.group_id}/senders", json={"sender_ids": [first.id]}).status_code == 404
    assert client.delete(f"/api/sender-groups/{a.id}/senders/{foreign.id}").status_code == 404
    assert client.patch(f"/api/senders/{foreign.id}", json={"daily_cap": 500}).status_code == 404
    assert session.scalar(select(func.count()).select_from(SenderGroupMembership)) == 0
    assert foreign.encrypted_oauth_credentials == "private-encrypted-session"


def test_oauth_reconnect_updates_one_saved_connection_and_preserves_other_groups(setup):
    client, session, a, b, first, second, _ = setup
    attach(client, b, first)
    updated = upsert_connected_sender(session, user_id=USER, group_id=b.id, email=first.email,
                                      display_name="Reconnected", encrypted_credentials="refreshed-encrypted-session", scopes=[])
    session.commit()
    assert updated.id == first.id and updated.group_id == a.id
    assert {group.id for group in updated.groups} == {b.id}
    assert {sender.id for sender in connected_senders(require_group(session, USER, a.id))} == {first.id, second.id}
    assert [sender.id for sender in connected_senders(require_group(session, USER, b.id))] == [first.id]
    assert session.scalar(select(Sender).where(Sender.user_id == USER, sender_group_filter(b.id))).id == first.id
    assert second.encrypted_oauth_credentials == "another-encrypted-session"


def test_shared_groups_queue_against_the_same_account_quota(setup):
    client, session, a, b, first, second, _ = setup
    second.status = "error"
    attach(client, b, first)
    campaigns = []
    for group in (a, b, a):
        campaign, recipient, job = delivery(session, group, first)
        session.delete(job)
        recipient.status = "approved"
        session.commit()
        campaigns.append(campaign)
    for campaign in campaigns[:2]:
        result = create_send_jobs_for_next_batch(session, user_id=USER, campaign_id=campaign.id)
        assert result["created"] == 1, result
        session.commit()
    result = create_send_jobs_for_next_batch(session, user_id=USER, campaign_id=campaigns[2].id)
    assert result["created"] == 0, result
    assert session.scalar(select(func.count()).select_from(SendJob)) == first.daily_cap == 2
    assert {job.sender_id for job in session.scalars(select(SendJob))} == {first.id}


def test_explicit_global_disconnect_only_removes_that_account_everywhere(setup):
    client, _, a, b, first, second, _ = setup
    attach(client, b, first)
    assert client.delete(f"/api/senders/{first.id}").status_code == 200
    result = groups(client)
    assert result[b.id]["senders"] == []
    assert [sender["id"] for sender in result[a.id]["senders"]] == [second.id]
    assert first.encrypted_oauth_credentials is None and first.status == "removed"
    assert second.encrypted_oauth_credentials == "another-encrypted-session"


def test_claimed_job_cannot_send_from_an_account_removed_from_its_group(setup, monkeypatch):
    client, session, a, b, first, _, _ = setup
    attach(client, b, first)
    campaign, recipient, job = delivery(session, a, first, "running")
    job_id = job.id
    assert client.delete(f"/api/sender-groups/{a.id}/senders/{first.id}").status_code == 200
    monkeypatch.setattr(jobs, "SessionLocal", sessionmaker(bind=session.bind, expire_on_commit=False))
    calls = []
    monkeypatch.setattr(jobs, "gmail_service_for_sender", lambda *args: calls.append("unexpected Gmail call"))
    result = jobs.perform_send_job(job_id, claimed=True)
    assert result["reason_code"] == "sender_group_changed"
    session.expire_all()
    assert session.get(SendJob, job_id) is None and recipient.status == "approved"
    assert calls == [] and first.encrypted_oauth_credentials == "unchanged-encrypted-session"


def test_delivery_from_secondary_group_uses_the_same_account_and_sent_count(setup, monkeypatch):
    client, session, a, b, first, _, _ = setup
    attach(client, b, first)
    campaign, recipient, job = delivery(session, b, first)
    campaign.engagement_tracking_enabled = False
    session.commit()
    monkeypatch.setattr(jobs, "SessionLocal", sessionmaker(bind=session.bind, expire_on_commit=False))
    captured = []
    monkeypatch.setattr(jobs, "gmail_service_for_sender", lambda db, sender: captured.append(sender.id))
    monkeypatch.setattr(jobs, "send_email", lambda **kwargs: SimpleNamespace(message_id="fake-gmail-id", thread_id="fake-thread"))
    assert jobs.perform_send_job(job.id)["status"] == "sent"
    session.expire_all()
    assert captured == [first.id] and recipient.status == "sent"
    result = groups(client)
    for group in (a, b):
        sender = next(sender for sender in result[group.id]["senders"] if sender["id"] == first.id)
        assert sender["sent_today"] == 1 and sender["daily_cap_remaining"] == 1
    assert session.scalar(select(func.count()).select_from(SendLog)) == 1


def test_migration_backfills_groups_without_changing_saved_tokens_or_history(tmp_path):
    spec = importlib.util.spec_from_file_location("shared_migration", Path(__file__).resolve().parents[1] / "alembic/versions/0016_shared_sender_groups.py")
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine(f"sqlite:///{tmp_path / 'old-schema.db'}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE sender_groups (id INTEGER PRIMARY KEY)"))
        connection.execute(text("CREATE TABLE senders (id INTEGER PRIMARY KEY, group_id INTEGER NOT NULL REFERENCES sender_groups(id), encrypted_oauth_credentials TEXT)"))
        connection.execute(text("CREATE TABLE history (sender_id INTEGER, status TEXT)"))
        connection.execute(text("INSERT INTO sender_groups VALUES (1), (2)"))
        connection.execute(text("INSERT INTO senders VALUES (11, 1, 'encrypted-session-one'), (22, 2, 'encrypted-session-two')"))
        connection.execute(text("INSERT INTO history VALUES (11, 'sent')"))
        before = list(connection.execute(text("SELECT * FROM senders ORDER BY id")))
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        assert list(connection.execute(text("SELECT * FROM senders ORDER BY id"))) == before
        assert list(connection.execute(text("SELECT * FROM sender_group_memberships ORDER BY sender_id"))) == [(1, 11), (2, 22)]
        assert next(column for column in inspect(connection).get_columns("senders") if column["name"] == "group_id")["nullable"]
        connection.execute(text("UPDATE senders SET group_id=NULL WHERE id=11"))
        migration.downgrade()
        assert connection.scalar(text("SELECT encrypted_oauth_credentials FROM senders WHERE id=11")) == "encrypted-session-one"
        assert list(connection.execute(text("SELECT * FROM history"))) == [(11, "sent")]
    engine.dispose()
