from datetime import timedelta
from io import StringIO
import csv

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api.auth import get_current_user_id
from src.platform.db import get_session
from src.platform.models import Campaign, EmailTrackingEvent, GmailActivityEvent, SendLog
from src.platform.time import utcnow

router = APIRouter(prefix="/api/analytics", tags=["analytics"])
SENT_STATUSES = ("sent", "success")
ATTEMPT_STATUSES = (*SENT_STATUSES, "bounced", "failed", "error")


def _window(user_id, days):
    now = utcnow()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days - 1)
    return start, now, [
        SendLog.user_id == user_id,
        SendLog.created_at >= start,
        SendLog.created_at <= now,
        SendLog.status.in_(ATTEMPT_STATUSES),
    ]


def _utc_date(session: Session, column):
    """Return a database-compatible calendar-day expression in UTC."""
    timestamp = func.timezone("UTC", column) if session.bind.dialect.name == "postgresql" else column
    return func.date(timestamp)


def _date_labels(start, days: int) -> list[str]:
    return [(start + timedelta(days=index)).date().isoformat() for index in range(days)]


def _breakdown_series(labels: list[str], rows) -> list[dict]:
    """Turn date/entity/count query rows into complete daily series for the UI."""
    groups: dict[object, dict] = {}
    for row in rows:
        day, key, name, count = row
        group = groups.setdefault(
            key,
            {"id": key, "name": name, "sent": 0, "daily": {}},
        )
        group["daily"][str(day)] = count
        group["sent"] += count

    return [
        {
            "id": group["id"],
            "name": group["name"],
            "sent": group["sent"],
            "series": [{"date": day, "sent": group["daily"].get(day, 0)} for day in labels],
        }
        for group in sorted(groups.values(), key=lambda group: (-group["sent"], group["name"].lower()))
    ]


