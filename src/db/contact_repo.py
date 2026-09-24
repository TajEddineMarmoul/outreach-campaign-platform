from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable

from src.models import ContactStatus

from .core import *

def insert_contact(conn: sqlite3.Connection, contact: dict[str, Any], user_id: str = "default_user") -> bool:
    now = utcnow_iso()
    data = {
        "first_name": "",
        "last_name": "",
        "full_name": "",
        "email": "",
        "email_normalized": "",
        "company_name": "",
        "company_website": "",
        "linkedin": "",
        "title": "",
        "industry": "",
        "keywords": "",
        "keyword_1": "",
        "keyword_2": "",
        "keyword_3": "",
        "country": "",
        "source_type": "csv",
        "source_url": "",
        "sheet_id": "",
        "sheet_name": "",
        "last_synced_at": None,
        "status": ContactStatus.PENDING.value,
        "custom_fields": "{}",
        "user_id": user_id,
        **contact,
        "created_at": now,
        "updated_at": now,
    }
    data["email_normalized"] = str(data["email"]).strip().lower()
    try:
        conn.execute(
            """
            INSERT INTO contacts (
                first_name, last_name, full_name, email, email_normalized, company_name,
                company_website, linkedin, title, industry, keywords,
                keyword_1, keyword_2, keyword_3, country, source_type,
                source_url, sheet_id, sheet_name, last_synced_at, status,
                custom_fields, created_at, updated_at, user_id
            )
            VALUES (
                :first_name, :last_name, :full_name, :email, :email_normalized, :company_name,
                :company_website, :linkedin, :title, :industry, :keywords,
                :keyword_1, :keyword_2, :keyword_3, :country, :source_type,
                :source_url, :sheet_id, :sheet_name, :last_synced_at, :status,
                :custom_fields, :created_at, :updated_at, :user_id
            )
            """,
            data,
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def fetch_contacts(
    conn: sqlite3.Connection,
    user_id: str = "default_user",
    statuses: Iterable[str] | None = None,
    limit: int | None = None,
) -> list[sqlite3.Row]:
    sql = "SELECT * FROM contacts WHERE user_id = ?"
    params: list[Any] = [user_id]
    if statuses:
        values = list(statuses)
        placeholders = ",".join("?" for _ in values)
        sql += f" AND status IN ({placeholders})"
        params.extend(values)
    sql += " ORDER BY id"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return list(conn.execute(sql, params).fetchall())


def add_campaign_recipients(
    conn: sqlite3.Connection,
    campaign_id: int,
    contact_ids: Iterable[int],
    user_id: str = "default_user",
) -> int:
    now = utcnow_iso()
    attached = 0
    for contact_id in contact_ids:
        normalized_contact_id = int(contact_id)
        contact = conn.execute(
            "SELECT status FROM contacts WHERE id = ? AND user_id = ?",
            (normalized_contact_id, user_id),
        ).fetchone()
        if not contact:
            continue
        contact_status = str(contact["status"] or ContactStatus.PENDING.value)
        if contact_status == ContactStatus.PENDING.value:
            contact_status = ContactStatus.APPROVED.value
            conn.execute(
                "UPDATE contacts SET status = ?, updated_at = ? WHERE id = ? AND user_id = ?",
                (contact_status, now, normalized_contact_id, user_id),
            )
        recipient_status = (
            ContactStatus.APPROVED.value
            if contact_status in {ContactStatus.APPROVED.value, ContactStatus.SENT.value}
            else contact_status
        )
        existing = conn.execute(
            """
            SELECT 1
            FROM campaign_recipients
            WHERE campaign_id = ? AND contact_id = ?
            LIMIT 1
            """,
            (campaign_id, normalized_contact_id),
        ).fetchone()
        if existing:
            continue
        conn.execute(
            """
            INSERT INTO campaign_recipients(campaign_id, contact_id, status, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (campaign_id, normalized_contact_id, recipient_status, now),
        )
        attached += 1
    conn.commit()
    return attached


def add_campaign_recipients_by_emails(
    conn: sqlite3.Connection,
    campaign_id: int,
    emails: Iterable[str],
    user_id: str = "default_user",
) -> int:
    normalized_emails = list(dict.fromkeys(str(email).strip().lower() for email in emails if str(email).strip()))
    if getattr(conn, "supports_bulk_operations", False):
        return _add_campaign_recipients_by_emails_bulk(
            conn,
            campaign_id,
            normalized_emails,
            user_id,
        )

    contact_ids: list[int] = []
    for email in normalized_emails:
        contact = fetch_contact_by_email(conn, email, user_id)
        if contact:
            contact_ids.append(int(contact["id"]))
    return add_campaign_recipients(conn, campaign_id, contact_ids, user_id)


def _add_campaign_recipients_by_emails_bulk(
    conn,
    campaign_id: int,
    emails: list[str],
    user_id: str,
) -> int:
    if not emails:
        return 0

    now = utcnow_iso()
    attached = 0
    chunk_size = 500
    for offset in range(0, len(emails), chunk_size):
        chunk = emails[offset : offset + chunk_size]
        placeholders = ",".join("?" for _ in chunk)
        conn.execute(
            f"""
            UPDATE contacts
            SET status = 'approved', updated_at = ?
            WHERE user_id = ?
              AND email_normalized IN ({placeholders})
              AND status = 'pending'
            """,
            [now, user_id, *chunk],
        )
        cursor = conn.execute(
            f"""
            INSERT INTO campaign_recipients(
                campaign_id, contact_id, status, created_at, updated_at
            )
            SELECT ?, c.id,
                CASE
                    WHEN c.status IN ('approved', 'sent') THEN 'approved'
                    ELSE c.status
                END,
                ?, ?
            FROM contacts c
            WHERE c.user_id = ?
              AND c.email_normalized IN ({placeholders})
            ON CONFLICT(campaign_id, contact_id) DO NOTHING
            """,
            [campaign_id, now, now, user_id, *chunk],
        )
        attached += max(cursor.rowcount, 0)
    conn.commit()
    return attached


def campaign_contacts(
    conn: sqlite3.Connection,
    campaign_id: int,
    user_id: str = "default_user",
    statuses: Iterable[str] | None = None,
    limit: int | None = None,
) -> list[sqlite3.Row]:
    sql = """
        SELECT c.*
        FROM contacts c
        INNER JOIN campaign_recipients cr ON cr.contact_id = c.id
        WHERE cr.campaign_id = ? AND c.user_id = ?
    """
    params: list[Any] = [campaign_id, user_id]
    if statuses:
        values = list(statuses)
        placeholders = ",".join("?" for _ in values)
        sql += f" AND c.status IN ({placeholders})"
        params.extend(values)
    sql += " ORDER BY c.id"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return list(conn.execute(sql, params).fetchall())


def campaign_contact_count(conn: sqlite3.Connection, campaign_id: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS count FROM campaign_recipients WHERE campaign_id = ?",
        (campaign_id,),
    ).fetchone()
    return int(row["count"])


def campaign_sent_count(conn: sqlite3.Connection, campaign_id: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS count FROM send_log WHERE campaign_id = ? AND status = 'sent'",
        (campaign_id,),
    ).fetchone()
    return int(row["count"])


def campaign_stats(conn: sqlite3.Connection, campaign_id: int) -> dict[str, int]:
    return {
        "recipients": campaign_contact_count(conn, campaign_id),
        "sent": campaign_sent_count(conn, campaign_id),
    }


def fetch_contact(conn: sqlite3.Connection, contact_id: int, user_id: str = "default_user") -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM contacts WHERE id = ? AND user_id = ?", (contact_id, user_id)).fetchone()


def fetch_contact_by_email(conn: sqlite3.Connection, email: str, user_id: str = "default_user") -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM contacts WHERE email = ? AND user_id = ?", (email.lower().strip(), user_id)).fetchone()


def set_contact_status(conn: sqlite3.Connection, contact_id: int, status: str, user_id: str = "default_user") -> None:
    conn.execute(
        "UPDATE contacts SET status = ?, updated_at = ? WHERE id = ? AND user_id = ?",
        (status, utcnow_iso(), contact_id, user_id),
    )
    conn.commit()


def set_contacts_status(conn: sqlite3.Connection, contact_ids: Iterable[int], status: str, user_id: str = "default_user") -> int:
    ids = list(contact_ids)
    if not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
    cursor = conn.execute(
        f"UPDATE contacts SET status = ?, updated_at = ? WHERE id IN ({placeholders}) AND user_id = ?",
        [status, utcnow_iso(), *ids, user_id],
    )
    conn.commit()
    return max(cursor.rowcount, 0)


def mark_preview_generated(
    conn: sqlite3.Connection,
    contact_id: int,
    subject: str,
    body: str,
    user_id: str = "default_user",
) -> None:
    now = utcnow_iso()
    conn.execute(
        """
        UPDATE contacts
        SET preview_generated_at = ?, last_preview_subject = ?, last_preview_body = ?,
            updated_at = ?
        WHERE id = ? AND user_id = ?
        """,
        (now, subject, body, now, contact_id, user_id),
    )
    conn.commit()


def clear_campaign_previews(conn: sqlite3.Connection, campaign_id: int, user_id: str = "default_user") -> None:
    conn.execute(
        """
        UPDATE contacts
        SET preview_generated_at = NULL,
            last_preview_subject = NULL,
            last_preview_body = NULL,
            updated_at = ?
        WHERE id IN (
            SELECT contact_id
            FROM campaign_recipients
            WHERE campaign_id = ?
        ) AND user_id = ?
        """,
        (utcnow_iso(), campaign_id, user_id),
    )
    conn.commit()


def count_contacts_by_status(conn: sqlite3.Connection, user_id: str = "default_user") -> dict[str, int]:
    counts = {status: 0 for status in STATUS_VALUES}
    rows = conn.execute("SELECT status, COUNT(*) AS count FROM contacts WHERE user_id = ? GROUP BY status", (user_id,)).fetchall()
    for row in rows:
        counts[str(row["status"])] = int(row["count"])
    counts["total"] = sum(counts.values())
    return counts


def sent_today_count(conn: sqlite3.Connection, today_prefix: str, user_id: str = "default_user") -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM send_log
        WHERE status = 'sent' AND CAST(sent_at AS TEXT) LIKE ? AND user_id = ?
        """,
        (f"{today_prefix}%", user_id),
    ).fetchone()
    return int(row["count"])


def first_successful_send_date(conn: sqlite3.Connection, user_id: str = "default_user") -> str | None:
    row = conn.execute(
        "SELECT MIN(sent_at) AS first_sent_at FROM send_log WHERE status = 'sent' AND user_id = ?",
        (user_id,)
    ).fetchone()
    return str(row["first_sent_at"]) if row and row["first_sent_at"] else None


def last_successful_send_at(conn: sqlite3.Connection, user_id: str = "default_user") -> str | None:
    row = conn.execute(
        "SELECT MAX(sent_at) AS last_sent_at FROM send_log WHERE status = 'sent' AND user_id = ?",
        (user_id,)
    ).fetchone()
    return str(row["last_sent_at"]) if row and row["last_sent_at"] else None


def has_send_attempt(conn: sqlite3.Connection, contact_id: int, user_id: str = "default_user") -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM send_log
        WHERE contact_id = ? AND status IN ('attempting', 'sent') AND user_id = ?
        LIMIT 1
        """,
        (contact_id, user_id),
    ).fetchone()
    return row is not None


def create_send_attempt(
    conn: sqlite3.Connection,
    contact_id: int | None,
    campaign_id: int | None,
    recipient_email: str,
    subject: str,
    body: str,
    attachment_name: str,
    user_id: str = "default_user",
    status: str = "attempting",
    sender_id: int | None = None,
    sender_email: str | None = None,
) -> int:
    now = utcnow_iso()
    cursor = conn.execute(
        """
        INSERT INTO send_log (
            contact_id, campaign_id, recipient_email, subject, body_snapshot,
            sent_at, status, error_message, gmail_message_id, gmail_thread_id,
            sender_id, sender_email, attachment_name, created_at, updated_at, user_id
        )
        VALUES (?, ?, ?, ?, ?, NULL, ?, NULL, NULL, NULL, ?, ?, ?, ?, ?, ?)
        """,
        (
            contact_id,
            campaign_id,
            recipient_email,
            subject,
            body,
            status,
            sender_id,
            sender_email,
            attachment_name,
            now,
            now,
            user_id,
        ),
    )
    conn.commit()
    return int(cursor.lastrowid)


def update_send_log(
    conn: sqlite3.Connection,
    log_id: int,
    status: str,
    error_message: str | None = None,
    gmail_message_id: str | None = None,
    gmail_thread_id: str | None = None,
    sent_at: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE send_log
        SET status = ?, error_message = ?, gmail_message_id = ?,
            gmail_thread_id = ?, sent_at = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            status,
            error_message,
            gmail_message_id,
            gmail_thread_id,
            sent_at,
            utcnow_iso(),
            log_id,
        ),
    )
    conn.commit()


def recent_send_errors(conn: sqlite3.Connection, limit: int, user_id: str = "default_user") -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT *
            FROM send_log
            WHERE status IN ('failed', 'sent') AND user_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()
    )


def bounce_rate_percent(conn: sqlite3.Connection, user_id: str = "default_user") -> float:
    counts = count_contacts_by_status(conn, user_id)
    sent_or_bounced = counts.get(ContactStatus.SENT.value, 0) + counts.get(ContactStatus.BOUNCED.value, 0)
    if sent_or_bounced == 0:
        return 0.0
    return (counts.get(ContactStatus.BOUNCED.value, 0) / sent_or_bounced) * 100


def sender_send_log_rows(conn: sqlite3.Connection, sender_id: int, user_id: str = "default_user") -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM send_log WHERE sender_id = ? AND user_id = ? ORDER BY created_at DESC",
            (sender_id, user_id),
        ).fetchall()
    )


def send_log_rows(conn: sqlite3.Connection, user_id: str = "default_user", campaign_id: int | None = None) -> list[sqlite3.Row]:
    if campaign_id is None:
        return list(conn.execute("SELECT * FROM send_log WHERE user_id = ? ORDER BY created_at DESC", (user_id,)).fetchall())
    return list(
        conn.execute(
            "SELECT * FROM send_log WHERE campaign_id = ? AND user_id = ? ORDER BY created_at DESC",
            (campaign_id, user_id),
        ).fetchall()
    )


def contact_rows_with_last_log(
    conn: sqlite3.Connection,
    user_id: str = "default_user",
    campaign_id: int | None = None,
) -> list[sqlite3.Row]:
    campaign_join = ""
    campaign_where = "WHERE c.user_id = ?"
    params: list[Any] = [user_id]
    if campaign_id is not None:
        campaign_join = "INNER JOIN campaign_recipients cr ON cr.contact_id = c.id"
        campaign_where = "WHERE cr.campaign_id = ? AND c.user_id = ?"
        params = [campaign_id, user_id]
    return list(
        conn.execute(
            f"""
            SELECT
                c.*,
                latest.sent_at AS last_sent_at,
                latest.error_message AS last_error_message
            FROM contacts c
            {campaign_join}
            LEFT JOIN (
                SELECT sl.*
                FROM send_log sl
                INNER JOIN (
                    SELECT contact_id, MAX(created_at) AS max_created_at
                    FROM send_log
                    WHERE contact_id IS NOT NULL AND user_id = ?
                    GROUP BY contact_id
                ) latest_ids
                    ON latest_ids.contact_id = sl.contact_id
                   AND latest_ids.max_created_at = sl.created_at
            ) latest ON latest.contact_id = c.id
            {campaign_where}
            ORDER BY c.id
            """,
            [user_id, *params],
        ).fetchall()
    )
