"""User-scoped Outreach MCP server. Run with ``python -m outreach_mcp.server``."""

from __future__ import annotations

import base64
import binascii
import csv
import mimetypes
import os
import threading
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from functools import lru_cache
from io import StringIO
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import httpx
from mcp.server import MCPServer


mcp = MCPServer("Outreach campaigns")
DEFAULT_API_URL = "https://api.outreachemails.online"
_bound_workspace_user: ContextVar[str | None] = ContextVar("outreach_mcp_workspace_user", default=None)
# The in-process client is shared, so serialize access to it.
_HOSTED_REQUEST_LOCK = threading.Lock()


def hosted_http_app():
    """Build the streamable-HTTP transport served by the Outreach API.

    The caller mounts this while the application module is imported, because a
    hosting platform may serve requests without running application startup.
    Startup only needs to start the session manager and may run more than once
    in a reused instance. The session manager is private to this module, so the
    caller drives it through ``start_hosted_transport`` instead of reaching into
    the returned application.
    """

    from mcp.server.transport_security import TransportSecuritySettings

    return mcp.streamable_http_app(
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            allowed_hosts=[
                "api.outreachemails.online",
                "testserver",
                "localhost:*",
                "127.0.0.1:*",
            ],
            allowed_origins=[
                "https://www.outreachemails.online",
                "http://localhost:*",
                "http://127.0.0.1:*",
            ],
        ),
    )


@contextmanager
def bound_workspace_user(user_id: str):
    """Bind a verified proxy identity to one hosted MCP request."""
    token = _bound_workspace_user.set(user_id)
    try:
        yield
    finally:
        _bound_workspace_user.reset(token)


@asynccontextmanager
async def start_hosted_transport():
    """Run the hosted transport for the duration of one application startup.

    A hosting platform can start the same instance more than once, and a session
    manager may only be run once per instance, so a completed previous run is
    released first.
    """

    manager = mcp.session_manager
    if getattr(manager, "_has_started", False):
        task_group = getattr(manager, "_task_group", None)
        if task_group is not None:
            await task_group.__aexit__(None, None, None)
        manager._has_started = False
        manager._task_group = None
    async with manager.run():
        yield


