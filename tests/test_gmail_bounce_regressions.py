from __future__ import annotations

import base64
from datetime import timedelta

import pytest
from googleapiclient.errors import HttpError
from httplib2 import Response
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from src.platform import gmail_activity as activity
from src.platform.models import Base, CampaignRecipient, GmailActivityEvent, SendLog
from src.platform.time import utcnow
from test_gmail_activity import _GmailService, _Request, _message
from test_gmail_activity import Campaign, Contact, Sender, SenderGroup, ensure_user, GMAIL_READONLY_SCOPE


def _part(mime, text, **extra):
    return {"mimeType": mime, "body": {"data": base64.urlsafe_b64encode(text.encode()).decode()}, **extra}


def _dsn(message_id="ndr", blocks=None):
    message = _message(message_id, sender="postmaster@example.test", subject="Delivery report")
    message["labelIds"] = ["Label_Undeliverable"]
    message["payload"]["mimeType"] = "multipart/report"
    message["payload"]["body"] = {}
    message["payload"]["parts"] = [_part("message/delivery-status", blocks or (
        "Final-Recipient: rfc822; target@example.test\nAction: failed\nStatus: 5.1.1\n"
    ))]
    return message


@pytest.fixture
def mailbox(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'bounce.db'}")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        ensure_user(session, "owner")
        group = SenderGroup(user_id="owner", name="Primary")
        session.add(group)
        session.flush()
        sender = Sender(
            user_id="owner", group_id=group.id, email="sender@example.test", status="connected",
            scopes=[GMAIL_READONLY_SCOPE], encrypted_oauth_credentials="encrypted",
            gmail_tracking_status="active", gmail_history_id="100", gmail_backfill_completed_at=utcnow(),
        )
        session.add(sender)
        session.flush()
        yield session, sender
    engine.dispose()


def _log(session, sender, *, message_id="outbound", hours=1, recipient="target@example.test", **kwargs):
    log = SendLog(
        user_id=sender.user_id, sender_id=sender.id, sender_email=sender.email,
        recipient_email=recipient, status="sent", sent_at=utcnow() - timedelta(hours=hours),
        gmail_message_id=message_id, **kwargs,
    )
    session.add(log)
    session.commit()
    return log


def _original(message_id, rfc_id):
    return _message(message_id, sender="sender@example.test", subject="Application", extra_headers=[("Message-ID", f"<{rfc_id}>")])


def test_mixed_dsn_only_marks_failed_recipients_and_accepts_final_4xx():
    message = _dsn(blocks=(
        "Reporting-MTA: dns; mx.example.test\n\n"
        "Final-Recipient: rfc822; failed@example.test\nAction: failed\nStatus: 4.4.7\n\n"
        "Final-Recipient: rfc822; delayed@example.test\nAction: delayed\nStatus: 4.2.0\n\n"
        "Final-Recipient: rfc822; delivered@example.test\nAction: delivered\nStatus: 2.0.0\n"
    ))
    notice = activity.parse_bounce_message(message)
    assert notice.recipients == ("failed@example.test",)


@pytest.mark.parametrize("action,status", [("delayed", "4.2.0"), ("delivered", "2.0.0"), ("relayed", "2.0.0")])
def test_nonfinal_reports_ignore_failure_text_in_returned_original(action, status):
    message = _dsn(blocks=f"Final-Recipient: rfc822; target@example.test\nAction: {action}\nStatus: {status}\n")
    message["payload"]["parts"].append({
        "mimeType": "message/rfc822",
        "parts": [_part("text/plain", "Address not found\nFinal-Recipient: rfc822; other@example.test\nAction: failed\nStatus: 5.1.1")],
    })
    assert activity.parse_bounce_message(message) is None
    assert activity.parse_response_message(message, sender_email="sender@example.test") is None


@pytest.mark.parametrize("label", ["SENT", "DRAFT"])
def test_outbound_or_draft_copies_are_never_bounces(label):
    message = _dsn()
    message["labelIds"].append(label)
    assert activity.parse_bounce_message(message) is None


@pytest.mark.parametrize("subject", ["Re: Undeliverable", "Automatic reply: Application"])
def test_human_and_auto_replies_quoting_failure_are_not_bounces(subject):
    message = _message("response", sender="target@example.test", subject=subject,
                       body="Quoted: Address not found\nFinal-Recipient: rfc822; other@example.test\nAction: failed")
    assert activity.parse_bounce_message(message) is None
    assert activity.parse_response_message(message, sender_email="sender@example.test") is not None


