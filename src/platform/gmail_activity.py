from __future__ import annotations

import base64
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.parser import Parser
from email.utils import getaddresses
from html import unescape
from typing import Any, Iterable

from googleapiclient.errors import HttpError
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.gmail_sender import GMAIL_READONLY_SCOPE
from src.platform.gmail import gmail_service_for_sender
from src.platform.models import CampaignRecipient, Contact, GmailActivityEvent, Sender, SendLog
from src.platform.time import utcnow


logger = logging.getLogger("outreach.gmail_activity")

GMAIL_ACTIVITY_MATCH_LOOKBACK = timedelta(days=45)
GMAIL_WATCH_RENEW_INTERVAL = timedelta(days=1)
GMAIL_WATCH_RETRY_INTERVAL = timedelta(minutes=15)
GMAIL_PUSH_RETRY_INTERVAL = timedelta(minutes=5)
GMAIL_PUSH_LOCK_TIMEOUT = timedelta(minutes=10)
BOUNCE_SEARCH_QUERY = (
    'in:anywhere -in:sent -in:drafts -in:chats '
    '{from:mailer-daemon from:postmaster subject:undeliverable '
    'subject:"delivery failure" subject:"delivery failed" '
    'subject:"delivery status notification" "Address not found" '
    '"couldn\'t be delivered" "Message not delivered"}'
)
RESPONSE_SEARCH_QUERY = "newer_than:45d in:anywhere -in:sent -in:drafts -in:chats"
EMAIL_PATTERN = re.compile(r"[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)
AUTOMATED_SUBJECT_MARKERS = (
    "automatic reply",
    "automated reply",
    "auto reply",
    "auto-reply",
    "autoreply",
    "out of office",
    "automatisch antwoord",
    "réponse automatique",
    "reponse automatique",
    "respuesta automática",
    "respuesta automatica",
    "risposta automatica",
    "автоматический ответ",
)


@dataclass(frozen=True)
class BounceNotice:
    gmail_message_id: str
    recipients: tuple[str, ...]
    reason: str
    occurred_at: datetime
    original_message_ids: tuple[str, ...] = ()
    gmail_thread_id: str | None = None


@dataclass(frozen=True)
class ResponseNotice:
    gmail_message_id: str
    gmail_thread_id: str | None
    recipient_email: str
    event_type: str
    occurred_at: datetime


def sender_tracks_gmail_activity(sender: Sender) -> bool:
    return GMAIL_READONLY_SCOPE in set(sender.scopes or [])


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _decode_body_data(data: str | None) -> str:
    if not data:
        return ""
    try:
        padded = data + "=" * (-len(data) % 4)
        return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
    except (ValueError, UnicodeError):
        return ""


def _walk_parts(payload: dict[str, Any] | None) -> Iterable[dict[str, Any]]:
    if not payload:
        return
    yield payload
    for part in payload.get("parts") or []:
        if isinstance(part, dict):
            yield from _walk_parts(part)


def _top_header_values(payload: dict[str, Any], name: str) -> list[str]:
    return [
        str(header.get("value", ""))
        for header in payload.get("headers") or []
        if str(header.get("name", "")).lower() == name.lower()
    ]


def _header_values(payload: dict[str, Any], name: str) -> list[str]:
    values: list[str] = []
    for part in _walk_parts(payload):
        values.extend(_top_header_values(part, name))
    return values


def _message_text(payload: dict[str, Any]) -> str:
    chunks: list[str] = []
    for part in _walk_parts(payload):
        mime_type = str(part.get("mimeType") or part.get("mime_type") or "").lower()
        if not (mime_type.startswith("text/") or mime_type in {"message/delivery-status", "message/rfc822"}):
            continue
        body = part.get("body") or {}
        content = body.get("content")
        if isinstance(content, str) and content:
            chunks.append(content)
        else:
            decoded = _decode_body_data(body.get("data"))
            if decoded:
                chunks.append(decoded)
    return "\n".join(chunks)


def _report_parts(payload: dict[str, Any]) -> Iterable[dict[str, Any]]:
    """Exclude the returned original email from delivery-report classification."""
    if payload.get("mimeType", "").lower() in {"message/rfc822", "text/rfc822-headers"}:
        return
    yield payload
    for part in payload.get("parts") or []:
        if isinstance(part, dict):
            yield from _report_parts(part)


def _part_text(part: dict[str, Any]) -> str:
    body = part.get("body") or {}
    return body.get("content") or _decode_body_data(body.get("data"))


def _report_text(payload: dict[str, Any]) -> str:
    return unescape("\n".join(
        _part_text(part) for part in _report_parts(payload)
        if part.get("mimeType", "").startswith("text/")
        or part.get("mimeType") == "message/delivery-status"
    ))


def _is_delivery_report(payload: dict[str, Any]) -> bool:
    senders = getaddresses(_top_header_values(payload, "From"))
    return bool(
        any(address.lower().partition("@")[0] in {"mailer-daemon", "postmaster"} for _, address in senders)
        or _top_header_values(payload, "X-Failed-Recipients")
        or any(part.get("mimeType") == "message/delivery-status" for part in _report_parts(payload))
    )


def _message_identifiers(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(
        match.strip() for value in values
        for match in re.findall(r"<([^<>\s]+@[^<>\s]+)>", value)
    ))


def _original_message_ids(payload: dict[str, Any]) -> tuple[str, ...]:
    values: list[str] = []
    for part in _walk_parts(payload):
        values.extend(_top_header_values(part, "Original-Message-ID"))
        if part is not payload:
            values.extend(_top_header_values(part, "Message-ID"))
        if part.get("mimeType") in {"text/rfc822-headers", "message/delivery-status", "message/rfc822"}:
            values.extend(re.findall(r"(?im)^(?:Original-)?Message-ID:\s*(<[^>]+>)", _part_text(part)))
    originals = _message_identifiers(values)
    if originals:
        return originals
    reply_to = _message_identifiers(_top_header_values(payload, "In-Reply-To"))
    if reply_to:
        return reply_to
    # References may include older sends in the conversation. The last entry
    # identifies the parent; earlier entries must not redirect a bounce.
    return _message_identifiers(_top_header_values(payload, "References"))[-1:]


def _dsn_recipients(payload: dict[str, Any]) -> tuple[bool, tuple[str, ...]]:
    found = False
    failed: list[str] = []
    for part in _report_parts(payload):
        if part.get("mimeType") != "message/delivery-status":
            continue
        for block in re.split(r"\r?\n\s*\r?\n", _message_text(part)):
            fields = Parser().parsestr(block)
            recipient = fields.get("Final-Recipient") or fields.get("Original-Recipient")
            if not recipient:
                continue
            found = True
            action = str(fields.get("Action", "")).strip().lower()
            status = str(fields.get("Status", "")).strip()
            if action == "failed" or (not action and status.startswith("5.")):
                failed.append(recipient)
    return found, _dedupe_addresses(failed)


def _dedupe_addresses(values: Iterable[str]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        for match in EMAIL_PATTERN.findall(unescape(value)):
            normalized = match.strip("<>.,;:\"'()[]{} ").lower()
            if normalized and normalized not in seen:
                seen.add(normalized)
                result.append(normalized)
    return tuple(result)


def _message_time(message: dict[str, Any]) -> datetime:
    try:
        return datetime.fromtimestamp(int(str(message.get("internalDate"))) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return utcnow()


def _failed_recipients(payload: dict[str, Any], body_text: str) -> tuple[str, ...]:
    candidates = _top_header_values(payload, "X-Failed-Recipients")
    patterns = (
        r"(?:Final|Original)-Recipient:\s*(?:rfc822;)?\s*([^\s<>]+@[^\s<>]+)",
        r"wasn(?:'|’|&#39;)t delivered to\s+([^\s<>]+@[^\s<>]+)",
        r"Delivery has failed to (?:these recipients or groups|this recipient):\s*([^\s<>]+@[^\s<>]+)",
        r"message to\s+([^\s<>]+@[^\s<>]+)\s+(?:couldn(?:'|’)t|could not) be delivered",
    )
    for pattern in patterns:
        candidates.extend(re.findall(pattern, body_text, flags=re.IGNORECASE))
    return _dedupe_addresses(candidates)


def _is_permanent_failure(subject: str, payload: dict[str, Any], body_text: str) -> bool:
    lowered_subject = subject.lower()
    lowered_body = body_text.lower()
    if "delivery status notification (delay)" in lowered_subject:
        return False
    if _top_header_values(payload, "X-Failed-Recipients"):
        return True
    if any(marker in lowered_subject for marker in ("undeliverable", "delivery failure", "delivery failed")):
        return True
    return bool(
        re.search(r"\bAction:\s*failed\b", body_text, flags=re.IGNORECASE)
        or re.search(r"\bStatus:\s*5(?:\.\d+){1,2}\b", body_text, flags=re.IGNORECASE)
        or "address not found" in lowered_body
    )


def _bounce_reason(body_text: str) -> str:
    lowered = re.sub(r"\s+", " ", unescape(body_text)).lower()
    if any(marker in lowered for marker in (
        "address not found", "recipient unknown", "does not exist", "couldn't be found",
        "could not be found", "unable to receive mail", "unable to receive email",
    )):
        return "Recipient address was not found or cannot receive email."
    if "dns error" in lowered or "no mx" in lowered:
        return "Recipient domain could not receive email (DNS error)."
    if "mailbox" in lowered and any(marker in lowered for marker in ("full", "quota", "over limit")):
        return "Recipient mailbox is full."
    if any(marker in lowered for marker in ("hop count exceeded", "mail loop", "routing loop")):
        return "Delivery failed because the recipient mail system encountered a routing loop."
    if any(marker in lowered for marker in ("blocked", "rejected", "not permitted", "policy")):
        return "Recipient mail server rejected the message."
    return "Recipient mail server returned the message as undeliverable."


def parse_bounce_message(message: dict[str, Any]) -> BounceNotice | None:
    message_id = str(message.get("id") or "").strip()
    payload = message.get("payload") or {}
    if not message_id or not isinstance(payload, dict) or set(message.get("labelIds") or []) & {"SENT", "DRAFT"}:
        return None
    if not _is_delivery_report(payload):
        return None
    subjects = _top_header_values(payload, "Subject")
    subject = subjects[0] if subjects else ""
    body_text = _report_text(payload)
    has_dsn, recipients = _dsn_recipients(payload)
    if not has_dsn and not _is_permanent_failure(subject, payload, body_text):
        return None
    if not has_dsn:
        recipients = _failed_recipients(payload, body_text)
    if not recipients:
        return None
    return BounceNotice(
        message_id, recipients, _bounce_reason(body_text), _message_time(message),
        _original_message_ids(payload), str(message.get("threadId") or "") or None,
    )


def _is_automated_response(payload: dict[str, Any], subject: str) -> bool:
    auto_submitted = " ".join(_top_header_values(payload, "Auto-Submitted")).strip().lower()
    if auto_submitted and auto_submitted != "no":
        return True
    if any(_top_header_values(payload, name) for name in (
        "X-Autoreply", "X-Autorespond", "X-Auto-Response-Suppress",
    )):
        return True
    precedence = " ".join(_top_header_values(payload, "Precedence")).lower()
    if any(marker in precedence for marker in ("auto_reply", "auto-reply")):
        return True
    lowered_subject = subject.lower().strip()
    return any(lowered_subject.startswith(marker) for marker in AUTOMATED_SUBJECT_MARKERS)


def parse_response_message(message: dict[str, Any], *, sender_email: str) -> ResponseNotice | None:
    message_id = str(message.get("id") or "").strip()
    payload = message.get("payload") or {}
    if not message_id or not isinstance(payload, dict) or "SENT" in set(message.get("labelIds") or []):
        return None
    addresses = _dedupe_addresses(address for _, address in getaddresses(_top_header_values(payload, "From")))
    if not addresses:
        return None
    response_from = addresses[0]
    if response_from == sender_email.lower() or response_from.partition("@")[0] in {"mailer-daemon", "postmaster"}:
        return None
    subjects = _top_header_values(payload, "Subject")
    subject = subjects[0] if subjects else ""
    if _is_delivery_report(payload):
        return None
    return ResponseNotice(
        gmail_message_id=message_id,
        gmail_thread_id=str(message.get("threadId") or "").strip() or None,
        recipient_email=response_from,
        event_type="automated_response" if _is_automated_response(payload, subject) else "replied",
        occurred_at=_message_time(message),
    )


def _gmail_message_ids(service: Any, query: str, *, max_pages: int | None = 5) -> list[str]:
    message_ids: list[str] = []
    page_token: str | None = None
    page = 0
    while max_pages is None or page < max_pages:
        page += 1
        response = service.users().messages().list(
            userId="me", q=query, maxResults=100, pageToken=page_token, includeSpamTrash=True,
        ).execute()
        message_ids.extend(str(item["id"]) for item in response.get("messages") or [] if item.get("id"))
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return message_ids


def _matching_sent_log(
    session: Session,
    *,
    sender_id: int,
    recipient_email: str,
    occurred_at: datetime,
    gmail_thread_id: str | None = None,
) -> SendLog | None:
    timing_filters = (
        SendLog.sender_id == sender_id,
        func.lower(SendLog.recipient_email) == recipient_email,
        SendLog.status.in_(("sent", "success")),
        SendLog.sent_at.is_not(None),
        SendLog.sent_at >= occurred_at - GMAIL_ACTIVITY_MATCH_LOOKBACK,
        SendLog.sent_at <= occurred_at + timedelta(minutes=15),
    )
    if gmail_thread_id:
        thread_match = session.scalar(
            select(SendLog).where(*timing_filters, SendLog.gmail_thread_id == gmail_thread_id)
            .order_by(SendLog.sent_at.desc(), SendLog.id.desc()).limit(1)
        )
        if thread_match:
            return thread_match
    return session.scalar(
        select(SendLog).where(
            *timing_filters,
        ).order_by(SendLog.sent_at.desc(), SendLog.id.desc()).limit(1)
    )


def _matching_bounce_log(
    session: Session,
    sender: Sender,
    notice: BounceNotice,
    recipient_email: str,
    *,
    service: Any | None,
    message_id_cache: dict[str, tuple[str, ...]],
) -> SendLog | None:
    candidates = list(session.scalars(select(SendLog).where(
        SendLog.user_id == sender.user_id,
        SendLog.sender_id == sender.id,
        func.lower(SendLog.recipient_email) == recipient_email,
        SendLog.status.in_(("sent", "success", "bounced")),
        SendLog.sent_at.is_not(None),
        SendLog.sent_at <= notice.occurred_at + timedelta(minutes=15),
    ).order_by(SendLog.sent_at.desc(), SendLog.id.desc())))
    if notice.original_message_ids:
        if service is None:
            return None
        matches = []
        for log in candidates:
            if not log.gmail_message_id:
                continue
            if log.gmail_message_id not in message_id_cache:
                try:
                    original = service.users().messages().get(
                        userId="me", id=log.gmail_message_id, format="metadata", metadataHeaders=["Message-ID"],
                    ).execute()
                except HttpError as exc:
                    if getattr(exc.resp, "status", None) == 404:
                        continue
                    raise
                message_id_cache[log.gmail_message_id] = _message_identifiers(
                    _top_header_values(original.get("payload") or {}, "Message-ID"),
                )
            if set(notice.original_message_ids) & set(message_id_cache[log.gmail_message_id]):
                matches.append(log)
        return matches[0] if len(matches) == 1 else None
    thread_matches = [log for log in candidates if notice.gmail_thread_id and log.gmail_thread_id == notice.gmail_thread_id]
    if thread_matches:
        return thread_matches[0] if len(thread_matches) == 1 else None
    # A recipient alone cannot distinguish repeat sends. Leave ambiguous reports
    # unmatched rather than assigning a failure to the most recent campaign.
    recent = [log for log in candidates if _aware_utc(log.sent_at) >= notice.occurred_at - GMAIL_ACTIVITY_MATCH_LOOKBACK]
    return recent[0] if len(recent) == 1 else None


def _event_seen(session: Session, *, sender_id: int, gmail_message_id: str) -> bool:
    events = list(session.scalars(select(GmailActivityEvent).where(
        GmailActivityEvent.sender_id == sender_id,
        GmailActivityEvent.gmail_message_id == gmail_message_id,
    )))
    # An NDR can arrive before the send transaction commits. Revisit unmatched
    # failures on recovery instead of permanently losing that correlation.
    return bool(events) and not any(event.event_type == "bounced" and event.send_log_id is None for event in events)


def _add_event(session: Session, event: GmailActivityEvent) -> bool:
    try:
        with session.begin_nested():
            session.add(event)
            session.flush()
        return True
    except IntegrityError:
        return False


def _record_bounce(
    session: Session, sender: Sender, notice: BounceNotice, *,
    service: Any | None = None, message_id_cache: dict[str, tuple[str, ...]] | None = None,
) -> tuple[int, int]:
    new_events = 0
    matched = 0
    for recipient_email in notice.recipients:
        log = _matching_bounce_log(
            session, sender, notice, recipient_email, service=service,
            message_id_cache=message_id_cache if message_id_cache is not None else {},
        )
        existing = session.scalar(select(GmailActivityEvent).where(
            GmailActivityEvent.sender_id == sender.id,
            GmailActivityEvent.gmail_message_id == notice.gmail_message_id,
            GmailActivityEvent.event_type == "bounced",
            GmailActivityEvent.recipient_email == recipient_email,
        ))
        if existing:
            if existing.send_log_id is not None or log is None:
                continue
            existing.send_log_id = log.id
        elif not _add_event(session, GmailActivityEvent(
            user_id=sender.user_id,
            sender_id=sender.id,
            send_log_id=log.id if log else None,
            gmail_message_id=notice.gmail_message_id,
            event_type="bounced",
            recipient_email=recipient_email,
            detail=notice.reason,
            occurred_at=notice.occurred_at,
        )):
            continue
        else:
            new_events += 1
        if not log:
            continue
        if log.status == "bounced":
            continue
        log.status = "bounced"
        log.error_message = notice.reason
        log.bounced_at = notice.occurred_at
        if log.campaign_id is not None and log.recipient_id is not None:
            campaign_recipient = session.get(
                CampaignRecipient, {"campaign_id": log.campaign_id, "contact_id": log.recipient_id},
            )
            newer_campaign_send = session.scalar(select(SendLog.id).where(
                SendLog.user_id == sender.user_id,
                SendLog.campaign_id == log.campaign_id,
                SendLog.recipient_id == log.recipient_id,
                SendLog.sent_at > log.sent_at,
            ).limit(1))
            if (
                campaign_recipient and campaign_recipient.status == "sent" and not newer_campaign_send
                and (not campaign_recipient.reset_at or _aware_utc(log.sent_at) >= _aware_utc(campaign_recipient.reset_at))
            ):
                campaign_recipient.status = "bounced"
        if log.recipient_id is not None:
            contact = session.get(Contact, log.recipient_id)
            newer_contact_send = session.scalar(select(SendLog.id).where(
                SendLog.user_id == sender.user_id, SendLog.recipient_id == log.recipient_id,
                SendLog.sent_at > log.sent_at,
            ).limit(1))
            if contact and contact.user_id == sender.user_id and contact.status in {"approved", "sent"} and not newer_contact_send:
                contact.status = "bounced"
        matched += 1
    return new_events, matched


def _record_response(session: Session, sender: Sender, notice: ResponseNotice) -> tuple[int, int]:
    log = _matching_sent_log(
        session,
        sender_id=sender.id,
        recipient_email=notice.recipient_email,
        occurred_at=notice.occurred_at,
        gmail_thread_id=notice.gmail_thread_id,
    )
    if not log:
        # Keeping ignored events makes backfills idempotent and gives us an
        # audit trail without treating unrelated inbox mail as a reply.
        _add_event(session, GmailActivityEvent(
            user_id=sender.user_id,
            sender_id=sender.id,
            send_log_id=None,
            gmail_message_id=notice.gmail_message_id,
            event_type="ignored",
            recipient_email=notice.recipient_email,
            detail="Inbound message was not related to a recorded outreach send.",
            occurred_at=notice.occurred_at,
        ))
        return 0, 0
    detail = "Automated response received." if notice.event_type == "automated_response" else "Human reply received."
    if not _add_event(session, GmailActivityEvent(
        user_id=sender.user_id,
        sender_id=sender.id,
        send_log_id=log.id,
        gmail_message_id=notice.gmail_message_id,
        event_type=notice.event_type,
        recipient_email=log.recipient_email.lower(),
        detail=detail,
        occurred_at=notice.occurred_at,
    )):
        return 0, 0
    if notice.event_type == "replied" or log.response_status != "replied":
        log.response_status = notice.event_type
        log.responded_at = notice.occurred_at
        log.response_gmail_message_id = notice.gmail_message_id
    return 1, 1


def _empty_sync_result() -> dict[str, int]:
    return {
        "checked_messages": 0,
        "new_events": 0,
        "undelivered": 0,
        "replied": 0,
        "automated_responses": 0,
    }


def _process_gmail_message(
    session: Session,
    sender: Sender,
    message: dict[str, Any],
    *,
    service: Any | None = None,
    message_id_cache: dict[str, tuple[str, ...]] | None = None,
) -> dict[str, int]:
    result = _empty_sync_result()
    gmail_message_id = str(message.get("id") or "").strip()
    if not gmail_message_id or _event_seen(session, sender_id=sender.id, gmail_message_id=gmail_message_id):
        return result
    result["checked_messages"] = 1
    bounce = parse_bounce_message(message)
    if bounce:
        new_events, matched = _record_bounce(session, sender, bounce, service=service, message_id_cache=message_id_cache)
        result["new_events"] += new_events
        result["undelivered"] += matched
        return result
    response = parse_response_message(message, sender_email=sender.email)
    if not response:
        return result
    new_events, matched = _record_response(session, sender, response)
    result["new_events"] += new_events
    if matched:
        result["automated_responses" if response.event_type == "automated_response" else "replied"] += matched
    return result


def _merge_sync_result(target: dict[str, int], source: dict[str, int]) -> None:
    for key in target:
        target[key] += source[key]


def sync_sender_gmail_activity(
    session: Session,
    sender: Sender,
    *,
    service: Any | None = None,
) -> dict[str, int]:
    """Recover delivery reports since this account's earliest recorded send.

    This runs during explicit checks, initial setup, and expired-cursor recovery.
    Ordinary worker ticks use incremental history rather than mailbox searches.
    """
    result = _empty_sync_result()
    if not sender_tracks_gmail_activity(sender):
        return result
    gmail = service or gmail_service_for_sender(session, sender)
    earliest_send = session.scalar(select(func.min(SendLog.sent_at)).where(
        SendLog.user_id == sender.user_id, SendLog.sender_id == sender.id,
    ))
    if earliest_send is None:
        return result
    after = int((_aware_utc(earliest_send) - timedelta(days=1)).timestamp())
    message_ids = list(dict.fromkeys([
        *_gmail_message_ids(gmail, f"{BOUNCE_SEARCH_QUERY} after:{after}", max_pages=None),
        *_gmail_message_ids(gmail, RESPONSE_SEARCH_QUERY),
    ]))
    message_id_cache: dict[str, tuple[str, ...]] = {}
    for gmail_message_id in message_ids:
        if _event_seen(session, sender_id=sender.id, gmail_message_id=gmail_message_id):
            continue
        try:
            message = gmail.users().messages().get(
                userId="me", id=gmail_message_id, format="full",
            ).execute()
        except HttpError as exc:
            if getattr(exc.resp, "status", None) == 404:
                continue
            raise
        _merge_sync_result(result, _process_gmail_message(
            session, sender, message, service=gmail, message_id_cache=message_id_cache,
        ))
    return result


def _sync_error_message(exc: Exception) -> str:
    detail = str(exc).lower()
    if any(marker in detail for marker in ("insufficient", "permission", "403", "invalid_grant")):
        return "Reconnect Gmail to allow reply and undelivered-message checks."
    return "Could not check Gmail for replies and undelivered messages. Try again later."


def _history_value(value: str | None) -> int:
    try:
        return int(value or "0")
    except (TypeError, ValueError):
        return 0


def gmail_pubsub_topic() -> str:
    return os.getenv("GMAIL_PUBSUB_TOPIC", "").strip()


def register_sender_gmail_watch(
    session: Session,
    sender: Sender,
    *,
    service: Any | None = None,
    now: datetime | None = None,
    reset_cursor: bool = False,
) -> dict[str, Any]:
    if not sender_tracks_gmail_activity(sender):
        raise PermissionError("Gmail read permission is missing")
    topic = gmail_pubsub_topic()
    if not topic:
        raise RuntimeError("GMAIL_PUBSUB_TOPIC is not configured")
    current = _aware_utc(now or utcnow())
    gmail = service or gmail_service_for_sender(session, sender)
    response = gmail.users().watch(
        userId="me",
        body={
            "topicName": topic,
        },
    ).execute()
    history_id = str(response.get("historyId") or "").strip()
    if not history_id:
        raise RuntimeError("Gmail watch did not return a history cursor")
    try:
        expiration = datetime.fromtimestamp(int(str(response["expiration"])) / 1000, tz=timezone.utc)
    except (KeyError, TypeError, ValueError, OSError) as exc:
        raise RuntimeError("Gmail watch did not return a valid expiration") from exc
    if reset_cursor or not sender.gmail_history_id:
        sender.gmail_history_id = history_id
    sender.gmail_watch_expiration_at = expiration
    sender.gmail_watch_refresh_at = min(current + GMAIL_WATCH_RENEW_INTERVAL, expiration - timedelta(hours=1))
    sender.gmail_tracking_status = "active"
    sender.gmail_sync_error = None
    return {"history_id": history_id, "expiration_at": expiration.isoformat()}


def _gmail_history_message_ids(service: Any, start_history_id: str) -> tuple[list[str], str]:
    message_ids: list[str] = []
    seen: set[str] = set()
    page_token: str | None = None
    latest_history_id = start_history_id
    while True:
        response = service.users().history().list(
            userId="me",
            startHistoryId=start_history_id,
            historyTypes=["messageAdded"],
            maxResults=500,
            pageToken=page_token,
        ).execute()
        latest_history_id = str(response.get("historyId") or latest_history_id)
        for history in response.get("history") or []:
            for added in history.get("messagesAdded") or []:
                message_id = str((added.get("message") or {}).get("id") or "").strip()
                if message_id and message_id not in seen:
                    seen.add(message_id)
                    message_ids.append(message_id)
        page_token = response.get("nextPageToken")
        if not page_token:
            return message_ids, latest_history_id


def process_sender_gmail_history(
    session: Session,
    sender: Sender,
    *,
    service: Any | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    result = _empty_sync_result()
    if not sender_tracks_gmail_activity(sender):
        return result
    gmail = service or gmail_service_for_sender(session, sender)
    if not sender.gmail_history_id:
        register_sender_gmail_watch(session, sender, service=gmail, now=now)
        if not sender.gmail_backfill_completed_at:
            result = sync_sender_gmail_activity(session, sender, service=gmail)
            sender.gmail_backfill_completed_at = _aware_utc(now or utcnow())
        sender.gmail_pending_history_id = None
        sender.gmail_push_pending = 0
        sender.gmail_push_locked_at = None
        sender.gmail_push_retry_at = None
        return result
    message_ids, latest_history_id = _gmail_history_message_ids(gmail, sender.gmail_history_id)
    message_id_cache: dict[str, tuple[str, ...]] = {}
    for gmail_message_id in message_ids:
        try:
            message = gmail.users().messages().get(
                userId="me", id=gmail_message_id, format="full",
            ).execute()
        except HttpError as exc:
            if getattr(exc.resp, "status", None) == 404:
                continue  # A message can be deleted between notification and read.
            raise
        _merge_sync_result(result, _process_gmail_message(
            session, sender, message, service=gmail, message_id_cache=message_id_cache,
        ))
    sender.gmail_history_id = latest_history_id
    sender.gmail_tracking_status = "active"
    sender.gmail_sync_checked_at = _aware_utc(now or utcnow())
    sender.gmail_sync_error = None

    # Lock at the end, after Gmail I/O, so a concurrent webhook cannot be
    # overwritten while we decide whether a newer notification is still due.
    session.flush()
    session.refresh(sender, attribute_names=["gmail_pending_history_id", "gmail_push_pending"], with_for_update=True)
    if _history_value(sender.gmail_pending_history_id) <= _history_value(latest_history_id):
        sender.gmail_pending_history_id = None
        sender.gmail_push_pending = 0
        sender.gmail_push_retry_at = None
    else:
        sender.gmail_push_pending = 1
        sender.gmail_push_retry_at = _aware_utc(now or utcnow())
    sender.gmail_push_locked_at = None
    return result


def enqueue_gmail_push_notification(
    session: Session,
    *,
    email_address: str,
    history_id: str,
    now: datetime | None = None,
) -> int:
    normalized_email = email_address.strip().lower()
    incoming_history = _history_value(history_id)
    if not normalized_email or not incoming_history:
        raise ValueError("The Gmail notification is missing a valid mailbox or history cursor")
    current = _aware_utc(now or utcnow())
    senders = list(session.scalars(
        select(Sender).where(
            Sender.email == normalized_email,
            Sender.status == "connected",
            Sender.encrypted_oauth_credentials.is_not(None),
            Sender.gmail_tracking_status.in_(("pending", "active", "error")),
        ).order_by(Sender.id).with_for_update()
    ))
    queued = 0
    for sender in senders:
        if not sender_tracks_gmail_activity(sender):
            continue
        if incoming_history <= _history_value(sender.gmail_history_id):
            continue
        sender.gmail_pending_history_id = str(max(
            incoming_history,
            _history_value(sender.gmail_pending_history_id),
        ))
        sender.gmail_push_pending = 1
        sender.gmail_push_retry_at = current
        queued += 1
    return queued


def renew_due_gmail_watches(
    session: Session,
    *,
    max_senders: int = 25,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = _aware_utc(now or utcnow())
    result: dict[str, Any] = {"checked_senders": 0, "renewed": 0, "errors": 0}
    if not gmail_pubsub_topic():
        return {**result, "status": "not_configured"}
    sender_ids = list(session.scalars(
        select(Sender.id).where(
            Sender.status == "connected",
            Sender.encrypted_oauth_credentials.is_not(None),
            Sender.gmail_tracking_status.in_(("pending", "active", "error")),
            or_(Sender.gmail_watch_refresh_at.is_(None), Sender.gmail_watch_refresh_at <= current),
        ).order_by(Sender.gmail_watch_refresh_at.asc().nullsfirst(), Sender.id).limit(max(1, max_senders))
    ))
    for sender_id in sender_ids:
        try:
            sender = session.get(Sender, sender_id)
            if not sender or not sender_tracks_gmail_activity(sender):
                continue
            register_sender_gmail_watch(session, sender, now=current)
            session.commit()
            result["renewed"] += 1
        except Exception as exc:
            session.rollback()
            logger.exception("Gmail watch registration failed for sender_id=%s", sender_id)
            failed_sender = session.get(Sender, sender_id)
            if failed_sender:
                failed_sender.gmail_tracking_status = "error"
                failed_sender.gmail_watch_refresh_at = current + GMAIL_WATCH_RETRY_INTERVAL
                failed_sender.gmail_sync_error = _sync_error_message(exc)
                session.commit()
            result["errors"] += 1
        result["checked_senders"] += 1
    result["status"] = "ok" if not result["errors"] else "partial"
    return result


def _is_expired_history_error(exc: Exception) -> bool:
    if not isinstance(exc, HttpError):
        return False
    return (
        getattr(exc, "status_code", None) == 404
        or getattr(getattr(exc, "resp", None), "status", None) == 404
    )


def process_pending_gmail_notifications(
    session: Session,
    *,
    max_senders: int = 25,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = _aware_utc(now or utcnow())
    result: dict[str, Any] = {
        "checked_senders": 0,
        "new_events": 0,
        "undelivered": 0,
        "replied": 0,
        "automated_responses": 0,
        "reconnect_required": 0,
        "errors": 0,
    }
    for _ in range(max(1, max_senders)):
        sender = session.scalar(
            select(Sender).where(
                Sender.status == "connected",
                Sender.encrypted_oauth_credentials.is_not(None),
                Sender.gmail_push_pending == 1,
                or_(Sender.gmail_push_retry_at.is_(None), Sender.gmail_push_retry_at <= current),
                or_(
                    Sender.gmail_push_locked_at.is_(None),
                    Sender.gmail_push_locked_at < current - GMAIL_PUSH_LOCK_TIMEOUT,
                ),
            ).order_by(Sender.gmail_push_retry_at.asc().nullsfirst(), Sender.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if not sender:
            break
        sender_id = sender.id
        sender.gmail_push_locked_at = current
        session.commit()
        try:
            sender = session.get(Sender, sender_id)
            if not sender or not sender_tracks_gmail_activity(sender):
                result["reconnect_required"] += 1
                if sender:
                    sender.gmail_push_pending = 0
                    sender.gmail_push_locked_at = None
                session.commit()
                continue
            sender_result = process_sender_gmail_history(session, sender, now=current)
            session.commit()
            result["checked_senders"] += 1
            for key in ("new_events", "undelivered", "replied", "automated_responses"):
                result[key] += sender_result[key]
        except Exception as exc:
            session.rollback()
            logger.exception("Gmail activity sync failed for sender_id=%s", sender_id)
            failed_sender = session.get(Sender, sender_id)
            if failed_sender:
                if _is_expired_history_error(exc):
                    try:
                        gmail = gmail_service_for_sender(session, failed_sender)
                        register_sender_gmail_watch(
                            session, failed_sender, service=gmail, now=current, reset_cursor=True,
                        )
                        sender_result = sync_sender_gmail_activity(session, failed_sender, service=gmail)
                        failed_sender.gmail_backfill_completed_at = current
                        _merge_sync_result(sender_result, process_sender_gmail_history(
                            session, failed_sender, service=gmail, now=current,
                        ))
                        session.commit()
                        result["checked_senders"] += 1
                        for key in ("new_events", "undelivered", "replied", "automated_responses"):
                            result[key] += sender_result[key]
                        continue
                    except Exception as recovery_exc:
                        session.rollback()
                        logger.exception("Gmail history recovery failed for sender_id=%s", sender_id)
                        exc = recovery_exc
                        failed_sender = session.get(Sender, sender_id)
                if failed_sender:
                    failed_sender.gmail_tracking_status = "error"
                    failed_sender.gmail_push_pending = 1
                    failed_sender.gmail_push_locked_at = None
                    failed_sender.gmail_push_retry_at = current + GMAIL_PUSH_RETRY_INTERVAL
                    failed_sender.gmail_sync_checked_at = current
                    failed_sender.gmail_sync_error = _sync_error_message(exc)
                session.commit()
            result["errors"] += 1
    result["status"] = "ok" if not result["errors"] else "partial"
    return result


def sync_selected_gmail_activity(
    session: Session,
    *,
    sender_ids: Iterable[int],
    now: datetime | None = None,
) -> dict[str, Any]:
    """User-triggered setup/backfill for a selected set of sender accounts."""
    current = _aware_utc(now or utcnow())
    ids = set(sender_ids)
    result: dict[str, Any] = {
        "checked_senders": 0,
        "new_events": 0,
        "undelivered": 0,
        "replied": 0,
        "automated_responses": 0,
        "reconnect_required": 0,
        "errors": 0,
    }
    if not ids:
        return {**result, "status": "ok"}
    for sender in session.scalars(select(Sender).where(Sender.id.in_(ids)).order_by(Sender.id)):
        sender_id = sender.id
        if not sender_tracks_gmail_activity(sender):
            result["reconnect_required"] += 1
            continue
        try:
            gmail = gmail_service_for_sender(session, sender)
            # Renew here even for an active connection so existing Inbox-only
            # watches are upgraded immediately when the user checks Gmail.
            register_sender_gmail_watch(session, sender, service=gmail, now=current)
            try:
                sender_result = process_sender_gmail_history(session, sender, service=gmail, now=current)
            except HttpError as exc:
                if not _is_expired_history_error(exc):
                    raise
                register_sender_gmail_watch(session, sender, service=gmail, now=current, reset_cursor=True)
                sender_result = _empty_sync_result()
            # A manual check must recover archived/older failures even when no
            # push is pending and a previous setup backfill was completed.
            _merge_sync_result(sender_result, sync_sender_gmail_activity(session, sender, service=gmail))
            sender.gmail_backfill_completed_at = current
            sender.gmail_sync_checked_at = current
            sender.gmail_sync_error = None
            session.commit()
            result["checked_senders"] += 1
            for key in ("new_events", "undelivered", "replied", "automated_responses"):
                result[key] += sender_result[key]
        except Exception as exc:
            session.rollback()
            logger.exception("Manual Gmail activity check failed for sender_id=%s", sender_id)
            failed_sender = session.get(Sender, sender_id)
            if failed_sender:
                failed_sender.gmail_tracking_status = "error"
                failed_sender.gmail_watch_refresh_at = current + GMAIL_WATCH_RETRY_INTERVAL
                failed_sender.gmail_sync_checked_at = current
                failed_sender.gmail_sync_error = _sync_error_message(exc)
                session.commit()
            result["errors"] += 1
    result["status"] = "ok" if not result["errors"] else "partial"
    return result
