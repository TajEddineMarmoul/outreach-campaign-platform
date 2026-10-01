"""The hosted MCP server must add and remove campaign attachments like the website.

The multipart wire format itself is exercised against the live deployment,
because a route registered after the application is built is not reachable
through the framework's compiled router.
"""

from __future__ import annotations

import base64

from outreach_mcp import server as mcp_server


class _RecordingClient:
    """Stands in for httpx and rejects the argument combination httpx rejects."""

    def __init__(self):
        self.calls: list[dict] = []

    def request(self, method, path, **kwargs):
        if kwargs.get("json") is not None and kwargs.get("files") is not None:
            raise TypeError("json and files cannot be combined")

        class _Response:
            is_redirect = False
            is_success = True
            status_code = 200
            content = b"{}"

            @staticmethod
            def json():
                return {"ok": True}

        self.calls.append({"method": method, "path": path, **kwargs})
        return _Response()


def test_api_never_combines_json_with_files(monkeypatch):
    """An upload must send multipart alone; httpx rejects json plus files."""

    client = _RecordingClient()
    monkeypatch.setattr(mcp_server, "_client", lambda: client)

    mcp_server._api("POST", "/api/campaigns/1/attachments",
                    files=[("files", ("a.txt", b"x", "text/plain"))])
    upload_call = client.calls[-1]
    assert upload_call["files"] is not None
    assert "json" not in upload_call

    # A normal JSON call still sends its body.
    mcp_server._api("POST", "/api/campaigns", body={"name": "draft"})
    json_call = client.calls[-1]
    assert json_call["json"] == {"name": "draft"}
    assert json_call["files"] is None


def test_multipart_encoding_survives_a_real_http_round_trip():
    """Upload bytes must survive multipart encoding and decoding.

    The receiving side is a plain Starlette route defined outside this module,
    because ``from __future__ import annotations`` turns a locally defined
    route's parameter annotations into unresolvable forward references.
    """

    from fastapi.testclient import TestClient

    from tests.multipart_receiver import build_receiver

    payload = b"%PDF-1.4 attachment bytes"
    with TestClient(build_receiver()) as client:
        response = client.post("/upload", files={"file": ("cv.pdf", payload, "application/pdf")})

    assert response.status_code == 200
    assert response.json() == {
        "filename": "cv.pdf",
        "content_type": "application/pdf",
        "content": payload.decode(),
    }


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