def test_routing_loop_is_not_reported_as_nonexistent_address():
    message = _dsn()
    message["payload"]["parts"].append(_part("text/plain", "SMTP; Hop count exceeded - possible mail loop"))
    assert "routing loop" in activity.parse_bounce_message(message).reason


def test_embedded_original_id_takes_priority_over_older_conversation_references():
    message = _dsn()
    message["payload"]["headers"].append({"name": "References", "value": "<older@example.test> <parent@example.test>"})
    message["payload"]["parts"].append(_part("text/rfc822-headers", "Message-ID: <ActualCase@example.test>\n"))
    assert activity.parse_bounce_message(message).original_message_ids == ("ActualCase@example.test",)


def test_exact_original_id_matches_old_send_without_overwriting_newer_send(mailbox):
    session, sender = mailbox
    campaign = Campaign(user_id=sender.user_id, name="Repeat application")
    contact = Contact(user_id=sender.user_id, email_normalized="target@example.test", status="sent")
    session.add_all([campaign, contact])
    session.flush()
    session.add(CampaignRecipient(campaign_id=campaign.id, contact_id=contact.id, status="sent"))
    old = _log(session, sender, message_id="old", hours=60 * 24, campaign_id=campaign.id, recipient_id=contact.id)
    new = _log(session, sender, message_id="new", campaign_id=campaign.id, recipient_id=contact.id)
    message = _dsn()
    message["payload"]["headers"].append({"name": "In-Reply-To", "value": "<old-rfc@example.test>"})
    service = _GmailService({"old": _original("old", "old-rfc@example.test"), "new": _original("new", "new-rfc@example.test")})
    first = activity._process_gmail_message(session, sender, message, service=service)
    session.commit()
    assert first["undelivered"] == 1
    assert old.status == "bounced"
    assert new.status == "sent"
    assert contact.status == "sent"
    assert session.get(CampaignRecipient, (campaign.id, contact.id)).status == "sent"
    assert activity._process_gmail_message(session, sender, message, service=service)["new_events"] == 0
    message["id"] = "second-notice-for-same-send"
    assert activity._process_gmail_message(session, sender, message, service=service)["undelivered"] == 0
    assert new.status == "sent"
    assert session.scalar(select(func.count()).select_from(GmailActivityEvent)) == 2


def test_original_id_mismatch_never_falls_back_to_latest_send(mailbox):
    session, sender = mailbox
    log = _log(session, sender)
    message = _dsn()
    message["payload"]["headers"].append({"name": "References", "value": "<unrelated@example.test>"})
    service = _GmailService({"outbound": _original("outbound", "actual@example.test")})
    result = activity._process_gmail_message(session, sender, message, service=service)
    assert result["undelivered"] == 0
    assert log.status == "sent"
    assert session.scalar(select(GmailActivityEvent)).send_log_id is None


def test_ambiguous_recipient_without_identifiers_does_not_mark_any_send(mailbox):
    session, sender = mailbox
    first = _log(session, sender, message_id="first", hours=2)
    second = _log(session, sender, message_id="second")
    result = activity._process_gmail_message(session, sender, _dsn())
    assert result["undelivered"] == 0
    assert first.status == second.status == "sent"


def test_unmatched_notice_is_reconciled_after_the_send_becomes_available(mailbox):
    session, sender = mailbox
    message = _dsn()
    assert activity._process_gmail_message(session, sender, message)["undelivered"] == 0
    session.commit()
    log = _log(session, sender)
    result = activity._process_gmail_message(session, sender, message)
    session.commit()
    assert result["undelivered"] == 1 and result["new_events"] == 0
    assert log.status == "bounced"
    assert session.scalar(select(func.count()).select_from(GmailActivityEvent)) == 1
    assert session.scalar(select(GmailActivityEvent)).send_log_id == log.id


def test_bounce_matching_is_scoped_to_account_user_and_recipient(mailbox):
    session, sender = mailbox
    other = Sender(user_id=sender.user_id, group_id=sender.group_id, email="other@example.test")
    session.add(other)
    session.flush()
    other_log = _log(session, other)
    wrong_recipient = _log(session, sender, recipient="other-recipient@example.test", gmail_thread_id="thread-ndr")
    result = activity._process_gmail_message(session, sender, _dsn())
    assert result["undelivered"] == 0
    assert other_log.status == wrong_recipient.status == "sent"


