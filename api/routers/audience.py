from __future__ import annotations

import json
from collections import Counter

from fastapi import APIRouter, Depends, HTTPException, Query

from api.deps import db, get_db, get_current_user_id


router = APIRouter(tags=["campaign-audience"])
AUDIENCE_FIELDS = ("company", "title", "industry", "country", "source_type", "status")


def _custom_fields(value: object) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError):
            pass
    return {}


def _audience_record(row: dict) -> dict:
    custom = _custom_fields(row.get("custom_fields"))
    contact_status = str(row.get("status") or "")
    return {
        "contact_id": row["id"],
        "email": row.get("email_normalized") or row.get("email"),
        "first_name": row.get("first_name") or custom.get("first_name", ""),
        "last_name": row.get("last_name") or custom.get("last_name", ""),
        "company": row.get("company_name") or custom.get("company", ""),
        "title": row.get("title") or custom.get("title", ""),
        "industry": row.get("industry") or custom.get("industry", ""),
        "country": row.get("country") or custom.get("country", ""),
        "source_type": row.get("source_type") or "",
        "status": (row.get("recipient_status") or contact_status) if contact_status in {"approved", "sent"} else contact_status,
        "contact_status": contact_status,
        "custom_fields": custom,
    }


def _matches(record: dict, field: str, value: str) -> bool:
    actual = record.get(field, record["custom_fields"].get(field, ""))
    return str(actual or "").casefold() == value.casefold()


def _filtered_page(
    rows: list,
    *,
    search: str,
    field: str,
    value: str,
    status: str,
    source_type: str,
    page: int,
    page_size: int,
    include_facets: bool,
) -> dict:
    if (field and not value) or (value and not field):
        raise HTTPException(status_code=422, detail="Provide both field and value for an audience filter")
    records = [_audience_record(dict(row)) for row in rows]
    if search:
        term = search.casefold().strip()
        records = [
            record for record in records
            if any(term in str(record.get(key) or "").casefold() for key in ("email", "first_name", "last_name", "company", "title", "industry", "country"))
        ]
    if status:
        records = [record for record in records if _matches(record, "status", status)]
    if source_type:
        records = [record for record in records if _matches(record, "source_type", source_type)]
    if field:
        records = [record for record in records if _matches(record, field, value)]

    total = len(records)
    facets: dict = {}
    if include_facets:
        for key in AUDIENCE_FIELDS:
            counts = Counter(str(record.get(key) or "").strip() for record in records)
            facets[key] = [
                {"value": label, "count": count}
                for label, count in counts.most_common(30) if label
            ]
        custom_counts = Counter(key for record in records for key in record["custom_fields"])
        facets["custom_fields"] = [
            {"name": key, "count": count} for key, count in custom_counts.most_common(50)
        ]
    start = (page - 1) * page_size
    return {
        "items": records[start:start + page_size],
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": max(1, (total + page_size - 1) // page_size),
        "facets": facets,
    }


@router.get("/api/contacts/audience")
def get_saved_contact_audience(
    search: str = Query(default="", max_length=200),
    field: str = Query(default="", max_length=120),
    value: str = Query(default="", max_length=240),
    status: str = Query(default="", max_length=40),
    source_type: str = Query(default="", max_length=80),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    include_facets: bool = True,
    conn=Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    return _filtered_page(
        db.fetch_contacts(conn, user_id),
        search=search,
        field=field,
        value=value,
        status=status,
        source_type=source_type,
        page=page,
        page_size=page_size,
        include_facets=include_facets,
    )


@router.get("/api/campaigns/{campaign_id}/audience")
def get_campaign_audience(
    campaign_id: int,
    search: str = Query(default="", max_length=200),
    field: str = Query(default="", max_length=120),
    value: str = Query(default="", max_length=240),
    status: str = Query(default="", max_length=40),
    source_type: str = Query(default="", max_length=80),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    include_facets: bool = True,
    conn=Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    if not db.get_campaign(conn, campaign_id, user_id):
        raise HTTPException(status_code=404, detail="Campaign not found")
    rows = conn.execute(
        """
        SELECT c.*, cr.status AS recipient_status
        FROM campaign_recipients cr
        INNER JOIN contacts c ON c.id = cr.contact_id
        WHERE cr.campaign_id = ? AND c.user_id = ?
        ORDER BY c.id
        """,
        (campaign_id, user_id),
    ).fetchall()
    return _filtered_page(
        rows,
        search=search,
        field=field,
        value=value,
        status=status,
        source_type=source_type,
        page=page,
        page_size=page_size,
        include_facets=include_facets,
    )
