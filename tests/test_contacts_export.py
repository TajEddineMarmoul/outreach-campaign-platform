import csv
import io
import json
import sqlite3

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routers import contacts


def _connection():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE contacts (
            id INTEGER PRIMARY KEY,
            user_id TEXT NOT NULL,
            email_normalized TEXT NOT NULL,
            first_name TEXT DEFAULT '',
            last_name TEXT DEFAULT '',
            company_name TEXT DEFAULT '',
            custom_fields TEXT DEFAULT '{}',
            status TEXT NOT NULL
        );
        CREATE TABLE campaigns (id INTEGER PRIMARY KEY, user_id TEXT NOT NULL);
        CREATE TABLE campaign_recipients (
            campaign_id INTEGER NOT NULL,
            contact_id INTEGER NOT NULL,
            status TEXT NOT NULL
        );
        CREATE TABLE send_log (
            id INTEGER PRIMARY KEY,
            user_id TEXT NOT NULL,
            campaign_id INTEGER,
            contact_id INTEGER,
            recipient_id INTEGER,
            status TEXT NOT NULL,
            response_status TEXT,
            click_count INTEGER NOT NULL DEFAULT 0
        );
    """)
    conn.execute("INSERT INTO campaigns VALUES (1, 'owner'), (2, 'owner')")
    conn.executemany(
        "INSERT INTO contacts VALUES (?, ?, ?, ?, '', ?, '{}', ?)",
        [
            (1, "owner", "ready@example.test", "Ready", "Ready Co", "approved"),
            (2, "owner", "clicked@example.test", "Clicked", "=HYPERLINK('bad')", "sent"),
            (3, "owner", "answered@example.test", "Answered", "Reply Co", "replied"),
            (4, "owner", "bounced@example.test", "Bounced", "Bounce Co", "bounced"),
            (5, "owner", "other-campaign@example.test", "Other", "Other Co", "sent"),
            (99, "another-user", "private@example.test", "Private", "Private Co", "sent"),
        ],
    )
    conn.executemany(
        "INSERT INTO campaign_recipients VALUES (?, ?, ?)",
        [(1, 1, "approved"), (1, 2, "sent"), (1, 3, "sent"), (1, 4, "bounced"), (1, 5, "sent"), (2, 5, "sent")],
    )
    conn.executemany(
        "INSERT INTO send_log VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (1, "owner", 1, 2, 2, "sent", None, 2),
            (2, "owner", 1, 3, 3, "sent", "replied", 0),
            (3, "owner", 1, 4, 4, "bounced", None, 0),
            (4, "owner", 2, 5, 5, "sent", "replied", 0),
            (5, "another-user", 1, 99, 99, "sent", None, 5),
        ],
    )
    return conn


def test_contact_filters_support_multiple_statuses_and_campaign_scope(monkeypatch):
    conn = _connection()
    monkeypatch.setattr(contacts.db, "get_campaign", lambda _conn, campaign_id, user_id: {"id": campaign_id} if (campaign_id, user_id) in {(1, "owner"), (2, "owner")} else None)

    result = contacts.contact_library(
        search="", campaign_id=1, statuses=["clicked", "answered"],
        page=1, page_size=10, conn=conn, user_id="owner",
    )
    assert result["total"] == 2
    assert {item["id"] for item in result["items"]} == {2, 3}

    replied_elsewhere = contacts.contact_library(
        search="", campaign_id=1, statuses=["answered"],
        page=1, page_size=10, conn=conn, user_id="owner",
    )
    assert [item["id"] for item in replied_elsewhere["items"]] == [3]

    ready = contacts.contact_library(
        search="", campaign_id=1, statuses=["ready"],
        page=1, page_size=10, conn=conn, user_id="owner",
    )
    assert [item["id"] for item in ready["items"]] == [1]

    undelivered = contacts.contact_library(
        search="", campaign_id=1, statuses=["undelivered"],
        page=1, page_size=10, conn=conn, user_id="owner",
    )
    assert [item["id"] for item in undelivered["items"]] == [4]
    conn.close()


def test_contact_csv_export_contains_all_matches_and_activity_flags(monkeypatch):
    conn = _connection()
    monkeypatch.setattr(contacts.db, "get_campaign", lambda _conn, campaign_id, user_id: {"id": campaign_id} if (campaign_id, user_id) == (1, "owner") else None)

    response = contacts.export_contacts(
        search="", campaign_id=1, statuses=["clicked", "answered"], contact_ids=[],
        conn=conn, user_id="owner",
    )

    assert response.status_code == 200
    assert response.headers["content-disposition"] == 'attachment; filename="contacts.csv"'
    rows = list(csv.reader(io.StringIO(response.body.decode("utf-8-sig"))))
    assert rows[0][:8] == ["Name", "Email", "Company", "Contact status", "Answered", "Clicked", "Ready", "Undelivered"]
    assert [row[1] for row in rows[1:]] == ["answered@example.test", "clicked@example.test"]
    assert rows[1][4:8] == ["Yes", "No", "No", "No"]
    assert rows[2][2] == "'=HYPERLINK('bad')"
    assert rows[2][4:8] == ["No", "Yes", "No", "No"]
    conn.close()


def test_full_export_includes_every_stored_and_custom_field_from_exported_contacts():
    conn = _connection()
    conn.execute("ALTER TABLE contacts ADD COLUMN title TEXT DEFAULT ''")
    conn.execute("ALTER TABLE contacts ADD COLUMN country TEXT DEFAULT ''")
    custom = {
        "greeting": "Bonjour Salma,",
        "keyword1": "innovation",
        "keyword2": "digitalisation",
        "keyword3": "automatisation",
        "PERSONNE": "Salma El Idrissi",
        "TEL": "+212 600000000",
        "ADRESSE": "Casablanca, Maroc\nÉtage 2",
        "EMAIL_SOURCE": "PDF source",
        "EMAIL_CONFIDENCE": 0,
        "verified_online": False,
        "source_details": {"url": "https://example.test/RH.pdf", "page": 12},
    }
    conn.execute(
        "UPDATE contacts SET custom_fields = ?, title = ?, country = ? WHERE id = 1",
        (json.dumps(custom, ensure_ascii=False), "Responsable RH", "Maroc"),
    )
    conn.execute("UPDATE contacts SET custom_fields = ? WHERE id = 2", (json.dumps({"linkedin": "https://example.test/person"}),))
    conn.execute("UPDATE contacts SET custom_fields = ? WHERE id = 99", (json.dumps({"private_other_user_column": "private"}),))

    response = contacts.export_contacts(
        search="", campaign_id=None, statuses=[], contact_ids=[], conn=conn, user_id="owner",
    )
    reader = csv.DictReader(io.StringIO(response.body.decode("utf-8-sig")))
    rows = {row["Email"]: row for row in reader}
    row = rows["ready@example.test"]
    assert "user_id" not in reader.fieldnames
    assert "private_other_user_column" not in reader.fieldnames
    assert "private@example.test" not in rows
    assert row["title"] == "Responsable RH"
    assert row["country"] == "Maroc"
    assert row["greeting"] == "Bonjour Salma,"
    assert [row[f"keyword{i}"] for i in range(1, 4)] == ["innovation", "digitalisation", "automatisation"]
    assert row["ADRESSE"] == custom["ADRESSE"]
    assert row["TEL"] == "'+212 600000000"
    assert row["EMAIL_SOURCE"] == "PDF source"
    assert row["EMAIL_CONFIDENCE"] == "0"
    assert row["verified_online"] == "False"
    assert json.loads(row["source_details"]) == custom["source_details"]
    assert json.loads(row["custom_fields"]) == custom
    assert row["linkedin"] == ""
    assert rows["clicked@example.test"]["linkedin"] == "https://example.test/person"
    conn.close()


def test_full_export_preserves_colliding_headers_and_protects_custom_csv_cells():
    conn = _connection()
    custom = {
        "Name": "Imported name",
        "CSV: Name": "A separate source column",
        "first_name": "Imported first name",
        "CSV: first_name": "Another source column",
        "=column": "=HYPERLINK(\"https://example.test\")",
        "'=column": "A literal apostrophe column",
    }
    conn.execute("UPDATE contacts SET custom_fields = ? WHERE id = 1", (json.dumps(custom),))
    response = contacts.export_contacts(
        search="", campaign_id=None, statuses=[], contact_ids=[1], conn=conn, user_id="owner",
    )
    reader = csv.DictReader(io.StringIO(response.body.decode("utf-8-sig")))
    row = next(reader)
    assert len(reader.fieldnames) == len(set(reader.fieldnames))
    assert row["Name"] == "Ready"
    assert row["first_name"] == "Ready"
    assert row["CSV: Name (2)"] == "Imported name"
    assert row["CSV: Name"] == "A separate source column"
    assert row["CSV: first_name (2)"] == "Imported first name"
    assert row["CSV: first_name"] == "Another source column"
    assert row["'=column"] == "'=HYPERLINK(\"https://example.test\")"
    assert row["CSV: '=column"] == "A literal apostrophe column"
    assert json.loads(row["custom_fields"]) == custom
    conn.close()


def test_emails_only_export_respects_selection_campaign_and_multiple_statuses(monkeypatch):
    conn = _connection()
    monkeypatch.setattr(contacts.db, "get_campaign", lambda _conn, campaign_id, user_id: {"id": campaign_id} if (campaign_id, user_id) == (1, "owner") else None)
    response = contacts.export_contacts(
        search="example.test", campaign_id=1, statuses=["clicked", "undelivered"],
        contact_ids=[2, 4, 99, 2], include_data=False, conn=conn, user_id="owner",
    )
    assert response.headers["content-disposition"] == 'attachment; filename="contact-emails.csv"'
    assert list(csv.reader(io.StringIO(response.body.decode("utf-8-sig")))) == [
        ["Email"], ["bounced@example.test"], ["clicked@example.test"],
    ]
    conn.close()


def test_full_export_retains_malformed_custom_fields_without_crashing():
    conn = _connection()
    conn.execute("UPDATE contacts SET custom_fields = ? WHERE id = 1", ("not valid JSON",))
    response = contacts.export_contacts(
        search="", campaign_id=None, statuses=[], contact_ids=[1], conn=conn, user_id="owner",
    )
    row = next(csv.DictReader(io.StringIO(response.body.decode("utf-8-sig"))))
    assert row["Email"] == "ready@example.test"
    assert row["custom_fields"] == "not valid JSON"
    conn.close()


def test_export_api_exposes_full_data_by_default_and_emails_only_as_an_option():
    conn = _connection()
    conn.execute("UPDATE contacts SET custom_fields = ? WHERE id = 1", (json.dumps({"greeting": "Bonjour,"}),))
    app = FastAPI()
    app.include_router(contacts.router)
    app.dependency_overrides[contacts.get_db] = lambda: conn
    app.dependency_overrides[contacts.get_current_user_id] = lambda: "owner"
    client = TestClient(app)

    full = client.get("/api/contacts/export?contact_ids=1")
    assert full.status_code == 200
    full_row = next(csv.DictReader(io.StringIO(full.content.decode("utf-8-sig"))))
    assert full_row["greeting"] == "Bonjour,"

    emails = client.get("/api/contacts/export?contact_ids=1&include_data=false")
    assert emails.status_code == 200
    assert list(csv.reader(io.StringIO(emails.content.decode("utf-8-sig")))) == [["Email"], ["ready@example.test"]]
    assert client.get("/api/contacts/export?include_data=invalid").status_code == 422
    conn.close()


def test_contacts_api_accepts_repeated_status_filters(monkeypatch):
    conn = _connection()
    monkeypatch.setattr(contacts.db, "get_campaign", lambda _conn, campaign_id, user_id: {"id": campaign_id} if (campaign_id, user_id) == (1, "owner") else None)
    app = FastAPI()
    app.include_router(contacts.router)
    app.dependency_overrides[contacts.get_db] = lambda: conn
    app.dependency_overrides[contacts.get_current_user_id] = lambda: "owner"

    response = TestClient(app).get(
        "/api/contacts/library?campaign_id=1&statuses=clicked&statuses=answered&page_size=10"
    )

    assert response.status_code == 200
    assert response.json()["total"] == 2
    assert {item["email"] for item in response.json()["items"]} == {
        "clicked@example.test",
        "answered@example.test",
    }
    conn.close()