def default_token_file() -> Path:
    if os.name == "nt" and os.getenv("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "Outreach" / "mcp-token"
    return Path.home() / ".config" / "outreach" / "mcp-token"


@lru_cache(maxsize=1)
def _client() -> httpx.Client:
    base_url = os.getenv("OUTREACH_MCP_API_URL", DEFAULT_API_URL).rstrip("/")
    parsed = urlparse(base_url)
    if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1"}):
        raise RuntimeError("OUTREACH_MCP_API_URL must use HTTPS, except for localhost")
    raw_token = os.getenv("OUTREACH_MCP_TOKEN", "").strip()
    if not raw_token:
        token_file = Path(os.getenv("OUTREACH_MCP_TOKEN_FILE", str(default_token_file())))
        try:
            raw_token = token_file.read_text(encoding="utf-8").strip()
        except FileNotFoundError as exc:
            raise RuntimeError(f"Outreach MCP token is missing: {token_file}") from exc
    if not raw_token.startswith("outreach_mcp_"):
        raise RuntimeError("Outreach MCP token has an invalid format")
    return httpx.Client(
        base_url=base_url,
        headers={"Authorization": f"Bearer {raw_token}"},
        timeout=45,
        follow_redirects=False,
    )


@lru_cache(maxsize=1)
def _hosted_client() -> Any:
    """One in-process transport for every hosted tool call.

    The hosted endpoint is served by the same FastAPI application, so a hosted
    request is a sub-request of the deployment that is already running. Reusing
    a single client keeps the worker thread and event loop stable across calls
    instead of creating one per tool call.
    """

    from fastapi.testclient import TestClient

    from api.main import app

    return TestClient(app)


def _hosted_headers(method: str, path: str, user_id: str) -> dict[str, str]:
    """Sign one API request for the workspace bound to this hosted MCP call."""

    from api.auth import (
        BACKEND_IDENTITY_SECRET,
        IS_PRODUCTION,
        LOCAL_DEV_USER_ID,
        identity_headers,
    )

    if not IS_PRODUCTION and LOCAL_DEV_USER_ID == user_id:
        return {"authorization": f"Bearer local_dev_{user_id}"}
    if not BACKEND_IDENTITY_SECRET:
        raise RuntimeError("Backend identity signing is not configured")
    return identity_headers(
        secret=BACKEND_IDENTITY_SECRET, method=method, path=path, user_id=user_id
    )


def _api(
    method: str,
    path: str,
    *,
    params: dict | None = None,
    body: dict | None = None,
    files: list[tuple[str, tuple[str, bytes, str]]] | None = None,
) -> Any:
    try:
        user_id = _bound_workspace_user.get()
        # httpx rejects a request that carries both a JSON body and files, so a
        # multipart upload must not also send body=None as JSON.
        payload: dict = {} if files else {"json": body}
        if user_id is None:
            response = _client().request(method, path, params=params, files=files, **payload)
        else:
            # The public HTTP MCP endpoint is authenticated by Clerk in the
            # web app, then by a signed proxy assertion in the API. Never use
            # the local owner's workspace token for a hosted request.
            headers = _hosted_headers(method, path, user_id)
            with _HOSTED_REQUEST_LOCK:
                response = _hosted_client().request(
                    method, path, params=params, files=files, headers=headers, **payload
                )
    except httpx.RequestError as exc:
        raise RuntimeError(f"Outreach API is unavailable: {exc.__class__.__name__}") from exc
    if response.is_redirect:
        raise RuntimeError("Outreach API redirected the request; check the configured API URL")
    if not response.is_success:
        try:
            detail = response.json().get("detail", response.reason_phrase)
        except (ValueError, AttributeError):
            detail = response.reason_phrase
        raise RuntimeError(f"Outreach API {response.status_code}: {detail}")
    if not response.content:
        return {}
    return response.json()


def _campaign_path(campaign_id: int, suffix: str = "") -> str:
    if campaign_id < 1:
        raise ValueError("campaign_id must be positive")
    return f"/api/campaigns/{campaign_id}{suffix}"


@mcp.tool()
def workspace_overview(search: str = "", status: str = "", limit: int = 50) -> dict:
    """One-call overview of campaigns, sender groups and workspace limits. Use first to find IDs."""
    campaigns = _api("GET", "/api/campaigns")
    if search:
        campaigns = [c for c in campaigns if search.casefold() in c["name"].casefold()]
    if status:
        campaigns = [c for c in campaigns if c["status"] == status]
    return {
        "campaigns": [
            {key: c.get(key) for key in ("id", "name", "status", "recipient_count", "sent_count", "updated_at")}
            for c in campaigns[:max(1, min(limit, 200))]
        ],
        "campaign_count": len(campaigns),
        "sender_groups": _api("GET", "/api/sender-groups"),
        "workspace_settings": _api("GET", "/api/settings"),
    }


@mcp.tool()
def inspect_campaign(campaign_id: int) -> dict:
    """Get complete campaign content, attachments, sender/schedule settings, audience facets and launch checks in one call."""
    path = _campaign_path(campaign_id)
    campaign = _api("GET", path)
    result = {
        "campaign": campaign,
        "settings": _api("GET", path + "/summary"),
        "attachments": _api("GET", path + "/attachments"),
        "audience": _api("GET", path + "/audience", params={"page_size": 5}),
        "validation": _api("GET", path + "/validation-summary"),
        "recipient_template_validation": _api("GET", path + "/recipient-template-validation"),
    }
    if campaign.get("status") in {"sending", "scheduled", "autopilot", "paused"}:
        result["delivery_progress"] = _api("GET", path + "/send-progress")
    return result


@mcp.tool()
def search_audience(
    campaign_id: int | None = None,
    search: str = "",
    field: str = "",
    value: str = "",
    status: str = "",
    source_type: str = "",
    page: int = 1,
    page_size: int = 50,
) -> dict:
    """Filter saved contacts or a campaign's audience by email/name, status, source, company, title, industry, country or a custom field. Returns facets."""
    if bool(field) != bool(value):
        raise ValueError("field and value must be supplied together")
    path = "/api/contacts/audience" if campaign_id is None else _campaign_path(campaign_id, "/audience")
    return _api("GET", path, params={
        "search": search,
        "field": field,
        "value": value,
        "status": status,
        "source_type": source_type,
        "page": max(1, page),
        "page_size": max(1, min(page_size, 200)),
    })


def _update_setup(
    campaign_id: int,
    *,
    name: str | None,
    subject: str | None,
    body: str | None,
    fallback_body: str | None,
    sender_group_id: int | None,
    send_settings: dict[str, Any] | None,
    engagement_tracking: bool | None,
    unsubscribe_link: bool | None,
    require_attachment: bool | None,
) -> dict:
    path = _campaign_path(campaign_id)
    completed: list[str] = []
    campaign_patch = {
        key: value for key, value in {
            "name": name,
            "subject_template": subject,
            "body_template": body,
            "fallback_body_template": fallback_body,
            "unsubscribe_link": unsubscribe_link,
            "require_attachment": require_attachment,
        }.items() if value is not None
    }
    try:
        if campaign_patch:
            _api("PATCH", path, body=campaign_patch)
            completed.append("campaign_content")
        if sender_group_id is not None:
            _api("PATCH", path + "/sender-group", body={"sender_group_id": sender_group_id})
            completed.append("sender_group")
        if send_settings is not None:
            _api("PATCH", path + "/send-settings", body=send_settings)
            completed.append("send_settings")
        if engagement_tracking is not None:
            _api("PATCH", path + "/engagement-tracking", body={"enabled": engagement_tracking})
            completed.append("engagement_tracking")
    except Exception as exc:
        return {"campaign_id": campaign_id, "completed": completed, "error": str(exc), "partial": bool(completed)}
    return {"campaign_id": campaign_id, "completed": completed}


@mcp.tool()
def create_campaign(
    name: str,
    subject: str | None = None,
    body: str | None = None,
    fallback_body: str | None = None,
    sender_group_id: int | None = None,
    send_settings: dict[str, Any] | None = None,
    recipient_csv: str | None = None,
) -> dict:
    """Create a draft and optionally set its message, sender, schedule and pasted CSV audience in one call. Never launches delivery."""
    if not name.strip():
        raise ValueError("Campaign name cannot be empty")
    created = _api("POST", "/api/campaigns", body={"name": name.strip()})
    campaign_id = int(created["id"])
    completed = ["created"]
    try:
        update = _update_setup(
            campaign_id, name=None, subject=subject, body=body,
            fallback_body=fallback_body, sender_group_id=sender_group_id,
            send_settings=send_settings, engagement_tracking=None,
            unsubscribe_link=None, require_attachment=None,
        )
        completed.extend(update["completed"])
        if "error" in update:
            return {"campaign_id": campaign_id, "completed": completed, "error": update["error"], "partial": True}
        if recipient_csv:
            _api("POST", _campaign_path(campaign_id, "/recipients/paste"), body={"raw": recipient_csv})
            completed.append("audience")
    except Exception as exc:
        return {"campaign_id": campaign_id, "completed": completed, "error": str(exc), "partial": True}
    return {"campaign_id": campaign_id, "completed": completed, "snapshot": inspect_campaign(campaign_id)}


@mcp.tool()
def update_campaign_setup(
    campaign_id: int,
    name: str | None = None,
    subject: str | None = None,
    body: str | None = None,
    fallback_body: str | None = None,
    sender_group_id: int | None = None,
    send_settings: dict[str, Any] | None = None,
    engagement_tracking: bool | None = None,
    unsubscribe_link: bool | None = None,
    require_attachment: bool | None = None,
) -> dict:
    """Change one or several editable campaign steps in one call; returns a refreshed complete snapshot. Partial successes are reported."""
    _api("GET", _campaign_path(campaign_id))
    result = _update_setup(
        campaign_id, name=name, subject=subject, body=body,
        fallback_body=fallback_body, sender_group_id=sender_group_id,
        send_settings=send_settings, engagement_tracking=engagement_tracking,
        unsubscribe_link=unsubscribe_link, require_attachment=require_attachment,
    )
    if "error" in result:
        return result
    return {**result, "snapshot": inspect_campaign(campaign_id)}


@mcp.tool()
def add_campaign_recipients(
    campaign_id: int,
    contact_ids: list[int] | None = None,
    people: list[dict[str, str]] | None = None,
    csv_text: str | None = None,
    google_sheet: dict[str, Any] | None = None,
) -> dict:
    """Add people to an editable campaign from saved contact IDs, a list of people, CSV text or a public Google Sheet. Choose one source."""
    sources = [contact_ids is not None, people is not None, csv_text is not None, google_sheet is not None]
    if sum(sources) != 1:
        raise ValueError("Choose exactly one audience source")
    path = _campaign_path(campaign_id, "/recipients")
    if contact_ids is not None:
        return _api("POST", path + "/select-existing", body={"contact_ids": contact_ids})
    if people is not None:
        if not people or len(people) > 1000:
            raise ValueError("Provide 1 to 1000 people per call")
        buffer = StringIO()
        columns = ["email", "first_name", "last_name", "company", "title", "industry", "country"]
        writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for person in people:
            writer.writerow(person)
        csv_text = buffer.getvalue()
    if csv_text is not None:
        return _api("POST", path + "/paste", body={"raw": csv_text})
    return _api("POST", path + "/google-sheet", body=google_sheet)


@mcp.tool()
def attach_saved_audience_by_filter(
    campaign_id: int,
    search: str = "",
    field: str = "",
    value: str = "",
    status: str = "",
    source_type: str = "",
    max_contacts: int = 1000,
) -> dict:
    """Find saved contacts matching a filter and attach them to an editable campaign in one call. Requires a nonempty filter; max 1000."""
    if not any((search, status, source_type, field and value)):
        raise ValueError("Provide at least one audience filter")
    if bool(field) != bool(value):
        raise ValueError("field and value must be supplied together")
    if not 1 <= max_contacts <= 1000:
        raise ValueError("max_contacts must be between 1 and 1000")
    ids: list[int] = []
    page = 1
    while True:
        result = search_audience(
            search=search, field=field, value=value, status=status,
            source_type=source_type, page=page, page_size=200,
        )
        if result["total"] > max_contacts:
            return {"matched": result["total"], "attached": 0, "error": "More contacts matched than max_contacts; narrow the filter or raise the limit"}
        ids.extend(int(item["contact_id"]) for item in result["items"])
        if page >= result["pages"]:
            break
        page += 1
    if not ids:
        return {"matched": 0, "attached": 0}
    attached = add_campaign_recipients(campaign_id, contact_ids=ids)
    return {"matched": len(ids), **attached}


@mcp.tool()
def preview_campaign(campaign_id: int, offset: int = 0, limit: int = 3) -> dict:
    """Render a few recipient-specific messages and return validation before launching."""
    path = _campaign_path(campaign_id)
    return {
        "preview": _api("GET", path + "/preview", params={"offset": max(0, offset), "limit": max(1, min(limit, 25))}),
        "validation": _api("GET", path + "/recipient-template-validation"),
    }


@mcp.tool()
def launch_campaign(
    campaign_id: int,
    mode: Literal["send_now", "schedule", "autopilot"],
    timezone: str | None = None,
    scheduled_at: str | None = None,
    schedule: dict[str, dict[str, Any]] | None = None,
    delay_minutes: int = 5,
    pacing_mode: Literal["fixed_delay", "spread_evenly"] = "fixed_delay",
    dry_run: bool = False,
) -> dict:
    """Launch delivery. send_now queues real emails immediately unless dry_run is true. Requires a prepared campaign with connected senders and approved recipients."""
    if mode == "schedule" and not scheduled_at:
        raise ValueError("scheduled_at is required for schedule mode")
    if mode == "autopilot" and not schedule:
        raise ValueError("schedule days are required for autopilot mode")
    body: dict[str, Any] = {"delay_minutes": delay_minutes, "dry_run": dry_run}
    if timezone:
        body["timezone"] = timezone
    if mode == "schedule":
        body["scheduled_at"] = scheduled_at
    if mode == "autopilot":
        body.update({"schedule": schedule, "pacing_mode": pacing_mode})
        if scheduled_at:
            body["scheduled_at"] = scheduled_at
    endpoint = {"send_now": "/send-now", "schedule": "/schedule", "autopilot": "/autopilot/start"}[mode]
    result = _api("POST", _campaign_path(campaign_id, endpoint), body=body)
    return {"campaign_id": campaign_id, "mode": mode, **result}


@mcp.tool()
def control_campaign(campaign_id: int, action: Literal["pause", "resume", "stop"]) -> dict:
    """Pause, resume or end a campaign. Stop cancels future sends but keeps campaign history."""
    return _api("POST", _campaign_path(campaign_id, "/" + action))


@mcp.tool()
def campaign_activity(campaign_id: int, page: int = 1, page_size: int = 20) -> dict:
    """Get delivery progress and recent send/reply/bounce activity in one call."""
    path = _campaign_path(campaign_id)
    return {
        "progress": _api("GET", path + "/send-progress"),
        "logs": _api("GET", path + "/send-logs", params={"page": max(1, page), "page_size": max(1, min(page_size, 50))}),
    }


@mcp.tool()
def manage_campaign_recipient(
    campaign_id: int,
    action: Literal["remove", "reset", "clear_all"],
    contact_id: int | None = None,
) -> dict:
    """Remove one recipient, reset one for retry, or clear all recipients from an editable campaign. clear_all is destructive."""
    path = _campaign_path(campaign_id, "/recipients")
    if action == "clear_all":
        return _api("DELETE", path)
    if contact_id is None or contact_id < 1:
        raise ValueError("contact_id is required for remove or reset")
    suffix = f"/{contact_id}"
    return _api("DELETE" if action == "remove" else "PATCH", path + suffix + ("/reset" if action == "reset" else ""))


@mcp.tool()
def delete_campaign(campaign_id: int, expected_name: str) -> dict:
    """Permanently delete a stopped, draft or completed campaign and its history. Requires the exact campaign name to prevent ID mistakes."""
    campaign = _api("GET", _campaign_path(campaign_id))
    if campaign["name"] != expected_name:
        raise ValueError("Campaign name did not match; inspect the campaign before deleting")
    return _api("DELETE", _campaign_path(campaign_id))


@mcp.tool()
def duplicate_campaign(campaign_id: int) -> dict:
    """Copy a campaign into a new editable draft."""
    return _api("POST", _campaign_path(campaign_id, "/duplicate"))


@mcp.tool()
def update_autopilot_limits(campaign_id: int, daily_limits: dict[str, int]) -> dict:
    """Change daily caps on a running Autopilot campaign without pausing it."""
    return _api("PATCH", _campaign_path(campaign_id, "/autopilot/daily-limits"), body={"daily_limits": daily_limits})


@mcp.tool()
def sender_groups(
    action: Literal["list", "create", "rename", "delete", "connect_url"] = "list",
    group_id: int | None = None,
    name: str | None = None,
) -> Any:
    """List or manage sender groups. connect_url starts Gmail OAuth and returns a browser URL; the account owner must finish Google sign-in."""
    path = "/api/sender-groups"
    if action == "list":
        return _api("GET", path)
    if action == "create":
        if not name or not name.strip():
            raise ValueError("name is required")
        return _api("POST", path, body={"name": name.strip()})
    if group_id is None or group_id < 1:
        raise ValueError("group_id is required")
    path += f"/{group_id}"
    if action == "rename":
        if not name or not name.strip():
            raise ValueError("name is required")
        return _api("PATCH", path, body={"name": name.strip()})
    if action == "delete":
        return _api("DELETE", path)
    return _api("POST", path + "/senders/oauth/start")


@mcp.tool()
def manage_sender(
    sender_id: int,
    action: Literal["update", "set_default", "remove"] = "update",
    display_name: str | None = None,
    daily_cap: int | None = None,
    group_id: int | None = None,
) -> dict:
    """Edit a connected sender's display name, daily cap or group; make it default; or disconnect it."""
    if sender_id < 1:
        raise ValueError("sender_id must be positive")
    path = f"/api/senders/{sender_id}"
    if action == "set_default":
        return _api("PATCH", path + "/default")
    if action == "remove":
        return _api("DELETE", path)
    body = {key: value for key, value in {
        "display_name": display_name,
        "daily_cap": daily_cap,
        "group_id": group_id,
    }.items() if value is not None}
    if not body:
        raise ValueError("Provide a sender setting to update")
    return _api("PATCH", path, body=body)


@mcp.tool()
def manage_template(
    action: Literal["list", "create", "update", "delete"] = "list",
    template_id: int | None = None,
    title: str | None = None,
    subject: str | None = None,
    body: str | None = None,
) -> Any:
    """List, create, edit or delete reusable campaign message templates."""
    path = "/api/templates"
    if action == "list":
        return _api("GET", path)
    if action in {"update", "delete"}:
        if template_id is None or template_id < 1:
            raise ValueError("template_id is required")
        path += f"/{template_id}"
    if action == "delete":
        return _api("DELETE", path)
    if not title or not subject or not body:
        raise ValueError("title, subject and body are required")
    return _api("POST" if action == "create" else "PATCH", path, body={"title": title, "subject": subject, "body": body})


@mcp.tool()
def workspace_settings(
    timezone: str | None = None,
    max_daily_cap: int | None = None,
    bounce_rate_pause_threshold: float | None = None,
    max_consecutive_errors: int | None = None,
) -> dict:
    """Read all workspace safety settings, or update supplied values while preserving the others."""
    current = _api("GET", "/api/settings")
    supplied = {key: value for key, value in {
        "timezone": timezone,
        "max_daily_cap": max_daily_cap,
        "bounce_rate_pause_threshold": bounce_rate_pause_threshold,
        "max_consecutive_errors": max_consecutive_errors,
    }.items() if value is not None}
    if not supplied:
        return current
    _api("PATCH", "/api/settings", body={**current, **supplied})
    return _api("GET", "/api/settings")


@mcp.tool()
def manage_contact(
    action: Literal["create", "do_not_contact"],
    email: str,
    first_name: str = "",
    last_name: str = "",
    company: str = "",
) -> dict:
    """Save one contact, or add an email to the workspace do-not-contact list."""
    if action == "do_not_contact":
        return _api("POST", "/api/contacts/dnc", body={"email": email})
    return _api("POST", "/api/contacts", body={
        "email": email,
        "first_name": first_name,
        "last_name": last_name,
        "company": company,
    })


def _decode_attachment(content: str, filename: str) -> bytes:
    """Decode one attachment payload supplied as base64 or a data URL."""

    payload = content.strip()
    if payload.startswith("data:"):
        _, _, payload = payload.partition(",")
    try:
        raw = base64.b64decode(payload, validate=False)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"Attachment content is not valid base64: {filename}") from exc
    if not raw:
        raise ValueError(f"Attachment is empty: {filename}")
    return raw


