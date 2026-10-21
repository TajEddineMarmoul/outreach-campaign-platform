from __future__ import annotations

import os
os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"

import csv
import json
from io import BytesIO, StringIO
from typing import Literal
import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File
from fastapi.responses import Response
from pydantic import BaseModel, Field

from api.deps import db, get_db, get_current_user_id
from src.dnc import add_email as dnc_add_email, rows as dnc_rows
from src.importer import import_dataframe, normalize_email, is_do_not_contact

router = APIRouter()

ContactFilter = Literal["answered", "clicked", "ready", "undelivered"]


def _query_list(value) -> list:
    return value if isinstance(value, list) else []


def _activity_predicates(campaign_id: int | None) -> dict[str, tuple[str, list]]:
    log_scope = "sl.user_id = c.user_id AND (sl.recipient_id = c.id OR sl.contact_id = c.id)"
    log_params: list = []
    if campaign_id is not None:
        log_scope += " AND sl.campaign_id = ?"
        log_params.append(campaign_id)

    if campaign_id is None:
        answered = (
            "(c.status = 'replied' OR EXISTS (SELECT 1 FROM send_log sl WHERE "
            f"{log_scope} AND sl.response_status IN ('replied', 'automated_response')))"
        )
        undelivered = (
            "(c.status = 'bounced' OR EXISTS (SELECT 1 FROM send_log sl WHERE "
            f"{log_scope} AND sl.status = 'bounced'))"
        )
    else:
        answered = (
            "EXISTS (SELECT 1 FROM send_log sl WHERE "
            f"{log_scope} AND (sl.response_status IN ('replied', 'automated_response') OR sl.status = 'replied'))"
        )
        undelivered = (
            "(EXISTS (SELECT 1 FROM send_log sl WHERE "
            f"{log_scope} AND sl.status = 'bounced') OR EXISTS ("
            "SELECT 1 FROM campaign_recipients cr WHERE cr.contact_id = c.id "
            "AND cr.campaign_id = ? AND cr.status = 'bounced'))"
        )

    clicked = (
        "EXISTS (SELECT 1 FROM send_log sl WHERE "
        f"{log_scope} AND sl.click_count > 0)"
    )
    clicked_params = [*log_params]
    answered_params = [*log_params]
    undelivered_params = [*log_params]
    if campaign_id is not None:
        undelivered_params.append(campaign_id)

    if campaign_id is None:
        ready = "c.status IN ('approved', 'sent')"
        ready_params: list = []
    else:
        ready = (
            "EXISTS (SELECT 1 FROM campaign_recipients cr WHERE cr.contact_id = c.id "
            "AND cr.campaign_id = ? AND cr.status IN ('approved', 'pending', 'queued') "
            "AND c.status IN ('approved', 'sent'))"
        )
        ready_params = [campaign_id]

    return {
        "answered": (answered, answered_params),
        "clicked": (clicked, clicked_params),
        "ready": (ready, ready_params),
        "undelivered": (undelivered, undelivered_params),
    }


def _contact_where(
    conn,
    *,
    user_id: str,
    search: str,
    campaign_id: int | None,
    statuses: list[str],
) -> tuple[str, list]:
    where = "c.user_id = ?"
    params: list = [user_id]
    if campaign_id is not None:
        if not db.get_campaign(conn, campaign_id, user_id):
            raise HTTPException(status_code=404, detail="Campaign not found")
        where += " AND EXISTS (SELECT 1 FROM campaign_recipients cr WHERE cr.contact_id = c.id AND cr.campaign_id = ?)"
        params.append(campaign_id)
    if search.strip():
        where += " AND (LOWER(c.email_normalized) LIKE ? OR LOWER(c.first_name) LIKE ? OR LOWER(c.last_name) LIKE ? OR LOWER(c.company_name) LIKE ?)"
        params.extend([f"%{search.strip().lower()}%"] * 4)
    if statuses:
        predicates = _activity_predicates(campaign_id)
        clauses = []
        for status in dict.fromkeys(statuses):
            predicate = predicates.get(status)
            if predicate:
                clause, clause_params = predicate
                clauses.append(clause)
                params.extend(clause_params)
        if clauses:
            where += " AND (" + " OR ".join(clauses) + ")"
    return where, params


