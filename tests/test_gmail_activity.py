from __future__ import annotations

import base64
import importlib.util
import json
from datetime import timedelta
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, func, inspect, select
from sqlalchemy.orm import Session

from api.routers.campaign_delivery import get_campaign_send_progress
from api.routers import gmail_push
from src.gmail_sender import GMAIL_READONLY_SCOPE
from src.platform.gmail_activity import (
    enqueue_gmail_push_notification,
    parse_bounce_message,
    parse_response_message,
    process_sender_gmail_history,
    register_sender_gmail_watch,
    sync_sender_gmail_activity,
)
from src.platform.models import (
    Base,
    Campaign,
    CampaignRecipient,
    Contact,
    GmailActivityEvent,
    Sender,
    SenderGroup,
    SendLog,
)
from src.platform.services import ensure_user
from src.platform.time import utcnow


def _message(
    message_id: str,
    *,
    sender: str,
    subject: str,
    body: str = "Hello",
    thread_id: str | None = None,
    extra_headers: list[tuple[str, str]] | None = None,
) -> dict:
    headers = [{"name": "From", "value": sender}, {"name": "Subject", "value": subject}]
    headers.extend({"name": name, "value": value} for name, value in (extra_headers or []))
    return {
        "id": message_id,
        "threadId": thread_id or f"thread-{message_id}",
        "internalDate": str(int(utcnow().timestamp() * 1000)),
        "labelIds": ["INBOX"],
        "payload": {
            "mimeType": "text/plain",
            "headers": headers,
            "body": {"data": base64.urlsafe_b64encode(body.encode()).decode().rstrip("=")},
        },
    }


class _Request:
    def __init__(self, result):
        self.result = result

    def execute(self):
        return self.result


class _Messages:
    def __init__(self, messages: dict[str, dict]):
        self._messages = messages
        self.list_calls = 0
        self.list_kwargs: list[dict] = []
        self.get_calls: list[str] = []

    def list(self, **_kwargs):
        self.list_calls += 1
        self.list_kwargs.append(_kwargs)
        return _Request({"messages": [{"id": message_id} for message_id in self._messages]})

    def get(self, *, id: str, **_kwargs):
        self.get_calls.append(id)
        return _Request(self._messages[id])


class _GmailService:
    def __init__(
        self,
        messages: dict[str, dict],
        *,
        history_response: dict | None = None,
        watch_response: dict | None = None,
    ):
        self._messages = _Messages(messages)
        self._history_response = history_response or {"historyId": "1"}
        self._watch_response = watch_response or {
            "historyId": "1",
            "expiration": str(int((utcnow() + timedelta(days=7)).timestamp() * 1000)),
        }
        self.history_calls: list[dict] = []
        self.watch_calls: list[dict] = []

    def users(self):
        return self

    def messages(self):
        return self._messages

    def history(self):
        return self

    def list(self, **kwargs):
        self.history_calls.append(kwargs)
        return _Request(self._history_response)

    def watch(self, **kwargs):
        self.watch_calls.append(kwargs)
        return _Request(self._watch_response)


def test_gmail_message_classification_separates_replies_automation_and_bounces():
    human = _message("human", sender="Person <person@example.test>", subject="Re: Hello")
    automated = _message(
        "auto",
        sender="Person <person@example.test>",
        subject="Automatic reply: Hello",
        extra_headers=[("Auto-Submitted", "auto-generated")],
    )
    bounce = _message(
        "bounce",
        sender="Mail Delivery Subsystem <mailer-daemon@googlemail.com>",
        subject="Delivery Status Notification (Failure)",
        body="Final-Recipient: rfc822; bad@example.test\nAction: failed\nStatus: 5.1.3\nAddress not found",
        extra_headers=[("X-Failed-Recipients", "bad@example.test")],
    )

    assert parse_response_message(human, sender_email="sender@example.test").event_type == "replied"
    assert parse_response_message(automated, sender_email="sender@example.test").event_type == "automated_response"
    assert parse_response_message(bounce, sender_email="sender@example.test") is None
    notice = parse_bounce_message(bounce)
    assert notice and notice.recipients == ("bad@example.test",)
    assert "not found" in notice.reason.lower()


