from __future__ import annotations

import json

import pandas as pd
import requests
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

from api.routers import campaigns
from src import importer
from src.db.contact_repo import add_campaign_recipients
from src.models import ContactStatus, ImportResult


def test_import_and_attach_auto_detects_email_column(monkeypatch):
    frame = pd.DataFrame(
        [
            {
                "First Name": "Sandy",
                "Company Name": "Shopify",
                "Email": "TEST@example.com",
                "Languages": "English, French",
            }
        ]
    )
    captured: dict = {}

    def fake_import_dataframe(_frame, _conn, **kwargs):
        captured["mapping"] = kwargs["column_mapping"]
        return ImportResult(imported=1)

    def fake_attach(_conn, campaign_id, emails, user_id):
        captured["attachment"] = (campaign_id, emails, user_id)
        return 1

    monkeypatch.setattr(campaigns, "import_dataframe", fake_import_dataframe)
    monkeypatch.setattr(campaigns.db, "add_campaign_recipients_by_emails", fake_attach)

    result = campaigns.import_and_attach_df(
        object(),
        campaign_id=21,
        df=frame,
        mapping={},
        source_type="csv",
        user_id="user-1",
    )

    assert captured["mapping"]["email"] == "Email"
    assert captured["attachment"] == (21, ["test@example.com"], "user-1")
    assert result["attached"] == 1


def test_batch_import_validates_every_source_before_writing(monkeypatch):
    calls = []
    monkeypatch.setattr(campaigns, "import_and_attach_df", lambda *_args, **_kwargs: calls.append(True))

    with pytest.raises(HTTPException, match="email column"):
        campaigns.import_and_attach_frames(
            object(),
            campaign_id=21,
            frames=[pd.DataFrame([{"email": "alex@example.com"}]), pd.DataFrame([{"skill": "Design"}])],
            mappings=[{}, {}],
            source_type="csv",
            source_urls=["first.csv", "second.csv"],
            user_id="user-1",
        )

    assert calls == []


def test_private_blob_csv_batch_import_uses_original_filenames_and_removes_files(monkeypatch):
    captured = {}
    sources = [
        campaigns.RecipientBlobCsv(
            url="https://store.private.blob.vercel-storage.com/campaign-imports/user-1/21/design.csv",
            filename="design.csv",
        ),
        campaigns.RecipientBlobCsv(
            url="https://store.private.blob.vercel-storage.com/campaign-imports/user-1/21/engineering.csv",
            filename="engineering.csv",
        ),
    ]

    monkeypatch.setattr(campaigns, "require_editable_campaign", lambda *_args: None)
    monkeypatch.setattr(
        campaigns,
        "read_recipient_blob_csvs",
        lambda received_sources, campaign_id, user_id: [
            pd.DataFrame([{"email": "alex@example.com"}]),
            pd.DataFrame([{"email": "sam@example.com"}]),
        ],
    )
    monkeypatch.setattr(campaigns, "import_and_attach_frames", lambda *_args: {"attached": 2})
    monkeypatch.setattr(campaigns, "delete_recipient_blob_csvs", lambda received_sources: captured.setdefault("deleted", received_sources))

    result = campaigns.post_recipients_blob_csv_batch(
        21,
        campaigns.RecipientsBlobCsvBatch(files=sources),
        object(),
        "user-1",
    )

    assert result == {"attached": 2}
    assert [source.filename for source in captured["deleted"]] == ["design.csv", "engineering.csv"]


def test_private_blob_csv_batch_enforces_total_size(monkeypatch):
    source = campaigns.RecipientBlobCsv(
        url="https://store.private.blob.vercel-storage.com/campaign-imports/user-1/21/contacts.csv",
        filename="contacts.csv",
    )
    monkeypatch.setattr(
        campaigns,
        "read_recipient_blob_csv",
        lambda *_args: (pd.DataFrame([{"email": "alex@example.com"}]), campaigns.MAX_RECIPIENT_IMPORT_BATCH_BYTES + 1),
    )

    with pytest.raises(HTTPException, match="200 MB"):
        campaigns.read_recipient_blob_csvs([source], campaign_id=21, user_id="user-1")