def _csv_cell(value: object) -> str:
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False)
    else:
        text = "" if value is None else str(value)
    if text.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + text
    return text


def _export_custom_fields(value: object) -> dict:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return {}
    return value if isinstance(value, dict) else {}


def _export_column_name(name: str, used: set[str], reserved: set[str]) -> str:
    """Keep source columns distinct from the built-in export columns."""
    safe_name = _csv_cell(name)
    if safe_name not in used:
        used.add(safe_name)
        return safe_name
    candidate = f"CSV: {name}"
    suffix = 2
    while candidate in used or candidate in reserved:
        candidate = f"CSV: {name} ({suffix})"
        suffix += 1
    used.add(candidate)
    return candidate


class DNCAddRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


class ContactCreate(DNCAddRequest):
    first_name: str = Field(default="", max_length=200)
    last_name: str = Field(default="", max_length=200)
    company: str = Field(default="", max_length=200)


@router.get("/api/contacts/library")
def contact_library(search: str = Query(default="", max_length=200), campaign_id: int | None = None,
                    statuses: list[ContactFilter] | None = Query(default=None),
                    page: int = Query(default=1, ge=1), page_size: int = Query(default=6, ge=1, le=100),
                    conn=Depends(get_db), user_id: str = Depends(get_current_user_id)):
    where, params = _contact_where(
        conn, user_id=user_id, search=search, campaign_id=campaign_id, statuses=_query_list(statuses)
    )
    total = conn.execute(f"SELECT COUNT(*) AS count FROM contacts c WHERE {where}", params).fetchone()["count"]
    rows = conn.execute(f"SELECT c.* FROM contacts c WHERE {where} ORDER BY c.id DESC LIMIT ? OFFSET ?", [*params, page_size, (page - 1) * page_size]).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        fields = item.get("custom_fields") or {}
        if isinstance(fields, str):
            try:
                fields = json.loads(fields)
            except (ValueError, TypeError):
                fields = {}
        items.append({"id": item["id"], "email": item["email_normalized"], "first_name": item.get("first_name") or fields.get("first_name", ""),
                      "last_name": item.get("last_name") or fields.get("last_name", ""), "company": item.get("company_name") or fields.get("company", ""), "status": item["status"]})
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.get("/api/contacts/export")
def export_contacts(
    search: str = Query(default="", max_length=200),
    campaign_id: int | None = None,
    statuses: list[ContactFilter] | None = Query(default=None),
    contact_ids: list[int] | None = Query(default=None),
    include_data: bool = True,
    conn=Depends(get_db),
    user_id: str = Depends(get_current_user_id),
):
    where, params = _contact_where(
        conn, user_id=user_id, search=search, campaign_id=campaign_id, statuses=_query_list(statuses)
    )
    selected_contact_ids = _query_list(contact_ids)
    if selected_contact_ids:
        unique_contact_ids = list(dict.fromkeys(selected_contact_ids))
        placeholders = ",".join("?" for _ in unique_contact_ids)
        where += f" AND c.id IN ({placeholders})"
        params.extend(unique_contact_ids)

    predicates = _activity_predicates(campaign_id)
    projections = []
    projection_params: list = []
    for name in ("answered", "clicked", "ready", "undelivered"):
        predicate, predicate_params = predicates[name]
        projections.append(f"CASE WHEN {predicate} THEN 'Yes' ELSE 'No' END AS {name}")
        projection_params.extend(predicate_params)

    rows = conn.execute(
        f"""
        SELECT c.*, {', '.join(projections)}
        FROM contacts c
        WHERE {where}
        ORDER BY c.id DESC
        """,
        [*projection_params, *params],
    ).fetchall()
    items = [dict(row) for row in rows]

    output = StringIO(newline="")
    writer = csv.writer(output)
    if include_data:
        headers = ["Name", "Email", "Company", "Contact status", "Answered", "Clicked", "Ready", "Undelivered"]
        # Export every contact column except the tenant key. Custom fields are
        # expanded below; their original JSON is also retained for lossless use.
        covered = {"email_normalized", "company_name", "status", "user_id", "answered", "clicked", "ready", "undelivered"}
        stored_keys = list(dict.fromkeys(key for item in items for key in item if key not in covered))
        fields = [_export_custom_fields(item.get("custom_fields")) for item in items]
        custom_keys = list(dict.fromkeys(str(key) for custom in fields for key in custom))
        used = set(headers)
        reserved = {_csv_cell(key) for key in [*stored_keys, *custom_keys]}
        stored_headers = [_export_column_name(key, used, reserved) for key in stored_keys]
        custom_headers = [_export_column_name(key, used, reserved) for key in custom_keys]
        writer.writerow([_csv_cell(header) for header in [*headers, *stored_headers, *custom_headers]])
        for item, custom in zip(items, fields):
            first_name = item.get("first_name") or custom.get("first_name") or ""
            last_name = item.get("last_name") or custom.get("last_name") or ""
            name = item.get("full_name") or f"{first_name} {last_name}".strip() or custom.get("full_name") or ""
            values = [
                name, item.get("email_normalized"),
                item.get("company_name") or custom.get("company_name") or custom.get("company") or "",
                item.get("status"), item["answered"], item["clicked"], item["ready"], item["undelivered"],
                *(item.get(key) for key in stored_keys),
                *(custom.get(key) for key in custom_keys),
            ]
            writer.writerow([_csv_cell(value) for value in values])
    else:
        writer.writerow(["Email"])
        writer.writerows([_csv_cell(item.get("email_normalized"))] for item in items)

    filename = "contacts.csv" if include_data else "contact-emails.csv"
    return Response(
        content="\ufeff" + output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/api/contacts")
def create_contact(req: ContactCreate, conn=Depends(get_db), user_id: str = Depends(get_current_user_id)):
    email = normalize_email(req.email)
    if db.fetch_contact_by_email(conn, email, user_id):
        raise HTTPException(status_code=409, detail="This contact is already saved")
    if is_do_not_contact(conn, email, user_id):
        raise HTTPException(status_code=422, detail="This email is on your do-not-contact list")
    result = import_dataframe(pd.DataFrame([{**req.model_dump(), "email": email}]), conn, user_id, source_type="manual")
    if result.errors:
        raise HTTPException(status_code=422, detail="; ".join(result.errors))
    return {"status": "success", **result.model_dump()}


@router.post("/api/contacts/import")
async def import_contacts(file: UploadFile = File(...), conn=Depends(get_db), user_id: str = Depends(get_current_user_id)):
    content = await file.read(5 * 1024 * 1024 + 1)
    if len(content) > 5 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Choose a CSV smaller than 5 MB")
    try:
        frame = pd.read_csv(BytesIO(content), dtype=str, keep_default_na=False, nrows=10001)
    except (ValueError, UnicodeError, pd.errors.ParserError):
        raise HTTPException(status_code=422, detail="Could not read this CSV. Use a UTF-8 CSV with an email column.")
    if len(frame) > 10000:
        raise HTTPException(status_code=422, detail="Import up to 10,000 contacts at a time")
    result = import_dataframe(frame, conn, user_id)
    if result.errors:
        raise HTTPException(status_code=422, detail="; ".join(result.errors))
    return {"status": "success", **result.model_dump()}

@router.get("/api/contacts")
def list_global_contacts(conn=Depends(get_db), user_id: str = Depends(get_current_user_id)):
    contacts = db.fetch_contacts(conn, user_id)
    return [dict(contact) for contact in contacts]

@router.get("/api/contacts/dnc")
def list_dnc_emails(conn=Depends(get_db), user_id: str = Depends(get_current_user_id)):
    rows = dnc_rows(conn, user_id)
    return [dict(row) for row in rows]

@router.post("/api/contacts/dnc")
def add_dnc_email(req: DNCAddRequest, conn=Depends(get_db), user_id: str = Depends(get_current_user_id)):
    email = req.email.strip().lower()
    if not email:
        raise HTTPException(status_code=400, detail="Email cannot be empty")
    dnc_add_email(conn, email, user_id)
    return {"status": "success"}