def test_sync_matches_each_gmail_outcome_to_the_sent_recipient(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'gmail-activity.db'}")
    Base.metadata.create_all(engine)
    now = utcnow()
    with Session(engine) as session:
        ensure_user(session, "user-1")
        group = SenderGroup(user_id="user-1", name="Primary")
        campaign = Campaign(user_id="user-1", name="Outreach")
        session.add_all([group, campaign])
        session.flush()
        campaign.selected_sender_group_id = group.id
        sender = Sender(
            user_id="user-1",
            group_id=group.id,
            email="sender@example.test",
            encrypted_oauth_credentials="encrypted",
            scopes=[GMAIL_READONLY_SCOPE],
            gmail_tracking_status="active",
        )
        session.add(sender)
        session.flush()

        logs: dict[str, SendLog] = {}
        for email in ("person@example.test", "auto@example.test", "bad@example.test"):
            contact = Contact(user_id="user-1", email_normalized=email, status="sent")
            session.add(contact)
            session.flush()
            session.add(CampaignRecipient(campaign_id=campaign.id, contact_id=contact.id, status="sent"))
            log = SendLog(
                user_id="user-1",
                campaign_id=campaign.id,
                recipient_id=contact.id,
                sender_id=sender.id,
                recipient_email=email,
                sender_email=sender.email,
                subject="Hello",
                status="sent",
                sent_at=now - timedelta(hours=1),
                gmail_thread_id=f"thread-{email}",
            )
            session.add(log)
            logs[email] = log
        session.commit()

        messages = {
            "human": _message(
                "human", sender="Person <person@example.test>", subject="Re: Hello", thread_id="thread-person@example.test",
            ),
            "auto": _message(
                "auto", sender="auto@example.test", subject="Out of Office: Hello", thread_id="thread-auto@example.test",
                extra_headers=[("Auto-Submitted", "auto-generated")],
            ),
            "bounce": _message(
                "bounce", sender="Mail Delivery Subsystem <mailer-daemon@googlemail.com>",
                subject="Delivery Status Notification (Failure)",
                body="Final-Recipient: rfc822; bad@example.test\nAction: failed\nStatus: 5.1.3\nAddress not found",
                extra_headers=[("X-Failed-Recipients", "bad@example.test")],
            ),
            "unrelated": _message("unrelated", sender="news@example.test", subject="Newsletter"),
        }
        result = sync_sender_gmail_activity(session, sender, service=_GmailService(messages))
        session.commit()

        assert result["replied"] == 1
        assert result["automated_responses"] == 1
        assert result["undelivered"] == 1
        assert logs["person@example.test"].response_status == "replied"
        assert logs["auto@example.test"].response_status == "automated_response"
        assert logs["bad@example.test"].status == "bounced"
        assert session.get(
            CampaignRecipient,
            (campaign.id, logs["bad@example.test"].recipient_id),
        ).status == "bounced"
        assert session.scalar(
            select(func.count()).select_from(GmailActivityEvent).where(GmailActivityEvent.event_type != "ignored")
        ) == 3

        progress = get_campaign_send_progress(campaign.id, session=session, user_id="user-1")
        assert progress["replied_count"] == 1
        assert progress["automated_response_count"] == 1
        assert progress["bounced_count"] == 1
        assert progress["send_error_count"] == 0
        assert progress["gmail_tracking"]["enabled"] is True

        repeated = sync_sender_gmail_activity(session, sender, service=_GmailService(messages))
        assert repeated["new_events"] == 0

    engine.dispose()


