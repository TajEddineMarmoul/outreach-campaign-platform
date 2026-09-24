from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from zoneinfo import ZoneInfo

from src import db
from src.dnc import add_email
from src.models import AppConfig, ContactStatus
from src.safety import is_allowed_send_time, pre_send_checks


def config_with_attachment(tmp_path: Path) -> tuple[AppConfig, Path]:
    cv_path = tmp_path / "cv.pdf"
    cv_path.write_bytes(b"%PDF-1.4\n")
    config = AppConfig()
    config.timezone = "Europe/Paris"
    config.sending.days = ["monday", "tuesday", "wednesday", "thursday", "friday"]
    config.sending.start_time = "09:00"
    config.sending.end_time = "17:00"
    config.sending.delay_minutes = 10
    config.sending.daily_cap = 10
    config.campaign.attachment_path = str(cv_path)
    return config, cv_path


def approved_previewed_contact(conn, email: str = "lead@example.com"):
    db.insert_contact(
        conn,
        {
            "first_name": "Lead",
            "email": email,
            "company_name": "Company",
            "keyword_1": "ai",
            "status": ContactStatus.APPROVED.value,
        },
        user_id="test_user",
    )
    contact = db.fetch_contact_by_email(conn, email, user_id="test_user")
    db.mark_preview_generated(conn, int(contact["id"]), "Subject", "Body", user_id="test_user")
    return db.fetch_contact_by_email(conn, email, user_id="test_user")


def update_campaign_attachment(conn, path: Path):
    campaign_id = db.create_campaign(conn, user_id="test_user", name="Test Campaign")
    campaign = db.get_campaign(conn, campaign_id, user_id="test_user")
    db.update_campaign(
        conn,
        int(campaign["id"]),
        user_id="test_user",
        subject_template=str(campaign["subject_template"]),
        body_template=str(campaign["body_template"]),
        fallback_body_template=str(campaign["fallback_body_template"]),
        attachment_path=str(path),
    )
    return db.get_campaign(conn, campaign_id, user_id="test_user")


def test_do_not_contact_skips_send(tmp_path: Path) -> None:
    conn = db.init_db(tmp_path / "outreach.db")
    config, cv_path = config_with_attachment(tmp_path)
    campaign = update_campaign_attachment(conn, cv_path)
    contact = approved_previewed_contact(conn)
    add_email(conn, "lead@example.com", user_id="test_user", reason="Asked not to contact")
    contact = db.fetch_contact_by_email(conn, "lead@example.com", user_id="test_user")

    result = pre_send_checks(
        conn,
        contact,
        campaign,
        config,
        user_id="test_user",
        now=datetime(2026, 7, 7, 10, 0, tzinfo=ZoneInfo("Europe/Paris")),
    )

    assert result.allowed is False
    assert "do-not-contact" in result.reason


def test_already_sent_contact_is_skipped(tmp_path: Path) -> None:
    conn = db.init_db(tmp_path / "outreach.db")
    config, cv_path = config_with_attachment(tmp_path)
    campaign = update_campaign_attachment(conn, cv_path)
    contact = approved_previewed_contact(conn)
    db.set_contact_status(conn, int(contact["id"]), ContactStatus.SENT.value, user_id="test_user")
    contact = db.fetch_contact_by_email(conn, "lead@example.com", user_id="test_user")

    result = pre_send_checks(
        conn,
        contact,
        campaign,
        config,
        user_id="test_user",
        now=datetime(2026, 7, 7, 10, 0, tzinfo=ZoneInfo("Europe/Paris")),
    )

    assert result.allowed is False


def test_daily_cap_is_enforced_with_warmup(tmp_path: Path) -> None:
    conn = db.init_db(tmp_path / "outreach.db")
    config, cv_path = config_with_attachment(tmp_path)
    campaign = update_campaign_attachment(conn, cv_path)
    contact = approved_previewed_contact(conn)

    for index in range(5):
        log_id = db.create_send_attempt(
            conn,
            None,
            int(campaign["id"]),
            f"sent{index}@example.com",
            "Subject",
            "Body",
            "",
            user_id="test_user",
            status="sent",
        )
        db.update_send_log(
            conn,
            log_id,
            "sent",
            sent_at=datetime(2026, 7, 7, 9, index, tzinfo=timezone.utc).isoformat(),
        )

    result = pre_send_checks(
        conn,
        contact,
        campaign,
        config,
        user_id="test_user",
        now=datetime(2026, 7, 7, 12, 0, tzinfo=ZoneInfo("Europe/Paris")),
    )

    assert result.allowed is False
    assert result.reason == "Daily cap reached"


def test_time_window_is_enforced(tmp_path: Path) -> None:
    conn = db.init_db(tmp_path / "outreach.db")
    config, cv_path = config_with_attachment(tmp_path)
    campaign = update_campaign_attachment(conn, cv_path)
    contact = approved_previewed_contact(conn)

    assert is_allowed_send_time(
        config, datetime(2026, 7, 7, 10, 0, tzinfo=ZoneInfo("Europe/Paris"))
    )
    result = pre_send_checks(
        conn,
        contact,
        campaign,
        config,
        user_id="test_user",
        now=datetime(2026, 7, 7, 20, 0, tzinfo=ZoneInfo("Europe/Paris")),
    )

    assert result.allowed is False
    assert result.reason == "Outside allowed sending window"


def test_legacy_attachment_path_is_ignored(tmp_path: Path) -> None:
    conn = db.init_db(tmp_path / "outreach.db")
    config = AppConfig()
    missing = tmp_path / "missing.pdf"
    missing = tmp_path / "missing.pdf"
    config.campaign.attachment_path = str(missing)
    campaign = update_campaign_attachment(conn, missing)
    contact = approved_previewed_contact(conn)

    result = pre_send_checks(
        conn,
        contact,
        campaign,
        config,
        user_id="test_user",
        now=datetime(2026, 7, 7, 10, 0, tzinfo=ZoneInfo("Europe/Paris")),
    )

    assert result.allowed is True


def test_resume_after_crash_does_not_resend_attempted_contact(tmp_path: Path) -> None:
    conn = db.init_db(tmp_path / "outreach.db")
    config, cv_path = config_with_attachment(tmp_path)
    campaign = update_campaign_attachment(conn, cv_path)
    contact = approved_previewed_contact(conn)
    db.create_send_attempt(
        conn,
        int(contact["id"]),
        int(campaign["id"]),
        str(contact["email"]),
        "Subject",
        "Body",
        cv_path.name,
        user_id="test_user",
        status="attempting",
    )

    result = pre_send_checks(
        conn,
        contact,
        campaign,
        config,
        user_id="test_user",
        now=datetime(2026, 7, 7, 10, 0, tzinfo=ZoneInfo("Europe/Paris")),
    )

    assert result.allowed is False
    assert "send attempt" in result.reason
