import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routers import campaigns


@pytest.fixture
def preview_client(monkeypatch):
    def require_campaign(_conn, campaign_id, user_id):
        assert campaign_id == 42
        assert user_id == "preview-user"

    def must_not_import(*_args, **_kwargs):
        raise AssertionError("A preview must never import contacts")

    monkeypatch.setattr(campaigns, "require_editable_campaign", require_campaign)
    monkeypatch.setattr(campaigns, "import_and_attach_df", must_not_import)
    app = FastAPI()
    app.include_router(campaigns.router)
    app.dependency_overrides[campaigns.get_db] = lambda: object()
    app.dependency_overrides[campaigns.get_current_user_id] = lambda: "preview-user"
    return TestClient(app)


def test_pasted_spreadsheet_preview_preserves_arbitrary_columns(preview_client):
    response = preview_client.post(
        "/api/campaigns/42/recipients/preview/paste",
        json={"raw": "email\tskill\tregion\nalex@example.com\tDesign\tNA\nsam@example.com\tEngineering\tParis"},
    )
    assert response.status_code == 200
    assert response.json() == {
        "columns": ["email", "skill", "region"],
        "email_column": "email",
        "total_rows": 2,
        "rows": [
            {"email": "alex@example.com", "skill": "Design", "region": "NA"},
            {"email": "sam@example.com", "skill": "Engineering", "region": "Paris"},
        ],
    }


def test_csv_preview_reads_quoted_fields_and_limits_sample(preview_client):
    raw = "email,custom note\n" + "\n".join(f'person{i}@example.com,"Hello, {i}"' for i in range(8))
    response = preview_client.post(
        "/api/campaigns/42/recipients/preview/csv",
        files={"file": ("contacts.csv", raw.encode(), "text/csv")},
    )
    assert response.status_code == 200
    assert response.json()["total_rows"] == 8
    assert len(response.json()["rows"]) == 5
    assert response.json()["rows"][0]["custom note"] == "Hello, 0"


def test_csv_batch_preview_combines_selected_files(preview_client):
    response = preview_client.post(
        "/api/campaigns/42/recipients/preview/csv/batch",
        files=[
            ("files", ("design.csv", b"email,skill\nalex@example.com,Design", "text/csv")),
            ("files", ("engineering.csv", b"email,region\nsam@example.com,Paris", "text/csv")),
        ],
    )

    assert response.status_code == 200
    assert response.json() == {
        "columns": ["email", "skill", "region"],
        "email_column": "email",
        "total_rows": 2,
        "rows": [
            {"email": "alex@example.com", "skill": "Design", "region": ""},
            {"email": "sam@example.com", "skill": "", "region": "Paris"},
        ],
    }


def test_private_blob_csv_preview_reads_selected_files(preview_client, monkeypatch):
    captured = []

    def read_blob(source, campaign_id, user_id):
        captured.append((source.filename, campaign_id, user_id))
        return pd.DataFrame([{"email": "alex@example.com", "skill": "Design"}]), 42

    monkeypatch.setattr(campaigns, "read_recipient_blob_csv", read_blob)
    response = preview_client.post(
        "/api/campaigns/42/recipients/preview/csv/blob",
        json={
            "files": [{
                "url": "https://store.private.blob.vercel-storage.com/campaign-imports/preview-user/42/design.csv",
                "filename": "design.csv",
            }],
        },
    )

    assert response.status_code == 200
    assert response.json()["total_rows"] == 1
    assert captured == [("design.csv", 42, "preview-user")]


def test_private_blob_csv_preview_rejects_other_storage_urls(preview_client):
    response = preview_client.post(
        "/api/campaigns/42/recipients/preview/csv/blob",
        json={"files": [{"url": "https://example.com/contacts.csv", "filename": "contacts.csv"}]},
    )

    assert response.status_code == 422


def test_private_blob_csv_preview_rejects_another_users_path(preview_client):
    response = preview_client.post(
        "/api/campaigns/42/recipients/preview/csv/blob",
        json={"files": [{
            "url": "https://store.private.blob.vercel-storage.com/campaign-imports/other-user/42/contacts.csv",
            "filename": "contacts.csv",
        }]},
    )

    assert response.status_code == 422


@pytest.mark.parametrize("raw", ["skill,region\nDesign,London", "email,skill\n"])
def test_preview_rejects_missing_email_header_or_empty_rows(preview_client, raw):
    response = preview_client.post("/api/campaigns/42/recipients/preview/paste", json={"raw": raw})
    assert response.status_code == 422


def test_sheet_preview_reads_the_requested_tab_without_importing(preview_client, monkeypatch):
    captured = {}

    def read_sheet(sheet_id, **kwargs):
        captured.update({"sheet_id": sheet_id, **kwargs})
        return pd.DataFrame([{"email": "alex@example.com", "specialty": "Design"}])

    monkeypatch.setattr(campaigns, "get_public_sheet_csv", read_sheet)
    response = preview_client.post(
        "/api/campaigns/42/recipients/preview/google-sheet",
        json={"url": "https://docs.google.com/spreadsheets/d/example-sheet/edit#gid=17", "tab_name": "Designers", "header_row": 1, "mapping": {}},
    )
    assert response.status_code == 200
    assert response.json()["columns"] == ["email", "specialty"]
    assert captured == {"sheet_id": "example-sheet", "gid": "17", "header_row": 1, "sheet_name": "Designers"}


def test_sheet_preview_does_not_claim_private_sheet_support(preview_client):
    response = preview_client.post(
        "/api/campaigns/42/recipients/preview/google-sheet",
        json={"url": "https://docs.google.com/spreadsheets/d/example-sheet/edit", "tab_name": "", "header_row": 1, "mapping": {}, "use_private": True},
    )
    assert response.status_code == 400


def test_sheet_batch_preview_combines_sheet_links(preview_client, monkeypatch):
    def read_sheet(sheet_id, **_kwargs):
        if sheet_id == "design-sheet":
            return pd.DataFrame([{"email": "alex@example.com", "skill": "Design"}])
        return pd.DataFrame([{"email": "sam@example.com", "region": "Paris"}])

    monkeypatch.setattr(campaigns, "get_public_sheet_csv", read_sheet)
    response = preview_client.post(
        "/api/campaigns/42/recipients/preview/google-sheet/batch",
        json={
            "sheets": [
                {"url": "https://docs.google.com/spreadsheets/d/design-sheet/edit", "tab_name": "", "header_row": 1, "mapping": {}},
                {"url": "https://docs.google.com/spreadsheets/d/engineering-sheet/edit", "tab_name": "", "header_row": 1, "mapping": {}},
            ],
        },
    )

    assert response.status_code == 200
    assert response.json()["total_rows"] == 2
    assert response.json()["columns"] == ["email", "skill", "region"]