def test_imported_contacts_are_approved_and_keep_every_csv_field(monkeypatch):
    frame = pd.DataFrame(
        [
            {
                "First Name": "Sandy",
                "First_Name": "Sandra",
                "Email": "sandy@example.com",
                "Languages": "English, French",
                "Organization Description": "Commerce platform",
            }
        ]
    )
    inserted: dict = {}

    monkeypatch.setattr(importer, "is_do_not_contact", lambda *_args: False)
    monkeypatch.setattr(importer.db, "fetch_contact_by_email", lambda *_args: None)

    def fake_insert(_conn, contact, user_id):
        inserted.update(contact)
        inserted["user_id"] = user_id
        return True

    monkeypatch.setattr(importer.db, "insert_contact", fake_insert)

    result = importer.import_dataframe(frame, object(), user_id="user-1")

    fields = json.loads(inserted["custom_fields"])
    assert result.imported == 1
    assert inserted["status"] == ContactStatus.APPROVED.value
    assert fields["First Name"] == "Sandy"
    assert fields["First_Name"] == "Sandra"
    assert fields["Languages"] == "English, French"
    assert fields["Organization Description"] == "Commerce platform"


class _Cursor:
    def __init__(self, row=None):
        self._row = row

    def fetchone(self):
        return self._row


class _RecipientConnection:
    def __init__(self, contact_status: str):
        self.contact_status = contact_status
        self.inserted_status: str | None = None

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        if normalized.startswith("SELECT status FROM contacts"):
            return _Cursor({"status": self.contact_status})
        if normalized.startswith("SELECT 1 FROM campaign_recipients"):
            return _Cursor(None)
        if normalized.startswith("INSERT INTO campaign_recipients"):
            self.inserted_status = params[2]
            return _Cursor()
        raise AssertionError(f"Unexpected SQL: {normalized}")

    def commit(self):
        return None


def test_previously_sent_contact_is_approved_for_a_new_campaign():
    conn = _RecipientConnection(ContactStatus.SENT.value)

    attached = add_campaign_recipients(conn, campaign_id=42, contact_ids=[7])

    assert attached == 1
    assert conn.inserted_status == ContactStatus.APPROVED.value


class _BulkCursor:
    def __init__(self, rows=None, rowcount=0):
        self._rows = rows or []
        self.rowcount = rowcount

    def fetchall(self):
        return self._rows


class _BulkImportConnection:
    supports_bulk_operations = True

    def __init__(self):
        self.executed: list[str] = []
        self.batches: list[tuple[str, list[dict]]] = []
        self.commits = 0

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        self.executed.append(normalized)
        if normalized.startswith("SELECT * FROM contacts"):
            return _BulkCursor()
        if normalized.startswith("SELECT email FROM do_not_contact"):
            return _BulkCursor()
        if normalized.startswith("UPDATE contacts"):
            return _BulkCursor()
        if normalized.startswith("INSERT INTO campaign_recipients"):
            return _BulkCursor(rowcount=len(params) - 4)
        raise AssertionError(f"Unexpected SQL: {normalized}")

    def executemany(self, sql, params, page_size=500):
        values = list(params)
        self.batches.append((" ".join(sql.split()), values))
        return _BulkCursor(rowcount=len(values))

    def commit(self):
        self.commits += 1


def test_500_recipient_import_uses_batched_database_operations():
    frame = pd.DataFrame(
        [
            {
                "First Name": f"Lead {index}",
                "First_Name": f"Distinct {index}",
                "Email": f"lead{index}@example.com",
            }
            for index in range(500)
        ]
    )
    conn = _BulkImportConnection()

    result = campaigns.import_and_attach_df(
        conn,
        campaign_id=78,
        df=frame,
        mapping={},
        source_type="google_sheet",
        user_id="user-1",
    )

    assert result["imported"] == 500
    assert result["attached"] == 500
    assert conn.commits == 2
    assert len(conn.batches) == 1
    assert len(conn.batches[0][1]) == 500
    assert len(conn.executed) == 4
    custom_fields = json.loads(conn.batches[0][1][0]["custom_fields"])
    assert custom_fields["First Name"] == "Lead 0"
    assert custom_fields["First_Name"] == "Distinct 0"


def test_google_sheet_timeout_returns_a_controlled_http_error(monkeypatch):
    monkeypatch.setattr(campaigns, "require_editable_campaign", lambda *_args: {"id": 78})
    monkeypatch.setattr(
        campaigns,
        "get_public_sheet_csv",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(requests.Timeout("slow")),
    )
    app = FastAPI()
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=".*",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(campaigns.router)
    app.dependency_overrides[campaigns.get_db] = lambda: object()
    app.dependency_overrides[campaigns.get_current_user_id] = lambda: "user-1"
    origin = "https://outreach-frontend-166059707324.us-central1.run.app"

    response = TestClient(app).post(
        "/api/campaigns/78/recipients/google-sheet",
        json={
            "url": "https://docs.google.com/spreadsheets/d/example-sheet-id/edit",
            "tab_name": "Sheet1",
            "header_row": 1,
            "mapping": {},
        },
        headers={"Origin": origin},
    )

    assert response.status_code == 504
    assert response.json()["detail"] == "Google Sheets did not respond within 120 seconds"
    assert response.headers["access-control-allow-origin"] == origin


