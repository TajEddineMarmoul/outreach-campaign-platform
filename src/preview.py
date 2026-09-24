from __future__ import annotations

from dataclasses import dataclass

from . import db
from .models import ContactStatus, RenderedEmail
from .template_engine import render_email


@dataclass(frozen=True)
class EmailPreview:
    contact_id: int
    recipient_email: str
    subject: str
    body: str
    used_fallback: bool
    attachment_path: str


def generate_preview(conn, contact_id: int, user_id: str, campaign_id: int | None = None, mark: bool = True) -> EmailPreview:
    contact = db.fetch_contact(conn, contact_id, user_id)
    if contact is None:
        raise ValueError(f"Contact {contact_id} not found")
        
    if campaign_id is None:
        campaign = db.get_default_campaign(conn, user_id)
    else:
        campaign = conn.execute(
            "SELECT * FROM campaigns WHERE id = ? AND user_id = ?", (campaign_id, user_id)
        ).fetchone()
        
    if campaign is None:
        raise ValueError("Campaign not found")

    rendered: RenderedEmail = render_email(contact, campaign)
    if mark:
        db.mark_preview_generated(conn, contact_id, rendered.subject, rendered.body, user_id)
    return EmailPreview(
        contact_id=contact_id,
        recipient_email=rendered.recipient_email,
        subject=rendered.subject,
        body=rendered.body,
        used_fallback=rendered.used_fallback,
        attachment_path="",
    )


def generate_previews(
    conn,
    user_id: str,
    limit: int = 10,
    statuses: tuple[str, ...] = (ContactStatus.PENDING.value, ContactStatus.APPROVED.value),
) -> list[EmailPreview]:
    contacts = db.fetch_contacts(conn, user_id, statuses=statuses, limit=limit)
    return [generate_preview(conn, int(contact["id"]), user_id, mark=True) for contact in contacts]


def approve_contacts(conn, contact_ids: list[int], user_id: str) -> int:
    approved = 0
    for contact_id in contact_ids:
        contact = db.fetch_contact(conn, contact_id, user_id)
        if not contact:
            continue
        if contact["preview_generated_at"] and contact["status"] == ContactStatus.PENDING.value:
            sub = str(contact["last_preview_subject"] or "")
            body = str(contact["last_preview_body"] or "")
            if "[missing " in sub or "[missing " in body:
                continue
            db.set_contact_status(conn, contact_id, ContactStatus.APPROVED.value, user_id)
            approved += 1
    return approved


def approve_first_n(conn, n: int, user_id: str) -> int:
    contacts = db.fetch_contacts(conn, user_id, statuses=(ContactStatus.PENDING.value,), limit=n)
    contact_ids: list[int] = []
    for contact in contacts:
        generated = generate_preview(conn, int(contact["id"]), user_id, mark=True)
        contact_ids.append(generated.contact_id)
    return approve_contacts(conn, contact_ids, user_id)


def reject_contacts(conn, contact_ids: list[int], user_id: str) -> int:
    from .dnc import add_email

    rejected = 0
    for contact_id in contact_ids:
        contact = db.fetch_contact(conn, contact_id, user_id)
        if not contact:
            continue
        if add_email(conn, str(contact["email"]), user_id, "Rejected in preview"):
            rejected += 1
    return rejected
