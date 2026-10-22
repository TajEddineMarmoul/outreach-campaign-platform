"""First-party, privacy-minimising email engagement tracking."""

from __future__ import annotations

import html
import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.platform.models import EmailTrackingEvent, EmailTrackingLink, SendLog
from src.platform.time import utcnow


HREF_PATTERN = re.compile(
    r"(?P<prefix>\bhref\s*=\s*)(?P<quote>[\"'])(?P<url>.*?)(?P=quote)",
    re.IGNORECASE,
)
HTML_TAG_PATTERN = re.compile(r"<[a-z][\s\S]*?>", re.IGNORECASE)


@dataclass(frozen=True)
class TrackedEmail:
    body: str
    enabled: bool


def public_tracking_base_url() -> str:
    """Return the recipient-reachable API origin, never a browser proxy URL."""
    return (
        os.getenv("TRACKING_BASE_URL", "").strip().rstrip("/")
        or os.getenv("BACKEND_URL", "").strip().rstrip("/")
    )


def tracking_is_configured() -> bool:
    return bool(public_tracking_base_url())


def _is_trackable_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme.lower() in {"http", "https"} and bool(parsed.netloc)


def _html_email_body(body: str) -> str:
    if HTML_TAG_PATTERN.search(body):
        return body
    return f"<div>{html.escape(body).replace(chr(10), '<br>')}</div>"


def _tracking_token() -> str:
    return secrets.token_urlsafe(24)


def _inject_pixel(body: str, pixel_url: str) -> str:
    pixel = (
        f'<img src="{html.escape(pixel_url, quote=True)}" width="1" height="1" '
        'alt="" aria-hidden="true" style="display:none!important;width:1px!important;'
        'height:1px!important;border:0!important;" />'
    )
    match = re.search(r"</body\s*>", body, flags=re.IGNORECASE)
    if match:
        return f"{body[:match.start()]}{pixel}{body[match.start():]}"
    return f"{body}{pixel}"


def prepare_tracked_email(session: Session, log: SendLog, body: str) -> TrackedEmail:
    """Create one opaque pixel and redirect links for a pending send log.

    The destination URLs remain server-side. We intentionally do not collect IP
    addresses or user-agent strings, which are neither needed for campaign
    metrics nor appropriate for this product's first-party reporting.
    """
    base_url = public_tracking_base_url()
    if not base_url:
        return TrackedEmail(body=body, enabled=False)

    html_body = _html_email_body(body)
    pixel_token = _tracking_token()
    log.tracking_token = pixel_token

    def replace_href(match: re.Match[str]) -> str:
        original_url = html.unescape(match.group("url")).strip()
        if not _is_trackable_url(original_url):
            return match.group(0)
        token = _tracking_token()
        session.add(
            EmailTrackingLink(
                send_log_id=log.id,
                token=token,
                destination_url=original_url,
            )
        )
        return f'{match.group("prefix")}{match.group("quote")}{base_url}/api/track/c/{token}{match.group("quote")}'

    linked_body = HREF_PATTERN.sub(replace_href, html_body)
    return TrackedEmail(
        body=_inject_pixel(linked_body, f"{base_url}/api/track/o/{pixel_token}.gif"),
        enabled=True,
    )


def _record_event(
    session: Session,
    log: SendLog,
    *,
    event_type: str,
    occurred_at: datetime,
    link_url: str | None = None,
) -> None:
    session.add(
        EmailTrackingEvent(
            user_id=log.user_id,
            campaign_id=log.campaign_id,
            send_log_id=log.id,
            event_type=event_type,
            link_url=link_url,
            occurred_at=occurred_at,
        )
    )
    if event_type == "opened":
        log.first_opened_at = log.first_opened_at or occurred_at
        log.last_opened_at = occurred_at
        log.open_count += 1
    else:
        log.first_clicked_at = log.first_clicked_at or occurred_at
        log.last_clicked_at = occurred_at
        log.click_count += 1


def record_open(session: Session, token: str) -> bool:
    log = session.scalar(
        select(SendLog).where(SendLog.tracking_token == token).with_for_update()
    )
    if not log:
        return False
    if log.status in {"bounced", "failed", "error"}:
        return True
    _record_event(session, log, event_type="opened", occurred_at=utcnow())
    session.commit()
    return True


def record_click(session: Session, token: str) -> str | None:
    link = session.scalar(select(EmailTrackingLink).where(EmailTrackingLink.token == token))
    if not link:
        return None
    log = session.scalar(select(SendLog).where(SendLog.id == link.send_log_id).with_for_update())
    if not log:
        return None
    if log.status in {"bounced", "failed", "error"}:
        return link.destination_url
    _record_event(
        session,
        log,
        event_type="clicked",
        occurred_at=utcnow(),
        link_url=link.destination_url,
    )
    session.commit()
    return link.destination_url