def test_google_sheet_import_rejects_non_google_urls_without_fetching(monkeypatch):
    monkeypatch.setattr(
        campaigns,
        "get_public_sheet_csv",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not fetch")),
    )

    with pytest.raises(HTTPException, match="docs.google.com") as error:
        campaigns.read_recipient_sheet(
            campaigns.RecipientsGoogleSheet(
                url="http://169.254.169.254/latest/meta-data/?format=csv",
                tab_name="",
                header_row=1,
                mapping={},
            )
        )

    assert error.value.status_code == 422


def test_published_google_sheet_rebuilds_a_trusted_export_url(monkeypatch):
    captured = {}

    def read_published(sheet_id, *, gid, header_row):
        captured.update({"sheet_id": sheet_id, "gid": gid, "header_row": header_row})
        return pd.DataFrame([{"email": "alex@example.com"}])

    monkeypatch.setattr(campaigns, "get_published_csv", read_published)

    frame = campaigns.read_recipient_sheet(
        campaigns.RecipientsGoogleSheet(
            url="https://docs.google.com/spreadsheets/d/e/published-sheet/pub?output=csv&gid=17",
            tab_name="",
            header_row=2,
            mapping={},
        )
    )

    assert list(frame.columns) == ["email"]
    assert captured == {"sheet_id": "published-sheet", "gid": "17", "header_row": 2}


def test_add_one_existing_contact_preserves_saved_fields(monkeypatch):
    monkeypatch.setattr(campaigns, "is_do_not_contact", lambda *_: False)
    captured = {}
    monkeypatch.setattr(campaigns, "require_editable_campaign", lambda *_: None)
    monkeypatch.setattr(campaigns.db, "fetch_contact_by_email", lambda *_: {"id": 4, "first_name": "Maya", "custom_fields": {"company": "Acme"}})
    def attach(_conn, campaign_id, emails, user_id):
        captured["attached"] = (campaign_id, emails, user_id)
        return 1
    def must_not_import(*_args, **_kwargs):
        raise AssertionError("Adding a saved contact must not overwrite it")
    monkeypatch.setattr(campaigns.db, "add_campaign_recipients_by_emails", attach)
    monkeypatch.setattr(campaigns, "import_and_attach_df", must_not_import)
    result = campaigns.post_recipient_one(21, campaigns.SingleRecipientRequest(email="MAYA@example.com"), object(), "user-1")
    assert result["attached"] == 1
    assert captured["attached"] == (21, ["maya@example.com"], "user-1")


def test_add_one_new_contact_preserves_punctuation_and_uses_import_safety(monkeypatch):
    monkeypatch.setattr(campaigns, "is_do_not_contact", lambda *_: False)
    monkeypatch.setattr(campaigns, "require_editable_campaign", lambda *_: None)
    monkeypatch.setattr(campaigns.db, "fetch_contact_by_email", lambda *_: None)
    captured = {}
    def imported(_conn, _campaign, frame, _mapping, source, **kwargs):
        captured.update(frame.iloc[0].to_dict())
        captured["source"] = source
        return {"attached": 1}
    monkeypatch.setattr(campaigns, "import_and_attach_df", imported)
    campaigns.post_recipient_one(21, campaigns.SingleRecipientRequest(email="new@example.com", first_name=" Jo ", company='A, B & "Co"'), object(), "user-1")
    assert captured == {"email": "new@example.com", "first_name": "Jo", "company": 'A, B & "Co"', "source": "manual"}


def test_add_one_rejects_do_not_contact_addresses(monkeypatch):
    monkeypatch.setattr(campaigns, "require_editable_campaign", lambda *_: None)
    monkeypatch.setattr(campaigns, "is_do_not_contact", lambda *_: True)
    with pytest.raises(HTTPException) as error:
        campaigns.post_recipient_one(21, campaigns.SingleRecipientRequest(email="blocked@example.com"), object(), "user-1")
    assert error.value.status_code == 422
