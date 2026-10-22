from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

from api.main import app
from src.platform import db as platform_db
from src.platform.email_tracking import prepare_tracked_email, record_click, record_open
from src.platform.models import (
    Base,
    Campaign,
    EmailTrackingEvent,
    EmailTrackingLink,
    SendLog,
    User,
)


USER_ID = "tracking-test-user"


def make_session():
    engine = create_engine(
        "sqlite://",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)()


def make_log(session):
    session.add(User(id=USER_ID, email="owner@example.com"))
    campaign = Campaign(user_id=USER_ID, name="Tracking campaign")
    session.add(campaign)
    session.flush()
    log = SendLog(
        user_id=USER_ID,
        campaign_id=campaign.id,
        recipient_email="lead@example.com",
        sender_email="owner@example.com",
        subject="Hello",
        body_snapshot="Hello",
        status="attempting",
    )
    session.add(log)
    session.flush()
    return log


def test_prepares_pixel_and_rewrites_only_web_links(monkeypatch):
    monkeypatch.setenv("TRACKING_BASE_URL", "https://track.example/")
    session = make_session()
    log = make_log(session)

    tracked = prepare_tracked_email(
        session,
        log,
        '<p>Hello <a href="https://example.com/offer?a=1&amp;b=2">offer</a> '
        '<a href="mailto:hello@example.com">email us</a></p>',
    )
    session.commit()

    assert tracked.enabled is True
    assert f"https://track.example/api/track/o/{log.tracking_token}.gif" in tracked.body
    assert "mailto:hello@example.com" in tracked.body
    assert "https://example.com/offer?a=1&amp;b=2" not in tracked.body
    link = session.scalar(select(EmailTrackingLink).where(EmailTrackingLink.send_log_id == log.id))
    assert link is not None
    assert link.destination_url == "https://example.com/offer?a=1&b=2"
    assert f"https://track.example/api/track/c/{link.token}" in tracked.body


def test_open_and_click_events_update_campaign_metrics(monkeypatch):
    monkeypatch.setenv("TRACKING_BASE_URL", "https://track.example")
    session = make_session()
    log = make_log(session)
    prepare_tracked_email(session, log, '<a href="https://example.com">Open</a>')
    session.commit()
    link = session.scalar(select(EmailTrackingLink).where(EmailTrackingLink.send_log_id == log.id))
    assert link is not None

    assert record_open(session, log.tracking_token or "") is True
    assert record_open(session, log.tracking_token or "") is True
    assert record_click(session, link.token) == "https://example.com"
    session.refresh(log)

    assert log.open_count == 2
    assert log.click_count == 1
    assert log.first_opened_at is not None
    assert log.last_opened_at is not None
    assert log.first_clicked_at is not None
    assert session.scalars(select(EmailTrackingEvent)).all()


def test_tracking_leaves_email_unchanged_without_a_public_origin(monkeypatch):
    monkeypatch.delenv("TRACKING_BASE_URL", raising=False)
    monkeypatch.delenv("BACKEND_URL", raising=False)
    session = make_session()
    log = make_log(session)

    tracked = prepare_tracked_email(session, log, "Plain text email")

    assert tracked.enabled is False
    assert tracked.body == "Plain text email"
    assert log.tracking_token is None


def test_public_pixel_and_redirect_endpoints_record_events(monkeypatch):
    monkeypatch.setenv("TRACKING_BASE_URL", "https://track.example")
    engine = create_engine(
        "sqlite://",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    session = factory()
    log = make_log(session)
    prepare_tracked_email(session, log, '<a href="https://example.com/path">Open</a>')
    session.commit()
    token = log.tracking_token
    link = session.scalar(select(EmailTrackingLink).where(EmailTrackingLink.send_log_id == log.id))
    session.close()

    def override_session():
        request_session = factory()
        try:
            yield request_session
        finally:
            request_session.close()

    app.dependency_overrides[platform_db.get_session] = override_session
    try:
        with TestClient(app) as client:
            pixel = client.get(f"/api/track/o/{token}.gif")
            click = client.get(f"/api/track/c/{link.token}", follow_redirects=False)
        assert pixel.status_code == 200
        assert pixel.headers["content-type"].startswith("image/gif")
        assert pixel.headers["cache-control"].startswith("no-store")
        assert click.status_code == 302
        assert click.headers["location"] == "https://example.com/path"
    finally:
        app.dependency_overrides.pop(platform_db.get_session, None)

    session = factory()
    persisted = session.get(SendLog, log.id)
    assert persisted.open_count == 1
    assert persisted.click_count == 1


def test_failed_delivery_preserves_history_but_excludes_engagement_metrics(monkeypatch):
    from api.routers.analytics import analytics
    from api.routers.campaign_delivery import get_campaign_send_logs, get_campaign_send_progress

    monkeypatch.setenv("TRACKING_BASE_URL", "https://track.example")
    session = make_session()
    log = make_log(session)
    log.status = "sent"
    prepare_tracked_email(session, log, '<a href="https://example.com">Open</a>')
    session.commit()
    link = session.scalar(select(EmailTrackingLink).where(EmailTrackingLink.send_log_id == log.id))
    record_open(session, log.tracking_token)
    record_click(session, link.token)
    log.status = "bounced"
    session.commit()

    # A returned original HTML message must not add more opens/clicks after failure.
    assert record_open(session, log.tracking_token) is True
    assert record_click(session, link.token) == "https://example.com"
    assert log.open_count == log.click_count == 1
    assert len(session.scalars(select(EmailTrackingEvent)).all()) == 2

    progress = get_campaign_send_progress(log.campaign_id, session=session, user_id=USER_ID)
    assert progress["opened_count"] == progress["clicked_count"] == 0
    report = analytics(days=7, page=1, session=session, user_id=USER_ID)
    assert report["opened"] == report["clicked"] == 0
    assert report["open_events"] == report["click_events"] == 0
    for engagement in ("opened", "clicked"):
        assert get_campaign_send_logs(log.campaign_id, engagement=engagement, session=session, user_id=USER_ID)["total"] == 0
    entries = get_campaign_send_logs(log.campaign_id, engagement="all", session=session, user_id=USER_ID)
    assert entries["total"] == 1
    assert entries["items"][0]["status"] == "bounced"
    assert entries["items"][0]["open_count"] == 1
    failed = get_campaign_send_logs(log.campaign_id, engagement="undelivered", session=session, user_id=USER_ID)
    assert failed["total"] == 1 and failed["items"][0]["status"] == "bounced"