@mcp.tool()
def manage_campaign_attachment(
    campaign_id: int,
    action: Literal["list", "add", "remove"],
    files: list[dict] | None = None,
    attachment_id: int | None = None,
) -> dict:
    """List, add or remove a campaign's email attachments.

    Add files as
    ``[{"filename": "cv.pdf", "content_base64": "<base64 or data URL>"}]``.
    The API's own limits apply: `.pdf .png .jpg .jpeg .gif .webp .txt .doc .docx`
    only, 10 MB per file and 20 MB per campaign, on an editable campaign.
    """
    path = _campaign_path(campaign_id, "/attachments")

    if action == "list":
        return {"campaign_id": campaign_id, "attachments": _api("GET", path)}

    if action == "remove":
        if attachment_id is None:
            raise ValueError("attachment_id is required to remove an attachment")
        _api("DELETE", f"{path}/{int(attachment_id)}")
        return {
            "campaign_id": campaign_id,
            "removed": int(attachment_id),
            "attachments": _api("GET", path),
        }

    if not files:
        raise ValueError("Provide at least one file to add")
    upload: list[tuple[str, tuple[str, bytes, str]]] = []
    for item in files:
        filename = str(item.get("filename") or "").strip().replace("\\", "/").rsplit("/", 1)[-1]
        if not filename:
            raise ValueError("Every file needs a filename")
        raw = _decode_attachment(str(item.get("content_base64") or ""), filename)
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        upload.append(("files", (filename, raw, content_type)))
    upload_result = _api("POST", path, files=upload)
    return {
        "campaign_id": campaign_id,
        "added": [item["filename"] for item in upload_result.get("attachments", [])][-len(upload):],
        "total_size_bytes": upload_result.get("total_size_bytes"),
        "attachments": _api("GET", path),
    }


if __name__ == "__main__":
    mcp.run()