def test_push_notification_coalesces_to_the_newest_history_cursor(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'gmail-push.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        ensure_user(session, "user-1")
        group = SenderGroup(user_id="user-1", name="Primary")
        session.add(group)
        session.flush()
        sender = Sender(
            user_id="user-1",
            group_id=group.id,
            email="sender@example.test",
            encrypted_oauth_credentials="encrypted",
            scopes=[GMAIL_READONLY_SCOPE],
            gmail_tracking_status="active",
            gmail_history_id="100",
        )
        session.add(sender)
        session.commit()

        assert enqueue_gmail_push_notification(
            session, email_address="SENDER@example.test", history_id="120",
        ) == 1
        assert enqueue_gmail_push_notification(
            session, email_address="sender@example.test", history_id="115",
        ) == 1
        assert enqueue_gmail_push_notification(
            session, email_address="sender@example.test", history_id="130",
        ) == 1
        session.commit()

        assert sender.gmail_push_pending == 1
        assert sender.gmail_pending_history_id == "130"
        assert enqueue_gmail_push_notification(
            session, email_address="sender@example.test", history_id="99",
        ) == 0

    engine.dispose()


def test_watch_and_history_use_push_cursor_instead_of_listing_mailbox(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'gmail-history.db'}")
    Base.metadata.create_all(engine)
    now = utcnow()
    monkeypatch.setenv("GMAIL_PUBSUB_TOPIC", "projects/test/topics/gmail-events")
    with Session(engine) as session:
        ensure_user(session, "user-1")
        group = SenderGroup(user_id="user-1", name="Primary")
        campaign = Campaign(user_id="user-1", name="Outreach")
        session.add_all([group, campaign])
        session.flush()
        sender = Sender(
            user_id="user-1",
            group_id=group.id,
            email="sender@example.test",
            encrypted_oauth_credentials="encrypted",
            scopes=[GMAIL_READONLY_SCOPE],
            gmail_tracking_status="pending",
        )
        contact = Contact(user_id="user-1", email_normalized="person@example.test", status="sent")
        session.add_all([sender, contact])
        session.flush()
        session.add(SendLog(
            user_id="user-1",
            campaign_id=campaign.id,
            recipient_id=contact.id,
            sender_id=sender.id,
            recipient_email=contact.email_normalized,
            sender_email=sender.email,
            subject="Hello",
            status="sent",
            sent_at=now - timedelta(hours=1),
            gmail_thread_id="reply-thread",
        ))
        session.commit()

        expiration = str(int((now + timedelta(days=7)).timestamp() * 1000))
        watch_service = _GmailService(
            {}, watch_response={"historyId": "100", "expiration": expiration},
        )
        register_sender_gmail_watch(session, sender, service=watch_service, now=now)
        assert sender.gmail_tracking_status == "active"
        assert sender.gmail_history_id == "100"
        assert watch_service.watch_calls[0]["body"] == {
            "topicName": "projects/test/topics/gmail-events",
        }
        renewal_service = _GmailService(
            {}, watch_response={"historyId": "110", "expiration": expiration},
        )
        register_sender_gmail_watch(session, sender, service=renewal_service, now=now)
        assert sender.gmail_history_id == "100"

        sender.gmail_push_pending = 1
        sender.gmail_pending_history_id = "120"
        reply = _message(
            "reply", sender="Person <person@example.test>", subject="Re: Hello", thread_id="reply-thread",
        )
        history_service = _GmailService(
            {"reply": reply},
            history_response={
                "historyId": "120",
                "history": [{"id": "110", "messagesAdded": [{"message": {"id": "reply"}}]}],
            },
        )
        result = process_sender_gmail_history(session, sender, service=history_service, now=now)
        session.commit()

        assert result["replied"] == 1
        assert history_service.history_calls[0]["startHistoryId"] == "100"
        assert "labelId" not in history_service.history_calls[0]
        assert history_service._messages.list_calls == 0
        assert history_service._messages.get_calls == ["reply"]
        assert sender.gmail_history_id == "120"
        assert sender.gmail_push_pending == 0
        assert sender.gmail_pending_history_id is None

    engine.dispose()


def test_authenticated_webhook_only_queues_the_affected_mailbox(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'gmail-webhook.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setenv("GMAIL_PUBSUB_PUSH_AUDIENCE", "https://api.example.test/api/webhooks/gmail")
    monkeypatch.setattr(gmail_push, "_verify_push_token", lambda *_args: {"email_verified": True})
    with Session(engine) as session:
        ensure_user(session, "user-1")
        group = SenderGroup(user_id="user-1", name="Primary")
        session.add(group)
        session.flush()
        affected = Sender(
            user_id="user-1",
            group_id=group.id,
            email="affected@example.test",
            encrypted_oauth_credentials="encrypted",
            scopes=[GMAIL_READONLY_SCOPE],
            gmail_tracking_status="active",
            gmail_history_id="100",
        )
        untouched = Sender(
            user_id="user-1",
            group_id=group.id,
            email="untouched@example.test",
            encrypted_oauth_credentials="encrypted",
            scopes=[GMAIL_READONLY_SCOPE],
            gmail_tracking_status="active",
            gmail_history_id="100",
        )
        session.add_all([affected, untouched])
        session.commit()

        encoded = base64.b64encode(json.dumps({
            "emailAddress": affected.email,
            "historyId": "125",
        }).encode()).decode()
        response = gmail_push.gmail_push_webhook(
            gmail_push.PubSubEnvelope(message=gmail_push.PubSubMessage(data=encoded)),
            authorization="Bearer signed-pubsub-token",
            session=session,
        )

        assert response.status_code == 204
        assert affected.gmail_push_pending == 1
        assert affected.gmail_pending_history_id == "125"
        assert untouched.gmail_push_pending == 0
        assert untouched.gmail_pending_history_id is None

    engine.dispose()


def test_gmail_activity_migration_upgrades_and_downgrades_existing_tables(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    metadata = MetaData()
    Table("users", metadata, Column("id", String(255), primary_key=True))
    Table(
        "senders",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("email", String(320)),
        Column("status", String(40)),
    )
    Table("send_log", metadata, Column("id", Integer, primary_key=True))
    metadata.create_all(engine)
    migration_path = Path(__file__).parents[1] / "alembic" / "versions" / "0013_gmail_activity_tracking.py"
    spec = importlib.util.spec_from_file_location("gmail_activity_migration", migration_path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        inspector = inspect(connection)
        assert "gmail_activity_events" in inspector.get_table_names()
        assert {column["name"] for column in inspector.get_columns("send_log")} >= {
            "bounced_at", "response_status", "responded_at", "response_gmail_message_id",
        }
        assert {column["name"] for column in inspector.get_columns("senders")} >= {
            "gmail_tracking_status", "gmail_history_id", "gmail_pending_history_id",
            "gmail_watch_expiration_at", "gmail_watch_refresh_at", "gmail_backfill_completed_at",
            "gmail_push_pending", "gmail_push_locked_at", "gmail_push_retry_at",
            "gmail_sync_checked_at", "gmail_sync_error",
        }

        migration.downgrade()
        inspector = inspect(connection)
        assert "gmail_activity_events" not in inspector.get_table_names()
        assert {column["name"] for column in inspector.get_columns("send_log")} == {"id"}

    engine.dispose()
