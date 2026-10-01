"""The hosted MCP server must add and remove campaign attachments like the website.

The multipart wire format itself is exercised against the live deployment,
because a route registered after the application is built is not reachable
through the framework's compiled router.
"""

from __future__ import annotations

import base64

from outreach_mcp import server as mcp_server


def test_manage_campaign_attachment_sends_multipart_bytes(monkeypatch):
    """Add must upload real multipart content, and list/remove must target the API."""

    calls: list[dict] = []
    stored: list[dict] = []

    def fake_api(method, path, *, params=None, body=None, files=None):
        calls.append({"method": method, "path": path, "files": files})
        if method == "POST":
            for field, (filename, raw, content_type) in files or []:
                assert field == "files"
                stored.append({
                    "filename": filename,
                    "raw": raw,
                    "content_type": content_type,
                })
            return {
                "attachments": [
                    {"id": index + 1, "filename": item["filename"],
                     "content_type": item["content_type"],
                     "size_bytes": len(item["raw"]), "sha256": "0" * 64}
                    for index, item in enumerate(stored)
                ],
                "total_size_bytes": sum(len(i["raw"]) for i in stored),
            }
        if method == "DELETE":
            # Real API ids are assigned on insert, so remove by position.
            del stored[int(path.rsplit("/", 1)[-1]) - 1]
        return [
            {"id": index + 1, "filename": item["filename"],
             "content_type": item["content_type"], "size_bytes": len(item["raw"]),
             "sha256": "0" * 64}
            for index, item in enumerate(stored)
        ]

    monkeypatch.setattr(mcp_server, "_api", fake_api)

    payload = b"%PDF-1.4 fake cv bytes"
    encoded = base64.b64encode(payload).decode()

    added = mcp_server.manage_campaign_attachment(
        campaign_id=42,
        action="add",
        files=[{"filename": "cv.pdf", "content_base64": encoded}],
    )
    assert added["campaign_id"] == 42
    assert [a["filename"] for a in added["attachments"]] == ["cv.pdf"]
    assert stored[0]["raw"] == payload
    assert stored[0]["content_type"] == "application/pdf"

    listed = mcp_server.manage_campaign_attachment(campaign_id=42, action="list")
    assert listed["attachments"][0]["filename"] == "cv.pdf"

    removed = mcp_server.manage_campaign_attachment(
        campaign_id=42, action="remove", attachment_id=1
    )
    assert removed["removed"] == 1
    assert removed["attachments"] == []
    assert any(c["method"] == "DELETE" for c in calls)
    assert all("/api/campaigns/42/attachments" in c["path"] for c in calls)


def test_manage_campaign_attachment_accepts_data_urls(monkeypatch):
    """A data URL must decode the same as a bare base64 payload."""

    captured: dict = {}

    def fake_api(method, path, *, params=None, body=None, files=None):
        if method == "POST":
            captured["files"] = files
            return {"attachments": [], "total_size_bytes": 0}
        return []

    monkeypatch.setattr(mcp_server, "_api", fake_api)
    raw = b"hello attachment"
    data_url = "data:text/plain;base64," + base64.b64encode(raw).decode()

    mcp_server.manage_campaign_attachment(
        campaign_id=7,
        action="add",
        files=[{"filename": "notes.txt", "content_base64": data_url}],
    )
    _, (filename, content, content_type) = captured["files"][0]
    assert filename == "notes.txt"
    assert content == raw
    assert content_type == "text/plain"
