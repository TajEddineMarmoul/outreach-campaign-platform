import csv
import io
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
    assert rows[0] == ["Name", "Email", "Company", "Contact status", "Answered", "Clicked", "Ready", "Undelivered"]
    assert [row[1] for row in rows[1:]] == ["answered@example.test", "clicked@example.test"]
    assert rows[1][4:8] == ["Yes", "No", "No", "No"]
    assert rows[2][2] == "'=HYPERLINK('bad')"
    assert rows[2][4:8] == ["No", "Yes", "No", "No"]
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