def test_manual_check_recovers_archived_old_failure_when_no_push_is_pending(mailbox, monkeypatch):
    session, sender = mailbox
    log = _log(session, sender, hours=110 * 24)
    message = _dsn()
    message["internalDate"] = str(int((utcnow() - timedelta(days=100)).timestamp() * 1000))
    message["payload"]["headers"].append({"name": "In-Reply-To", "value": "<original@example.test>"})
    service = _GmailService({"ndr": message, "outbound": _original("outbound", "original@example.test")})
    monkeypatch.setenv("GMAIL_PUBSUB_TOPIC", "projects/test/topics/gmail-events")
    monkeypatch.setattr(activity, "gmail_service_for_sender", lambda *_: service)
    result = activity.sync_selected_gmail_activity(session, sender_ids=[sender.id])
    assert result["errors"] == 0
    assert result["undelivered"] == 1
    assert log.status == "bounced"
    assert service.watch_calls[0]["body"] == {"topicName": "projects/test/topics/gmail-events"}
    bounce_query = service._messages.list_kwargs[0]
    assert "in:anywhere" in bounce_query["q"] and "after:" in bounce_query["q"]
    assert "newer_than:45d" not in bounce_query["q"]
    assert bounce_query["includeSpamTrash"] is True
    assert activity.sync_selected_gmail_activity(session, sender_ids=[sender.id])["undelivered"] == 0


def test_archived_bounce_is_processed_from_whole_mailbox_history(mailbox):
    session, sender = mailbox
    log = _log(session, sender)
    service = _GmailService({"ndr": _dsn()}, history_response={
        "historyId": "101", "history": [{"messagesAdded": [{"message": {"id": "ndr"}}]}],
    })
    result = activity.process_sender_gmail_history(session, sender, service=service)
    assert result["undelivered"] == 1 and log.status == "bounced"
    assert "labelId" not in service.history_calls[0]
    assert sender.gmail_history_id == "101"


def test_delay_then_final_failure_only_changes_status_on_final_report(mailbox):
    session, sender = mailbox
    log = _log(session, sender)
    delayed = _dsn("delay", "Final-Recipient: rfc822; target@example.test\nAction: delayed\nStatus: 4.4.1\n")
    assert activity._process_gmail_message(session, sender, delayed)["undelivered"] == 0
    assert log.status == "sent"
    assert activity._process_gmail_message(session, sender, _dsn("final"))["undelivered"] == 1
    assert log.status == "bounced"


def test_expired_history_establishes_new_cursor_before_recovery_search(mailbox, monkeypatch):
    session, sender = mailbox
    _log(session, sender)
    sender.gmail_push_pending = 1
    session.commit()
    service = _GmailService({"ndr": _dsn()}, watch_response={
        "historyId": "110", "expiration": str(int((utcnow() + timedelta(days=7)).timestamp() * 1000)),
    })
    order = []
    history_calls = 0
    def history(**kwargs):
        nonlocal history_calls
        history_calls += 1
        order.append("history")
        if history_calls == 1:
            raise HttpError(Response({"status": "404"}), b'{"error":"History expired"}')
        assert kwargs["startHistoryId"] == "110"
        return _Request({"historyId": "120"})
    normal_watch, normal_search = service.watch, service._messages.list
    def watch(**kwargs):
        order.append("watch")
        return normal_watch(**kwargs)
    def search(**kwargs):
        order.append("search")
        return normal_search(**kwargs)
    service.list, service.watch, service._messages.list = history, watch, search
    monkeypatch.setenv("GMAIL_PUBSUB_TOPIC", "projects/test/topics/gmail-events")
    monkeypatch.setattr(activity, "gmail_service_for_sender", lambda *_: service)
    result = activity.process_pending_gmail_notifications(session)
    assert result["errors"] == 0 and result["undelivered"] == 1
    assert order.index("watch") < order.index("search")
    assert sender.gmail_history_id == "120" and sender.gmail_push_pending == 0


def test_deleted_message_does_not_stall_history_cursor(mailbox):
    session, sender = mailbox
    service = _GmailService({}, history_response={
        "historyId": "101", "history": [{"messagesAdded": [{"message": {"id": "deleted"}}]}],
    })
    def missing(**kwargs):
        raise HttpError(Response({"status": "404"}), b'{"error":"Not found"}')
    service._messages.get = missing
    activity.process_sender_gmail_history(session, sender, service=service)
    assert sender.gmail_history_id == "101"


def test_recovery_paginates_past_the_old_five_page_limit():
    service = _GmailService({})
    calls = []
    def listing(**kwargs):
        calls.append(kwargs)
        page = int(kwargs.get("pageToken") or 0)
        return _Request({"messages": [{"id": f"notice-{page}"}], **({"nextPageToken": str(page + 1)} if page < 6 else {})})
    service._messages.list = listing
    assert len(activity._gmail_message_ids(service, activity.BOUNCE_SEARCH_QUERY, max_pages=None)) == 7
    assert len(calls) == 7
