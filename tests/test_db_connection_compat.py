from pathlib import Path

from src import db


class NoTotalChangesConnection:
    def __init__(self, conn):
        self._conn = conn

    def execute(self, *args, **kwargs):
        return self._conn.execute(*args, **kwargs)

    def commit(self):
        return self._conn.commit()


def test_add_campaign_recipients_does_not_require_sqlite_total_changes(tmp_path: Path):
    conn = db.init_db(tmp_path / "compat.db")
    user_id = "compat_user"
    campaign_id = db.create_campaign(conn, user_id, "Compat Campaign")
    db.insert_contact(conn, {"email": "compat@example.com", "company_name": "Acme"}, user_id)
    contact = db.fetch_contact_by_email(conn, "compat@example.com", user_id)

    wrapped = NoTotalChangesConnection(conn)

    assert db.add_campaign_recipients(wrapped, campaign_id, [contact["id"]], user_id) == 1
    assert db.add_campaign_recipients(wrapped, campaign_id, [contact["id"]], user_id) == 0


def test_set_contacts_status_does_not_require_sqlite_total_changes(tmp_path: Path):
    conn = db.init_db(tmp_path / "status_compat.db")
    user_id = "compat_user"
    db.insert_contact(conn, {"email": "status@example.com", "company_name": "Acme"}, user_id)
    contact = db.fetch_contact_by_email(conn, "status@example.com", user_id)

    wrapped = NoTotalChangesConnection(conn)

    assert db.set_contacts_status(wrapped, [contact["id"]], "approved", user_id) == 1
    updated = db.fetch_contact(conn, contact["id"], user_id)
    assert updated["status"] == "approved"