@router.get("")
def analytics(days: int = Query(default=7, ge=1, le=30), page: int = Query(default=1, ge=1),
              session: Session = Depends(get_session), user_id: str = Depends(get_current_user_id)):
    start, now, filters = _window(user_id, days)
    dates = _date_labels(start, days)
    counts = dict(session.execute(select(SendLog.status, func.count()).where(*filters).group_by(SendLog.status)).all())
    sent = sum(counts.get(status, 0) for status in SENT_STATUSES)
    undelivered = counts.get("bounced", 0)
    send_errors = counts.get("failed", 0) + counts.get("error", 0)
    total = sum(counts.values())
    response_counts = dict(
        session.execute(
            select(GmailActivityEvent.event_type, func.count(func.distinct(GmailActivityEvent.recipient_email)))
            .where(
                GmailActivityEvent.user_id == user_id,
                GmailActivityEvent.occurred_at >= start,
                GmailActivityEvent.occurred_at <= now,
                GmailActivityEvent.event_type.in_(("replied", "automated_response")),
            )
            .group_by(GmailActivityEvent.event_type)
        ).all()
    )
    engagement_counts = dict(
        session.execute(
            select(
                EmailTrackingEvent.event_type,
                func.count(func.distinct(EmailTrackingEvent.send_log_id)),
            )
            .join(SendLog, SendLog.id == EmailTrackingEvent.send_log_id)
            .where(
                EmailTrackingEvent.user_id == user_id,
                EmailTrackingEvent.occurred_at >= start,
                EmailTrackingEvent.occurred_at <= now,
                EmailTrackingEvent.event_type.in_(("opened", "clicked")),
                SendLog.status.notin_(("bounced", "failed", "error")),
            )
            .group_by(EmailTrackingEvent.event_type)
        ).all()
    )
    engagement_event_counts = dict(
        session.execute(
            select(EmailTrackingEvent.event_type, func.count())
            .join(SendLog, SendLog.id == EmailTrackingEvent.send_log_id)
            .where(
                EmailTrackingEvent.user_id == user_id,
                EmailTrackingEvent.occurred_at >= start,
                EmailTrackingEvent.occurred_at <= now,
                EmailTrackingEvent.event_type.in_(("opened", "clicked")),
                SendLog.status.notin_(("bounced", "failed", "error")),
            )
            .group_by(EmailTrackingEvent.event_type)
        ).all()
    )
    send_date = _utc_date(session, SendLog.created_at)
    daily_statuses: dict[str, dict[str, int]] = {}
    for day, status, count in session.execute(
        select(send_date, SendLog.status, func.count())
        .where(*filters)
        .group_by(send_date, SendLog.status)
    ):
        daily_statuses.setdefault(str(day), {})[status] = count

    activity_date = _utc_date(session, GmailActivityEvent.occurred_at)
    daily_responses: dict[str, dict[str, int]] = {}
    for day, event_type, count in session.execute(
        select(
            activity_date,
            GmailActivityEvent.event_type,
            func.count(func.distinct(GmailActivityEvent.recipient_email)),
        )
        .where(
            GmailActivityEvent.user_id == user_id,
            GmailActivityEvent.occurred_at >= start,
            GmailActivityEvent.occurred_at <= now,
            GmailActivityEvent.event_type.in_(("replied", "automated_response")),
        )
        .group_by(activity_date, GmailActivityEvent.event_type)
    ):
        daily_responses.setdefault(str(day), {})[event_type] = count

    series = [
        {
            "date": day,
            "sent": sum(daily_statuses.get(day, {}).get(status, 0) for status in SENT_STATUSES),
            "undelivered": daily_statuses.get(day, {}).get("bounced", 0),
            "send_errors": (
                daily_statuses.get(day, {}).get("failed", 0)
                + daily_statuses.get(day, {}).get("error", 0)
            ),
            "human_replies": daily_responses.get(day, {}).get("replied", 0),
            "automated_replies": daily_responses.get(day, {}).get("automated_response", 0),
        }
        for day in dates
    ]

    campaign_rows = session.execute(
        select(send_date, SendLog.campaign_id, Campaign.name, func.count())
        .select_from(SendLog)
        .outerjoin(Campaign, Campaign.id == SendLog.campaign_id)
        .where(*filters, SendLog.status.in_(SENT_STATUSES))
        .group_by(send_date, SendLog.campaign_id, Campaign.name)
    )
    campaigns = _breakdown_series(
        dates,
        [
            (day, campaign_id, campaign_name or "Unassigned campaign", count)
            for day, campaign_id, campaign_name, count in campaign_rows
        ],
    )

    sender_rows = session.execute(
        select(send_date, SendLog.sender_email, func.count())
        .where(*filters, SendLog.status.in_(SENT_STATUSES))
        .group_by(send_date, SendLog.sender_email)
    )
    senders = _breakdown_series(
        dates,
        [
            (day, sender_email or "", sender_email or "Unknown sending account", count)
            for day, sender_email, count in sender_rows
        ],
    )
    logs = session.scalars(select(SendLog).where(*filters).order_by(SendLog.created_at.desc(), SendLog.id.desc()).offset((page - 1) * 6).limit(6))
    return {"attempts": total, "sent": sent, "replied": response_counts.get("replied", 0),
            "automated_responses": response_counts.get("automated_response", 0),
            "opened": engagement_counts.get("opened", 0),
            "clicked": engagement_counts.get("clicked", 0),
            "open_events": engagement_event_counts.get("opened", 0),
            "click_events": engagement_event_counts.get("clicked", 0),
            "undelivered": undelivered, "send_errors": send_errors,
            "failed": undelivered + send_errors, "series": series,
            "campaigns": campaigns, "senders": senders,
            "timezone": "UTC", "page": page,
            "items": [{"id": row.id, "email": row.recipient_email, "subject": row.subject, "created_at": row.created_at,
                       "status": row.status, "response_status": row.response_status,
                       "open_count": row.open_count, "click_count": row.click_count,
                       "last_opened_at": row.last_opened_at, "last_clicked_at": row.last_clicked_at,
                       "responded_at": row.responded_at, "error_message": row.error_message} for row in logs]}


@router.get("/export")
def export_analytics(days: int = Query(default=7, ge=1, le=30), session: Session = Depends(get_session), user_id: str = Depends(get_current_user_id)):
    _, _, filters = _window(user_id, days)
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["Recipient", "Subject", "Delivery status", "Response", "Open count", "Click count", "First opened at (UTC)", "First clicked at (UTC)", "Sent at (UTC)", "Responded at (UTC)", "Details"])
    for row in session.scalars(select(SendLog).where(*filters).order_by(SendLog.created_at.desc())):
        values = [
            row.recipient_email,
            row.subject,
            row.status,
            row.response_status or "",
            str(row.open_count),
            str(row.click_count),
            row.first_opened_at.isoformat() if row.first_opened_at else "",
            row.first_clicked_at.isoformat() if row.first_clicked_at else "",
            row.created_at.isoformat(),
            row.responded_at.isoformat() if row.responded_at else "",
            row.error_message or "",
        ]
        writer.writerow(["'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value for value in values])
    return Response("\ufeff" + output.getvalue(), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="delivery-last-{days}-days.csv"'})
