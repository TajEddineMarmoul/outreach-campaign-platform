from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker
from fastapi.testclient import TestClient

from api.main import app
from api.routers import campaign_delivery, oauth as oauth_router, settings as settings_router
from src.platform import db as platform_db
from src.platform import jobs as platform_jobs
from src.platform import oauth as platform_oauth
from src.platform import scheduler as platform_scheduler
from src.platform import services as platform_services
from src.platform import worker as platform_worker
from src.platform.jobs import WEEKDAY_NAMES, create_send_jobs_for_next_batch
from src.platform.models import AutopilotDaySchedule, Base, Campaign, CampaignAttachment, CampaignRecipient, Contact, OAuthState, Sender, SenderGroup, SendJob, SendLog, UserSettings
from src.platform.security import decrypt_text, encrypt_text
from src.platform.services import ensure_user
from src.platform.time import utcnow
from src.db.contact_repo import insert_contact
from src.db.campaign_repo import create_template


USER_ID = "mock_app_user"
HEADERS = {"Authorization": f"Bearer {USER_ID}"}


@pytest.fixture(autouse=True)
def legacy_delivery_regressions(monkeypatch):
    # This suite retains the pre-coordinator contract as a rollback regression.
    # The default-on coordinator has its own end-to-end delivery suite.
    monkeypatch.setenv("OUTREACH_SHARED_DISPATCH", "false")


def all_day_schedule(cap: int = 10) -> dict[str, dict[str, object]]:
    return {
        day: {"cap": cap, "start": "00:00", "end": "23:59"}
        for day in WEEKDAY_NAMES
    }


def make_session_factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'app.db'}", future=True, connect_args={"check_same_thread": False})
    # Large batch tests spill SQLite statement journals to TEMP by default.
    # Keep that scratch space in memory on hosts without a writable OS temp dir.
    event.listen(engine, "connect", lambda connection, _record: connection.execute("PRAGMA temp_store=MEMORY"))
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False, future=True)


def install_session_override(session_factory):
    def override_session():
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[platform_db.get_session] = override_session


def clear_session_override():
    app.dependency_overrides.pop(platform_db.get_session, None)


def test_contact_insert_uses_pending_status_without_implicit_imports():
    connection = MagicMock()
    assert insert_contact(connection, {"email": "lead@example.com"}, USER_ID) is True
    inserted_data = connection.execute.call_args.args[1]
    assert inserted_data["status"] == "pending"
    assert inserted_data["user_id"] == USER_ID
    assert inserted_data["email_normalized"] == "lead@example.com"


def test_template_insert_uses_database_cursor_id():
    connection = MagicMock()
    connection.execute.return_value.lastrowid = 41

    assert create_template(connection, USER_ID, "Title", "Subject", "Body") == 41
    assert connection.execute.call_count == 1
    connection.commit.assert_called_once_with()


def test_sender_group_can_hold_multiple_senders_and_delete_clears_credentials(tmp_path):
    session_factory = make_session_factory(tmp_path)
    install_session_override(session_factory)
    try:
        client = TestClient(app)
        group_res = client.post("/api/sender-groups", json={"name": "Primary"}, headers=HEADERS)
        assert group_res.status_code == 200
        group_id = group_res.json()["id"]

        session = session_factory()
        ensure_user(session, USER_ID)
        session.add_all(
            [
                Sender(
                    user_id=USER_ID,
                    group_id=group_id,
                    email="one@example.com",
                    display_name="One",
                    status="connected",
                    daily_cap=10,
                    encrypted_oauth_credentials="encrypted-1",
                    scopes=["https://www.googleapis.com/auth/gmail.send"],
                    connected_at=utcnow(),
                ),
                Sender(
                    user_id=USER_ID,
                    group_id=group_id,
                    email="two@example.com",
                    display_name="Two",
                    status="connected",
                    daily_cap=10,
                    encrypted_oauth_credentials="encrypted-2",
                    scopes=["https://www.googleapis.com/auth/gmail.send"],
                    connected_at=utcnow(),
                ),
            ]
        )
        session.commit()
        session.close()

        groups = client.get("/api/sender-groups", headers=HEADERS).json()
        assert groups[0]["connected_sender_count"] == 2
        assert {sender["email"] for sender in groups[0]["senders"]} == {"one@example.com", "two@example.com"}

        sender_id = groups[0]["senders"][0]["id"]
        delete_res = client.delete(f"/api/senders/{sender_id}", headers=HEADERS)
        assert delete_res.status_code == 200

        session = session_factory()
        removed = session.get(Sender, sender_id)
        assert removed.status == "removed"
        assert removed.encrypted_oauth_credentials is None
        session.close()

        groups = client.get("/api/sender-groups", headers=HEADERS).json()
        assert len(groups[0]["senders"]) == 1

        second_sender_id = groups[0]["senders"][0]["id"]
        assert client.delete(f"/api/senders/{second_sender_id}", headers=HEADERS).status_code == 200
        delete_group = client.delete(f"/api/sender-groups/{group_id}", headers=HEADERS)
        assert delete_group.status_code == 200

        # The authenticated MCP catch-all owns unmatched URLs. Check that the
        # retired endpoint itself is absent rather than depending on its 404/405.
        assert not any(
            getattr(route, "path", None) == "/api/senders/connect"
            and "POST" in getattr(route, "methods", set())
            for route in app.routes
        )
    finally:
        clear_session_override()


def test_campaign_selects_sender_group_not_sender(tmp_path):
    session_factory = make_session_factory(tmp_path)
    install_session_override(session_factory)
    try:
        client = TestClient(app)
        group_id = client.post("/api/sender-groups", json={"name": "Outbound"}, headers=HEADERS).json()["id"]

        res = client.patch(
            "/api/campaigns/19/sender-group",
            json={"sender_group_id": group_id},
            headers=HEADERS,
        )
        assert res.status_code == 200
        assert res.json()["sender_group"]["id"] == group_id

        session = session_factory()
        campaign = session.get(Campaign, 19)
        assert campaign.selected_sender_group_id == group_id
        assert campaign.user_id == USER_ID
        session.close()
    finally:
        clear_session_override()


def test_delivery_validation_uses_exact_csv_header_names(tmp_path):
    session_factory = make_session_factory(tmp_path)
    session = session_factory()
    ensure_user(session, USER_ID)
    campaign = Campaign(
        user_id=USER_ID,
        name="Exact headers",
        subject_template="{{ First Name }} / {{ First_Name }}",
        body_template="Body",
    )
    contact = Contact(
        user_id=USER_ID,
        email_normalized="lead@example.com",
        status="approved",
        custom_fields={"First Name": "Sandy", "First_Name": "Sandra"},
    )
    session.add_all([campaign, contact])
    session.flush()
    session.add(CampaignRecipient(campaign_id=campaign.id, contact_id=contact.id))
    session.commit()

    campaign_delivery.require_known_template_variables(session, campaign)
    campaign.subject_template = "{{ First-Name }}"

    with pytest.raises(HTTPException) as exc_info:
        campaign_delivery.require_known_template_variables(session, campaign)

    assert getattr(exc_info.value, "status_code", None) == 409
    assert "First-Name" in str(getattr(exc_info.value, "detail", ""))
    session.close()


def test_send_now_reports_and_skips_rows_with_missing_template_values(tmp_path):
    session_factory = make_session_factory(tmp_path)
    install_session_override(session_factory)
    try:
        session = session_factory()
        ensure_user(session, USER_ID)
        group = SenderGroup(user_id=USER_ID, name="Template validation")
        session.add(group)
        session.flush()
        session.add(
            Sender(
                user_id=USER_ID,
                group_id=group.id,
                email="sender@example.com",
                display_name="Sender",
                status="connected",
                daily_cap=10,
                encrypted_oauth_credentials="encrypted",
                scopes=["https://www.googleapis.com/auth/gmail.send"],
                connected_at=utcnow(),
            )
        )
        campaign = Campaign(
            user_id=USER_ID,
            selected_sender_group_id=group.id,
            name="Missing row values",
            subject_template="Hello {{First Name}}",
            body_template="{{keyword_3}} at {{Company Name}}",
            fallback_body_template="Fallback",
            status="draft",
        )
        contacts = [
            Contact(
                user_id=USER_ID,
                email_normalized="ready@example.com",
                status="approved",
                custom_fields={"First Name": "Ready", "keyword_3": "AI", "Company Name": "Acme"},
            ),
            Contact(
                user_id=USER_ID,
                email_normalized="missing-keyword@example.com",
                status="approved",
                custom_fields={"First Name": "Keyword", "Company Name": "Beta"},
            ),
            Contact(
                user_id=USER_ID,
                email_normalized="missing-company@example.com",
                status="approved",
                custom_fields={"First Name": "Company", "keyword_3": "Data", "Company Name": ""},
            ),
            Contact(
                user_id=USER_ID,
                email_normalized="missing-both@example.com",
                status="approved",
                custom_fields={"First Name": "Both", "keyword_3": "  "},
            ),
        ]
        session.add_all([campaign, *contacts])
        session.flush()
        session.add_all(
            CampaignRecipient(campaign_id=campaign.id, contact_id=contact.id, status="approved")
            for contact in contacts
        )
        session.commit()
        campaign_id = campaign.id
        contact_ids = [contact.id for contact in contacts]
        session.close()

        client = TestClient(app)
        preview = client.get(
            f"/api/campaigns/{campaign_id}/recipient-template-validation",
            headers=HEADERS,
        )
        assert preview.status_code == 200
        assert preview.json() == {
            "checked_recipient_count": 4,
            "ready_recipient_count": 1,
            "skipped_recipient_count": 3,
            "overlap_recipient_count": 1,
            "missing_by_variable": [
                {"variable": "keyword_3", "row_count": 2},
                {"variable": "Company Name", "row_count": 2},
            ],
        }

        session = session_factory()
        assert all(
            session.get(
                CampaignRecipient,
                {"campaign_id": campaign_id, "contact_id": contact_id},
            ).status == "approved"
            for contact_id in contact_ids
        )
        session.close()

        response = client.post(
            f"/api/campaigns/{campaign_id}/send-now",
            json={"delay_minutes": 2},
            headers=HEADERS,
        )
        assert response.status_code == 200
        assert response.json()["queued"] == 1
        assert response.json()["recipient_validation"] == preview.json()

        session = session_factory()
        assert session.get(
            CampaignRecipient,
            {"campaign_id": campaign_id, "contact_id": contact_ids[0]},
        ).status == "queued"
        assert [
            session.get(
                CampaignRecipient,
                {"campaign_id": campaign_id, "contact_id": contact_id},
            ).status
            for contact_id in contact_ids[1:]
        ] == ["rejected", "rejected", "rejected"]
        assert session.get(Campaign, campaign_id).send_settings["recipient_validation"] == preview.json()
        session.close()

        progress = client.get(f"/api/campaigns/{campaign_id}/send-progress", headers=HEADERS)
        assert progress.status_code == 200
        assert progress.json()["skipped_count"] == 3
        assert progress.json()["recipient_validation"] == preview.json()
    finally:
        clear_session_override()


def test_exact_500_recipient_campaign_never_queues_incomplete_rows(tmp_path):
    session_factory = make_session_factory(tmp_path)
    install_session_override(session_factory)
    try:
        session = session_factory()
        ensure_user(session, USER_ID)
        group = SenderGroup(user_id=USER_ID, name="500-recipient validation")
        session.add(group)
        session.flush()
        session.add_all(
            Sender(
                user_id=USER_ID,
                group_id=group.id,
                email=f"sender{index}@example.com",
                display_name=f"Sender {index}",
                status="connected",
                daily_cap=500,
                encrypted_oauth_credentials=f"encrypted-{index}",
                scopes=["https://www.googleapis.com/auth/gmail.send"],
                connected_at=utcnow(),
            )
            for index in range(20)
        )
        campaign = Campaign(
            user_id=USER_ID,
            selected_sender_group_id=group.id,
            name="Exact production-size campaign",
            subject_template="Junior technical role at {{Company Name}}",
            body_template=(
                "Hi {{First Name}},\n\n"
                "I found your LinkedIn profile while looking at {{Company Name}}, "
                "and I liked that the company focuses on {{keyword_1}}, "
                "{{keyword_2}}, and {{keyword_3}}."
            ),
            fallback_body_template="Fallback",
            status="draft",
        )
        session.add(campaign)
        session.flush()

        contacts = []
        expected_invalid_ids: set[int] = set()
        for index in range(500):
            custom_fields = {
                "First Name": f"Lead {index}",
                "Company Name": f"Company {index}",
                "keyword_1": "software engineering",
                "keyword_2": "AI systems",
                "keyword_3": "data platforms",
            }
            if 400 <= index < 450:
                custom_fields["keyword_3"] = ""
            elif 450 <= index < 475:
                custom_fields["Company Name"] = "  "
            elif index >= 475:
                custom_fields.pop("Company Name")
                custom_fields.pop("keyword_3")
            contact = Contact(
                user_id=USER_ID,
                email_normalized=f"lead{index}@example.com",
                status="approved",
                custom_fields=custom_fields,
            )
            contacts.append(contact)

        session.add_all(contacts)
        session.flush()
        for index, contact in enumerate(contacts):
            session.add(
                CampaignRecipient(
                    campaign_id=campaign.id,
                    contact_id=contact.id,
                    status="approved",
                )
            )
            if index >= 400:
                expected_invalid_ids.add(contact.id)
        session.commit()
        campaign_id = campaign.id
        session.close()

        client = TestClient(app)
        preview = client.get(
            f"/api/campaigns/{campaign_id}/recipient-template-validation",
            headers=HEADERS,
        )
        assert preview.status_code == 200
        assert preview.json() == {
            "checked_recipient_count": 500,
            "ready_recipient_count": 400,
            "skipped_recipient_count": 100,
            "overlap_recipient_count": 25,
            "missing_by_variable": [
                {"variable": "Company Name", "row_count": 50},
                {"variable": "keyword_3", "row_count": 75},
            ],
        }

        response = client.post(
            f"/api/campaigns/{campaign_id}/send-now",
            json={"delay_minutes": 0, "dry_run": True},
            headers=HEADERS,
        )
        assert response.status_code == 200
        assert response.json()["queued"] == 20
        assert response.json()["recipient_validation"] == preview.json()

        session = session_factory()
        batches = 0
        while True:
            queued_jobs = list(
                session.scalars(
                    select(SendJob).where(
                        SendJob.campaign_id == campaign_id,
                        SendJob.status == "queued",
                    )
                )
            )
            if queued_jobs:
                batches += 1
            for job in queued_jobs:
                assert job.recipient_id not in expected_invalid_ids
                job.status = "sent"
                session.get(
                    CampaignRecipient,
                    {"campaign_id": campaign_id, "contact_id": job.recipient_id},
                ).status = "sent"
            session.commit()

            result = create_send_jobs_for_next_batch(
                session,
                user_id=USER_ID,
                campaign_id=campaign_id,
                delay_minutes=0,
            )
            session.commit()
            if result.get("exhausted"):
                break

        jobs = list(session.scalars(select(SendJob).where(SendJob.campaign_id == campaign_id)))
        assert batches == 20
        assert len(jobs) == 400
        assert expected_invalid_ids.isdisjoint(job.recipient_id for job in jobs)
        recipient_status_counts = dict(
            session.execute(
                select(CampaignRecipient.status, func.count())
                .where(CampaignRecipient.campaign_id == campaign_id)
                .group_by(CampaignRecipient.status)
            ).all()
        )
        assert recipient_status_counts == {"rejected": 100, "sent": 400}
        session.close()

        progress = client.get(f"/api/campaigns/{campaign_id}/send-progress", headers=HEADERS)
        assert progress.status_code == 200
        assert progress.json()["total_recipients"] == 500
        assert progress.json()["sent_count"] == 400
        assert progress.json()["skipped_count"] == 100
        assert progress.json()["is_active"] is False
    finally:
        clear_session_override()


@pytest.mark.parametrize(
    ("endpoint", "expected_campaign_status"),
    [
        ("send-now", "sending"),
        ("schedule", "scheduled"),
        ("autopilot/start", "autopilot"),
    ],
)
def test_every_campaign_start_mode_skips_incomplete_rows(
    tmp_path,
    endpoint,
    expected_campaign_status,
):
    session_factory = make_session_factory(tmp_path)
    install_session_override(session_factory)
    try:
        session = session_factory()
        ensure_user(session, USER_ID)
        group = SenderGroup(user_id=USER_ID, name=f"Validation {endpoint}")
        sender = Sender(
            user_id=USER_ID,
            group=group,
            email=f"{endpoint.replace('/', '-')}@example.com",
            status="connected",
            daily_cap=10,
            encrypted_oauth_credentials="encrypted",
            scopes=["https://www.googleapis.com/auth/gmail.send"],
            connected_at=utcnow(),
        )
        campaign = Campaign(
            user_id=USER_ID,
            selected_sender_group=group,
            name=f"Validation {endpoint}",
            subject_template="Hello {{First Name}}",
            body_template="Focus: {{keyword_3}}",
            status="draft",
        )
        ready = Contact(
            user_id=USER_ID,
            email_normalized=f"ready-{endpoint.replace('/', '-')}@example.com",
            status="approved",
            custom_fields={"First Name": "Ready", "keyword_3": "AI"},
        )
        incomplete = Contact(
            user_id=USER_ID,
            email_normalized=f"incomplete-{endpoint.replace('/', '-')}@example.com",
            status="approved",
            custom_fields={"First Name": "Incomplete", "keyword_3": ""},
        )
        session.add_all([group, sender, campaign, ready, incomplete])
        session.flush()
        session.add_all(
            [
                CampaignRecipient(campaign_id=campaign.id, contact_id=ready.id, status="approved"),
                CampaignRecipient(campaign_id=campaign.id, contact_id=incomplete.id, status="approved"),
            ]
        )
        session.commit()
        campaign_id = campaign.id
        ready_id = ready.id
        incomplete_id = incomplete.id
        session.close()

        payload: dict[str, object] = {"delay_minutes": 0, "dry_run": True}
        if endpoint == "schedule":
            payload["scheduled_at"] = (utcnow() + timedelta(hours=1)).isoformat()
        elif endpoint == "autopilot/start":
            payload["schedule"] = all_day_schedule()
            payload["scheduled_at"] = (utcnow() + timedelta(hours=1)).isoformat()

        response = TestClient(app).post(
            f"/api/campaigns/{campaign_id}/{endpoint}",
            json=payload,
            headers=HEADERS,
        )
        assert response.status_code == 200
        assert response.json()["recipient_validation"] == {
            "checked_recipient_count": 2,
            "ready_recipient_count": 1,
            "skipped_recipient_count": 1,
            "overlap_recipient_count": 0,
            "missing_by_variable": [{"variable": "keyword_3", "row_count": 1}],
        }

        session = session_factory()
        assert session.get(Campaign, campaign_id).status == expected_campaign_status
        assert session.get(
            CampaignRecipient,
            {"campaign_id": campaign_id, "contact_id": incomplete_id},
        ).status == "rejected"
        expected_ready_status = "queued" if endpoint == "send-now" else "approved"
        assert session.get(
            CampaignRecipient,
            {"campaign_id": campaign_id, "contact_id": ready_id},
        ).status == expected_ready_status
        assert session.scalar(
            select(func.count()).select_from(SendJob).where(
                SendJob.campaign_id == campaign_id,
                SendJob.recipient_id == incomplete_id,
            )
        ) == 0
        session.close()
    finally:
        clear_session_override()


def test_send_is_blocked_with_structured_counts_when_every_row_is_incomplete(tmp_path):
    session_factory = make_session_factory(tmp_path)
    install_session_override(session_factory)
    try:
        session = session_factory()
        ensure_user(session, USER_ID)
        group = SenderGroup(user_id=USER_ID, name="All incomplete")
        sender = Sender(
            user_id=USER_ID,
            group=group,
            email="all-incomplete-sender@example.com",
            status="connected",
            daily_cap=10,
            encrypted_oauth_credentials="encrypted",
            scopes=["https://www.googleapis.com/auth/gmail.send"],
            connected_at=utcnow(),
        )
        campaign = Campaign(
            user_id=USER_ID,
            selected_sender_group=group,
            name="All incomplete",
            subject_template="Hello {{First Name}}",
            body_template="Focus: {{keyword_3}}",
            status="draft",
        )
        contacts = [
            Contact(
                user_id=USER_ID,
                email_normalized=f"all-incomplete-{index}@example.com",
                status="approved",
                custom_fields={"First Name": f"Lead {index}"},
            )
            for index in range(3)
        ]
        session.add_all([group, sender, campaign, *contacts])
        session.flush()
        session.add_all(
            CampaignRecipient(campaign_id=campaign.id, contact_id=contact.id, status="approved")
            for contact in contacts
        )
        session.commit()
        campaign_id = campaign.id
        session.close()

        client = TestClient(app)
        preview = client.get(
            f"/api/campaigns/{campaign_id}/recipient-template-validation",
            headers=HEADERS,
        )
        assert preview.status_code == 200
        assert preview.json()["ready_recipient_count"] == 0
        assert preview.json()["skipped_recipient_count"] == 3

        response = client.post(
            f"/api/campaigns/{campaign_id}/send-now",
            json={"delay_minutes": 0},
            headers=HEADERS,
        )
        assert response.status_code == 409
        assert response.json()["detail"] == {
            "code": "no_recipients_with_complete_template_data",
            "message": "No recipients have all template values. 3 recipients would be skipped.",
            "recipient_validation": preview.json(),
        }

        session = session_factory()
        assert session.scalar(
            select(func.count()).select_from(SendJob).where(SendJob.campaign_id == campaign_id)
        ) == 0
        assert session.get(Campaign, campaign_id).status == "draft"
        session.close()
    finally:
        clear_session_override()


def test_group_oauth_connect_delete_and_reconnect_stays_in_database(tmp_path, monkeypatch):
    session_factory = make_session_factory(tmp_path)
    install_session_override(session_factory)
    credentials_path = tmp_path / "credentials.json"
    credentials_path.write_text(
        json.dumps(
            {
                "web": {
                    "client_id": "test-client",
                    "client_secret": "test-secret",
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                }
            }
        ),
        encoding="utf-8",
    )

    start_flow = MagicMock()
    start_flow.code_verifier = "pkce-verifier"
    start_flow.authorization_url.return_value = ("https://accounts.google.com/o/oauth2/auth", "ignored")
    monkeypatch.setattr(platform_oauth, "credentials_file_path", lambda: credentials_path)
    monkeypatch.setattr(
        platform_oauth.Flow,
        "from_client_secrets_file",
        lambda *_args, **_kwargs: start_flow,
    )

    exchanged_credentials = MagicMock()
    exchanged_credentials.scopes = ["https://www.googleapis.com/auth/gmail.send"]
    exchanged_credentials.to_json.return_value = json.dumps(
        {"token": "access-token", "refresh_token": "refresh-token"}
    )
    exchange_flow = MagicMock()
    exchange_flow.credentials = exchanged_credentials
    monkeypatch.setattr(oauth_router, "flow_from_state", lambda _state: exchange_flow)
    monkeypatch.setattr(
        oauth_router,
        "oauth_email_for_credentials",
        lambda _credentials: "sender@example.com",
    )

    try:
        client = TestClient(app)
        group_id = client.post(
            "/api/sender-groups",
            json={"name": "OAuth group"},
            headers=HEADERS,
        ).json()["id"]

        def connect_sender() -> int:
            start_response = client.post(
                f"/api/sender-groups/{group_id}/senders/oauth/start",
                headers=HEADERS,
            )
            assert start_response.status_code == 200
            assert start_response.json()["auth_url"].startswith("https://accounts.google.com/")

            session = session_factory()
            oauth_state = session.scalar(
                select(OAuthState)
                .where(OAuthState.group_id == group_id, OAuthState.used_at.is_(None))
                .order_by(OAuthState.created_at.desc())
            )
            assert oauth_state.user_id == USER_ID
            assert oauth_state.code_verifier == "pkce-verifier"
            state_value = oauth_state.state
            session.close()

            callback_response = client.get(
                f"/api/oauth/callback?code=fake-code&state={state_value}",
                follow_redirects=False,
            )
            assert callback_response.status_code in (302, 307)
            assert "oauth=success" in callback_response.headers["location"]
            exchange_flow.fetch_token.assert_called_with(code="fake-code")

            session = session_factory()
            sender = session.scalar(
                select(Sender).where(Sender.user_id == USER_ID, Sender.email == "sender@example.com")
            )
            state = session.get(OAuthState, state_value)
            assert sender.group_id == group_id
            assert sender.status == "connected"
            assert json.loads(decrypt_text(sender.encrypted_oauth_credentials))["refresh_token"] == "refresh-token"
            assert state.used_at is not None
            sender_id = sender.id
            session.close()
            return sender_id

        sender_id = connect_sender()
        assert client.delete(f"/api/senders/{sender_id}", headers=HEADERS).status_code == 200
        reconnected_sender_id = connect_sender()
        assert reconnected_sender_id == sender_id

        session = session_factory()
        reconnected = session.get(Sender, sender_id)
        assert reconnected.status == "connected"
        assert reconnected.removed_at is None
        session.close()

        assert list(tmp_path.rglob("*.json")) == [credentials_path]
        assert not any(
            getattr(route, "path", None) == "/api/oauth/start"
            and "POST" in getattr(route, "methods", set())
            for route in app.routes
        )
        assert not any(
            getattr(route, "path", None) == "/api/oauth/callback"
            and "POST" in getattr(route, "methods", set())
            for route in app.routes
        )
    finally:
        clear_session_override()


def test_next_batch_queues_one_email_per_connected_sender(tmp_path, monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    session_factory = make_session_factory(tmp_path)
    session = session_factory()
    ensure_user(session, USER_ID)
    group = SenderGroup(user_id=USER_ID, name="Three senders")
    session.add(group)
    session.flush()
    senders = [
        Sender(
            user_id=USER_ID,
            group_id=group.id,
            email=f"sender{i}@example.com",
            display_name=f"Sender {i}",
            status="connected",
            daily_cap=10,
            encrypted_oauth_credentials=f"encrypted-{i}",
            scopes=["https://www.googleapis.com/auth/gmail.send"],
            connected_at=utcnow(),
        )
        for i in range(3)
    ]
    campaign = Campaign(
        user_id=USER_ID,
        selected_sender_group_id=group.id,
        name="Batch test",
        subject_template="Hi {{ email }}",
        body_template="Body",
        fallback_body_template="Body",
        status="draft",
    )
    session.add_all([*senders, campaign])
    session.flush()
    contacts = [
        Contact(
            user_id=USER_ID,
            email_normalized=f"lead{i}@example.com",
            status="approved",
            custom_fields={"email": f"lead{i}@example.com", "f": f"value{i}"},
        )
        for i in range(5)
    ]
    session.add_all(contacts)
    session.flush()
    session.add_all(
        [
            CampaignRecipient(campaign_id=campaign.id, contact_id=contact.id)
            for contact in contacts
        ]
    )
    session.commit()

    result = create_send_jobs_for_next_batch(session, user_id=USER_ID, campaign_id=campaign.id, delay_minutes=7)
    assert result["created"] == 3
    assert result["queued"] == 0

    jobs = list(session.scalars(select(SendJob).order_by(SendJob.id)))
    assert len(jobs) == 3
    assert len({job.sender_id for job in jobs}) == 3
    assert len({job.recipient_id for job in jobs}) == 3
    assert all(job.status == "queued" for job in jobs)
    assert [(job.sender_id, job.recipient_id) for job in jobs] == [
        (senders[index].id, contacts[index].id) for index in range(3)
    ]

    for job in jobs:
        job.status = "sent"
        session.get(
            CampaignRecipient,
            {"campaign_id": campaign.id, "contact_id": job.recipient_id},
        ).status = "sent"
    session.commit()

    second_batch = create_send_jobs_for_next_batch(
        session,
        user_id=USER_ID,
        campaign_id=campaign.id,
        delay_minutes=7,
    )
    assert second_batch["created"] == 2
    assert len(list(session.scalars(select(SendJob)))) == 5
    session.close()


def test_autopilot_campaign_cap_reserves_queued_recipients(tmp_path):
    session_factory = make_session_factory(tmp_path)
    session = session_factory()
    ensure_user(session, USER_ID)
    group = SenderGroup(user_id=USER_ID, name="Autopilot cap")
    sender = Sender(
        user_id=USER_ID,
        group=group,
        email="sender@example.com",
        status="connected",
        daily_cap=10,
        encrypted_oauth_credentials="encrypted",
    )
    campaign = Campaign(
        user_id=USER_ID,
        selected_sender_group=group,
        name="Two per day",
        status="autopilot",
        send_settings={"mode": "autopilot", "delay_minutes": 5},
    )
    contacts = [
        Contact(user_id=USER_ID, email_normalized=f"cap{i}@example.com", status="approved")
        for i in range(3)
    ]
    session.add_all([group, sender, campaign, *contacts])
    session.flush()
    session.add(
        AutopilotDaySchedule(
            campaign_id=campaign.id,
            day_of_week=WEEKDAY_NAMES[utcnow().weekday()],
            daily_cap=2,
            start_time="00:00",
            end_time="23:59",
        )
    )
    session.add_all(
        [CampaignRecipient(campaign_id=campaign.id, contact_id=contact.id, status="approved") for contact in contacts]
    )
    session.commit()

    first = create_send_jobs_for_next_batch(session, user_id=USER_ID, campaign_id=campaign.id)
    second = create_send_jobs_for_next_batch(session, user_id=USER_ID, campaign_id=campaign.id)
    third = create_send_jobs_for_next_batch(session, user_id=USER_ID, campaign_id=campaign.id)

    assert first["created"] == 1
    assert second["created"] == 1
    assert third["reason_code"] == "campaign_daily_cap_reached"
    assert session.scalar(select(SendJob.id)) is not None
    session.close()


def test_autopilot_can_spread_parallel_batches_across_daily_window(tmp_path, monkeypatch):
    session_factory = make_session_factory(tmp_path)
    session = session_factory()
    ensure_user(session, USER_ID)
    current = datetime(2026, 7, 13, 9, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(platform_jobs, "utcnow", lambda: current)
    group = SenderGroup(user_id=USER_ID, name="Even pacing")
    campaign = Campaign(
        user_id=USER_ID,
        selected_sender_group=group,
        name="Even pacing",
        status="autopilot",
        send_settings={
            "mode": "autopilot",
            "delay_minutes": 5,
            "pacing_mode": "spread_evenly",
        },
    )
    senders = [
        Sender(
            user_id=USER_ID,
            group=group,
            email=f"pacing-{index}@example.com",
            status="connected",
            daily_cap=20,
            encrypted_oauth_credentials=f"encrypted-{index}",
        )
        for index in range(2)
    ]
    contacts = [
        Contact(user_id=USER_ID, email_normalized=f"paced-{index}@example.com", status="approved")
        for index in range(12)
    ]
    session.add_all([group, campaign, *senders, *contacts])
    session.flush()
    session.add(
        AutopilotDaySchedule(
            campaign_id=campaign.id,
            day_of_week=WEEKDAY_NAMES[current.weekday()],
            daily_cap=10,
            start_time="09:00",
            end_time="17:00",
        )
    )
    session.add_all(
        CampaignRecipient(campaign_id=campaign.id, contact_id=contact.id, status="approved")
        for contact in contacts
    )
    session.commit()

    result = create_send_jobs_for_next_batch(
        session,
        user_id=USER_ID,
        campaign_id=campaign.id,
        delay_minutes=5,
        scheduled_for=current,
    )

    expected_next = datetime(2026, 7, 13, 10, 36, tzinfo=timezone.utc)
    assert result["created"] == 2
    assert datetime.fromisoformat(result["next_batch_due_at"]) == expected_next
    assert campaign.scheduled_at == expected_next

    campaign.send_settings = {"mode": "autopilot", "delay_minutes": 5}
    fixed = platform_services.campaign_batch_schedule(
        session,
        campaign,
        after=current,
        delay_minutes=5,
        batch_size=2,
    )
    assert fixed["next_at"] == current + timedelta(minutes=5)
    session.close()


def test_autopilot_does_not_queue_on_disabled_day_or_outside_window(tmp_path):
    session_factory = make_session_factory(tmp_path)
    session = session_factory()
    ensure_user(session, USER_ID)
    group = SenderGroup(user_id=USER_ID, name="Autopilot policy")
    sender = Sender(
        user_id=USER_ID,
        group=group,
        email="policy@example.com",
        status="connected",
        daily_cap=10,
        encrypted_oauth_credentials="encrypted",
    )
    campaign = Campaign(
        user_id=USER_ID,
        selected_sender_group=group,
        name="Policy campaign",
        status="autopilot",
        send_settings={"mode": "autopilot", "delay_minutes": 5},
    )
    contact = Contact(user_id=USER_ID, email_normalized="policy-lead@example.com", status="approved")
    session.add_all([group, sender, campaign, contact])
    session.flush()
    session.add(CampaignRecipient(campaign_id=campaign.id, contact_id=contact.id, status="approved"))
    tomorrow = WEEKDAY_NAMES[(utcnow().weekday() + 1) % 7]
    session.add(
        AutopilotDaySchedule(
            campaign_id=campaign.id,
            day_of_week=tomorrow,
            daily_cap=10,
            start_time="00:00",
            end_time="23:59",
        )
    )
    session.commit()

    disabled = create_send_jobs_for_next_batch(session, user_id=USER_ID, campaign_id=campaign.id)
    assert disabled["reason_code"] == "autopilot_day_disabled"
    assert session.scalar(select(SendJob.id)) is None

    session.query(AutopilotDaySchedule).delete()
    session.add(
        AutopilotDaySchedule(
            campaign_id=campaign.id,
            day_of_week=WEEKDAY_NAMES[utcnow().weekday()],
            daily_cap=10,
            start_time="00:00",
            end_time="00:01",
        )
    )
    session.commit()
    outside = create_send_jobs_for_next_batch(session, user_id=USER_ID, campaign_id=campaign.id)
    assert outside["reason_code"] == "autopilot_outside_window"
    assert session.scalar(select(SendJob.id)) is None
    session.close()


def test_daily_counts_follow_user_timezone(tmp_path, monkeypatch):
    session_factory = make_session_factory(tmp_path)
    session = session_factory()
    ensure_user(session, USER_ID)
    session.get(UserSettings, USER_ID).timezone = "Europe/Paris"
    campaign = Campaign(user_id=USER_ID, name="Timezone", status="draft")
    session.add(campaign)
    session.flush()
    session.add(
        SendLog(
            user_id=USER_ID,
            campaign_id=campaign.id,
            recipient_email="timezone@example.com",
            sender_email="sender@example.com",
            subject="Timezone",
            status="sent",
            sent_at=datetime(2026, 7, 13, 22, 30, tzinfo=timezone.utc),
        )
    )
    session.commit()
    monkeypatch.setattr(platform_services, "utcnow", lambda: datetime(2026, 7, 13, 23, 30, tzinfo=timezone.utc))
    assert platform_services.campaign_sent_today(session, campaign.id) == 1
    session.close()


def test_next_batch_does_not_queue_pending_contact(tmp_path):
    session_factory = make_session_factory(tmp_path)
    session = session_factory()
    ensure_user(session, USER_ID)
    group = SenderGroup(user_id=USER_ID, name="Pending contacts")
    session.add(group)
    session.flush()
    sender = Sender(
        user_id=USER_ID,
        group_id=group.id,
        email="sender@example.com",
        status="connected",
        daily_cap=10,
        encrypted_oauth_credentials="encrypted",
    )
    campaign = Campaign(
        user_id=USER_ID,
        selected_sender_group_id=group.id,
        name="Pending contact test",
        status="sending",
    )
    contact = Contact(user_id=USER_ID, email_normalized="pending@example.com", status="pending")
    session.add_all([sender, campaign, contact])
    session.flush()
    session.add(CampaignRecipient(campaign_id=campaign.id, contact_id=contact.id, status="approved"))
    session.commit()

    result = create_send_jobs_for_next_batch(session, user_id=USER_ID, campaign_id=campaign.id)
    assert result["created"] == 0
    assert result["exhausted"] is True
    assert session.scalar(select(SendJob.id)) is None
    session.close()


def test_reset_recipient_removes_terminal_job_and_starts_new_attempt_boundary(tmp_path):
    session_factory = make_session_factory(tmp_path)
    install_session_override(session_factory)
    try:
        session = session_factory()
        ensure_user(session, USER_ID)
        group = SenderGroup(user_id=USER_ID, name="Reset flow")
        session.add(group)
        session.flush()
        sender = Sender(
            user_id=USER_ID,
            group_id=group.id,
            email="sender@example.com",
            status="connected",
            daily_cap=10,
            encrypted_oauth_credentials="encrypted",
        )
        campaign = Campaign(user_id=USER_ID, selected_sender_group_id=group.id, name="Reset flow", status="draft")
        contact = Contact(user_id=USER_ID, email_normalized="sent@example.com", status="sent")
        session.add_all([sender, campaign, contact])
        session.flush()
        recipient = CampaignRecipient(campaign_id=campaign.id, contact_id=contact.id, status="sent")
        job = SendJob(
            user_id=USER_ID,
            campaign_id=campaign.id,
            recipient_id=contact.id,
            sender_id=sender.id,
            status="sent",
            scheduled_for=utcnow(),
            batch_id="old-batch",
            idempotency_key=f"campaign:{campaign.id}:recipient:{contact.id}",
        )
        session.add_all([recipient, job])
        session.commit()
        campaign_id = campaign.id
        contact_id = contact.id
        session.close()

        response = TestClient(app).patch(
            f"/api/campaigns/{campaign_id}/recipients/{contact_id}/reset",
            headers=HEADERS,
        )
        assert response.status_code == 200

        session = session_factory()
        updated = session.get(CampaignRecipient, {"campaign_id": campaign_id, "contact_id": contact_id})
        assert updated.status == "approved"
        assert updated.reset_at is not None
        assert session.get(Contact, contact_id).status == "approved"
        assert session.scalar(select(SendJob.id)) is None
        session.get(Campaign, campaign_id).status = "sending"
        session.commit()
        retry = create_send_jobs_for_next_batch(session, user_id=USER_ID, campaign_id=campaign_id)
        assert retry["created"] == 1
        session.close()
    finally:
        clear_session_override()


def test_approving_pending_recipient_updates_contact_and_campaign_status(tmp_path):
    session_factory = make_session_factory(tmp_path)
    install_session_override(session_factory)
    try:
        session = session_factory()
        ensure_user(session, USER_ID)
        campaign = Campaign(user_id=USER_ID, name="Pending approval", status="draft")
        contact = Contact(
            user_id=USER_ID,
            email_normalized="pending@example.com",
            status="pending",
        )
        session.add_all([campaign, contact])
        session.flush()
        session.add(
            CampaignRecipient(
                campaign_id=campaign.id,
                contact_id=contact.id,
                status="pending",
            )
        )
        session.commit()
        campaign_id = campaign.id
        contact_id = contact.id
        session.close()

        response = TestClient(app).patch(
            f"/api/campaigns/{campaign_id}/recipients/{contact_id}/reset",
            headers=HEADERS,
        )
        assert response.status_code == 200

        session = session_factory()
        recipient = session.get(
            CampaignRecipient,
            {"campaign_id": campaign_id, "contact_id": contact_id},
        )
        assert recipient.status == "approved"
        assert recipient.reset_at is not None
        assert session.get(Contact, contact_id).status == "approved"
        session.close()
    finally:
        clear_session_override()


@pytest.mark.parametrize(
    ("endpoint", "payload", "expected_status", "expected_recipient_status"),
    [
        ("send-now", {"delay_minutes": 0}, "queued", "queued"),
        (
            "schedule",
            {"delay_minutes": 0, "scheduled_at": (utcnow() + timedelta(hours=1)).isoformat()},
            "scheduled",
            "approved",
        ),
        ("autopilot/start", {"delay_minutes": 0, "schedule": all_day_schedule()}, "autopilot", "approved"),
    ],
)
def test_restarting_campaign_reopens_failed_recipients_only(
    tmp_path,
    endpoint,
    payload,
    expected_status,
    expected_recipient_status,
):
    session_factory = make_session_factory(tmp_path)
    install_session_override(session_factory)
    try:
        campaign_id, sender_ids, contact_ids = _seed_delivery_campaign(session_factory, recipient_count=2)
        sent_contact_id, failed_contact_id = contact_ids
        session = session_factory()
        campaign = session.get(Campaign, campaign_id)
        campaign.status = "ended"
        sent_recipient = session.get(
            CampaignRecipient,
            {"campaign_id": campaign_id, "contact_id": sent_contact_id},
        )
        failed_recipient = session.get(
            CampaignRecipient,
            {"campaign_id": campaign_id, "contact_id": failed_contact_id},
        )
        sent_recipient.status = "sent"
        failed_recipient.status = "failed"
        session.add_all(
            [
                SendJob(
                    user_id=USER_ID,
                    campaign_id=campaign_id,
                    recipient_id=sent_contact_id,
                    sender_id=sender_ids[0],
                    status="sent",
                    scheduled_for=utcnow(),
                    batch_id="completed-batch",
                    idempotency_key=f"campaign:{campaign_id}:recipient:{sent_contact_id}",
                ),
                SendJob(
                    user_id=USER_ID,
                    campaign_id=campaign_id,
                    recipient_id=failed_contact_id,
                    sender_id=sender_ids[1],
                    status="failed",
                    attempts=3,
                    scheduled_for=utcnow(),
                    batch_id="failed-batch",
                    idempotency_key=f"campaign:{campaign_id}:recipient:{failed_contact_id}",
                ),
                SendLog(
                    user_id=USER_ID,
                    campaign_id=campaign_id,
                    contact_id=failed_contact_id,
                    recipient_id=failed_contact_id,
                    sender_id=sender_ids[1],
                    recipient_email="lead2@example.com",
                    sender_email="sender2@example.com",
                    subject="Hello",
                    body_snapshot="Body",
                    status="failed",
                    error_message="Previous delivery failed",
                ),
            ]
        )
        session.commit()
        session.close()

        response = TestClient(app).post(
            f"/api/campaigns/{campaign_id}/{endpoint}",
            json=payload,
            headers=HEADERS,
        )
        assert response.status_code == 200
        assert response.json()["status"] == expected_status
        assert response.json()["retried_failed"] == 1

        session = session_factory()
        persisted_sent = session.get(
            CampaignRecipient,
            {"campaign_id": campaign_id, "contact_id": sent_contact_id},
        )
        persisted_failed = session.get(
            CampaignRecipient,
            {"campaign_id": campaign_id, "contact_id": failed_contact_id},
        )
        jobs = list(session.scalars(select(SendJob).where(SendJob.campaign_id == campaign_id)))
        assert persisted_sent.status == "sent"
        assert persisted_sent.reset_at is None
        assert persisted_failed.status == expected_recipient_status
        assert persisted_failed.reset_at is not None
        assert any(job.recipient_id == sent_contact_id and job.status == "sent" for job in jobs)
        replacement_jobs = [job for job in jobs if job.recipient_id == failed_contact_id]
        if endpoint == "send-now":
            assert len(replacement_jobs) == 1
            assert replacement_jobs[0].status == "queued"
            assert replacement_jobs[0].attempts == 0
        else:
            assert replacement_jobs == []
        assert session.scalar(select(func.count()).select_from(SendLog).where(SendLog.campaign_id == campaign_id)) == 1
        session.close()
    finally:
        clear_session_override()


def test_next_batch_skips_capped_senders_and_uses_remaining_sender(tmp_path):
    session_factory = make_session_factory(tmp_path)
    session = session_factory()
    ensure_user(session, USER_ID)
    group = SenderGroup(user_id=USER_ID, name="Cap handling")
    session.add(group)
    session.flush()
    capped = Sender(
        user_id=USER_ID,
        group_id=group.id,
        email="capped@example.com",
        status="connected",
        daily_cap=1,
        encrypted_oauth_credentials="encrypted-1",
    )
    available = Sender(
        user_id=USER_ID,
        group_id=group.id,
        email="available@example.com",
        status="connected",
        daily_cap=10,
        encrypted_oauth_credentials="encrypted-2",
    )
    campaign = Campaign(
        user_id=USER_ID,
        selected_sender_group_id=group.id,
        name="Cap test",
        subject_template="Hello",
        body_template="Body",
        fallback_body_template="Body",
        status="sending",
    )
    contact = Contact(user_id=USER_ID, email_normalized="lead@example.com", status="approved")
    session.add_all([capped, available, campaign, contact])
    session.flush()
    session.add(CampaignRecipient(campaign_id=campaign.id, contact_id=contact.id))
    session.add(
        SendLog(
            user_id=USER_ID,
            campaign_id=campaign.id,
            sender_id=capped.id,
            recipient_email="previous@example.com",
            sender_email=capped.email,
            status="sent",
            sent_at=utcnow(),
        )
    )
    session.commit()

    result = create_send_jobs_for_next_batch(session, user_id=USER_ID, campaign_id=campaign.id)
    job = session.get(SendJob, result["job_ids"][0])
    assert result["created"] == 1
    assert job.sender_id == available.id
    session.close()


def test_scheduler_pauses_send_now_but_keeps_autopilot_when_all_senders_are_capped(tmp_path, monkeypatch):
    session_factory = make_session_factory(tmp_path)
    session = session_factory()
    ensure_user(session, USER_ID)
    now = utcnow()
    campaigns = [
        Campaign(
            user_id=USER_ID,
            name="Send now capped",
            subject_template="Hello",
            body_template="Body",
            fallback_body_template="Body",
            status="sending",
            scheduled_at=now - timedelta(seconds=1),
            send_settings={"mode": "send_now"},
        ),
        Campaign(
            user_id=USER_ID,
            name="Autopilot capped",
            subject_template="Hello",
            body_template="Body",
            fallback_body_template="Body",
            status="autopilot",
            scheduled_at=now - timedelta(seconds=1),
            send_settings={"mode": "autopilot", "days": ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]},
        ),
    ]
    session.add_all(campaigns)
    session.commit()
    monkeypatch.setattr(
        platform_scheduler,
        "create_send_jobs_for_next_batch",
        lambda *_args, **_kwargs: {
            "created": 0,
            "job_ids": [],
            "reason_code": "daily_caps_reached",
            "reason": "All senders reached their daily cap",
        },
    )
    platform_scheduler.enqueue_due_campaign_batches(session)

    assert campaigns[0].status == "paused"
    assert campaigns[0].scheduled_at is None
    assert campaigns[1].status == "autopilot"
    assert campaigns[1].scheduled_at > now
    session.close()


def test_campaign_delivery_control_routes_are_registered_once():
    registered_routes = []
    for route in app.routes:
        original_router = getattr(route, "original_router", None)
        if original_router is not None:
            registered_routes.extend(original_router.routes)
        else:
            registered_routes.append(route)

    for action in ("send-now", "schedule", "autopilot/start", "pause", "resume", "stop"):
        path = f"/api/campaigns/{{campaign_id}}/{action}"
        matches = [
            route
            for route in registered_routes
            if getattr(route, "path", None) == path
            and "POST" in (getattr(route, "methods", set()) or set())
        ]
        assert len(matches) == 1, f"{path} is registered {len(matches)} times"


def test_worker_tick_requires_token_and_runs_without_browser(tmp_path, monkeypatch):
    session_factory = make_session_factory(tmp_path)
    monkeypatch.setattr(campaign_delivery, "recover_stale_jobs", lambda **_kwargs: 2)
    monkeypatch.setattr(campaign_delivery, "run_worker_cycle", lambda **_kwargs: 3)
    monkeypatch.setenv("WORKER_TICK_TOKEN", "test-worker-token")

    client = TestClient(app)
    assert client.post("/internal/worker/tick").status_code == 401
    response = client.post(
        "/internal/worker/tick",
        headers={"X-Worker-Token": "test-worker-token"},
    )
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "recovered": 2, "processed": 3}


def test_send_now_creates_jobs_without_sending_inside_api_process(tmp_path):
    session_factory = make_session_factory(tmp_path)
    install_session_override(session_factory)
    try:
        session = session_factory()
        ensure_user(session, USER_ID)
        group = SenderGroup(user_id=USER_ID, name="Send now")
        session.add(group)
        session.flush()
        sender = Sender(
            user_id=USER_ID,
            group_id=group.id,
            email="sender@example.com",
            display_name="Sender",
            status="connected",
            daily_cap=10,
            encrypted_oauth_credentials="encrypted",
            scopes=["https://www.googleapis.com/auth/gmail.send"],
            connected_at=utcnow(),
        )
        campaign = Campaign(
            user_id=USER_ID,
            selected_sender_group_id=group.id,
            name="Send now test",
            subject_template="Hello",
            body_template="Body",
            fallback_body_template="Fallback",
            status="draft",
        )
        contact = Contact(
            user_id=USER_ID,
            email_normalized="lead@example.com",
            status="approved",
            custom_fields={},
        )
        session.add_all([sender, campaign, contact])
        session.flush()
        session.add(CampaignRecipient(campaign_id=campaign.id, contact_id=contact.id))
        session.commit()
        campaign_id = campaign.id
        session.close()

        response = TestClient(app).post(
            f"/api/campaigns/{campaign_id}/send-now",
            json={"delay_minutes": 2},
            headers=HEADERS,
        )
        assert response.status_code == 200
        assert response.json()["queued"] == 1

        session = session_factory()
        persisted_campaign = session.get(Campaign, campaign_id)
        job = session.scalar(select(SendJob).where(SendJob.campaign_id == campaign_id))
        recipient = session.get(
            CampaignRecipient,
            {"campaign_id": campaign_id, "contact_id": contact.id},
        )
        assert persisted_campaign.status == "sending"
        assert persisted_campaign.send_settings["delay_minutes"] == 2
        assert job.status == "queued"
        assert recipient.status == "queued"
        session.close()
    finally:
        clear_session_override()


def test_send_now_does_not_succeed_silently_when_nothing_is_queued(tmp_path):
    session_factory = make_session_factory(tmp_path)
    install_session_override(session_factory)
    try:
        session = session_factory()
        ensure_user(session, USER_ID)
        group = SenderGroup(user_id=USER_ID, name="Empty campaign")
        session.add(group)
        session.flush()
        session.add(
            Sender(
                user_id=USER_ID,
                group_id=group.id,
                email="sender@example.com",
                display_name="Sender",
                status="connected",
                daily_cap=10,
                encrypted_oauth_credentials="encrypted",
                scopes=["https://www.googleapis.com/auth/gmail.send"],
                connected_at=utcnow(),
            )
        )
        campaign = Campaign(
            user_id=USER_ID,
            selected_sender_group_id=group.id,
            name="Empty",
            subject_template="Hello",
            body_template="Body",
            fallback_body_template="Fallback",
            status="draft",
        )
        session.add(campaign)
        session.commit()
        campaign_id = campaign.id
        session.close()

        response = TestClient(app).post(
            f"/api/campaigns/{campaign_id}/send-now",
            json={"delay_minutes": 2},
            headers=HEADERS,
        )
        assert response.status_code == 409
        assert response.json()["detail"] == "No unsent approved recipients are ready"

        schedule_response = TestClient(app).post(
            f"/api/campaigns/{campaign_id}/schedule",
            json={"delay_minutes": 2, "scheduled_at": utcnow().isoformat()},
            headers=HEADERS,
        )
        assert schedule_response.status_code == 409
        assert schedule_response.json()["detail"] == "Campaign has no approved recipients"
    finally:
        clear_session_override()


def test_perform_send_job_sends_and_persists_delivery_state(tmp_path, monkeypatch):
    session_factory = make_session_factory(tmp_path)
    session = session_factory()
    ensure_user(session, USER_ID)
    group = SenderGroup(user_id=USER_ID, name="Delivery")
    session.add(group)
    session.flush()
    sender = Sender(
        user_id=USER_ID,
        group_id=group.id,
        email="sender@example.com",
        display_name="Sender",
        status="connected",
        daily_cap=10,
        encrypted_oauth_credentials="encrypted",
        scopes=["https://www.googleapis.com/auth/gmail.send"],
        connected_at=utcnow(),
    )
    campaign = Campaign(
        user_id=USER_ID,
        selected_sender_group_id=group.id,
        name="Delivery test",
        subject_template="Hello {{ First Name }} / {{ First_Name }}",
        body_template="Message for {{ email }}",
        fallback_body_template="Fallback",
        status="sending",
    )
    contact = Contact(
        user_id=USER_ID,
        email_normalized="lead@example.com",
        status="approved",
        custom_fields={"First Name": "Ada", "First_Name": "Grace"},
    )
    session.add_all([sender, campaign, contact])
    session.flush()
    recipient = CampaignRecipient(campaign_id=campaign.id, contact_id=contact.id, status="queued")
    job = SendJob(
        user_id=USER_ID,
        campaign_id=campaign.id,
        recipient_id=contact.id,
        sender_id=sender.id,
        status="queued",
        scheduled_for=utcnow(),
        batch_id="batch",
        idempotency_key=f"campaign:{campaign.id}:recipient:{contact.id}",
    )
    session.add_all([recipient, job])
    session.commit()
    job_id = job.id
    session.close()

    monkeypatch.setattr(platform_jobs, "SessionLocal", session_factory)
    monkeypatch.setattr(platform_jobs, "gmail_service_for_sender", lambda _session, _sender: object())
    sent_payload: dict = {}

    def fake_send(**kwargs):
        sent_payload.update(kwargs)
        return SimpleNamespace(message_id="gmail-message", thread_id="gmail-thread")

    monkeypatch.setattr(platform_jobs, "send_email", fake_send)

    assert platform_jobs.perform_send_job(job_id)["status"] == "sent"

    session = session_factory()
    persisted_job = session.get(SendJob, job_id)
    persisted_recipient = session.get(
        CampaignRecipient,
        {"campaign_id": campaign.id, "contact_id": contact.id},
    )
    log = session.scalar(select(SendLog).where(SendLog.campaign_id == campaign.id))
    assert persisted_job.status == "sent"
    assert persisted_job.attempts == 1
    assert sent_payload["subject"] == "Hello Ada / Grace"
    assert persisted_recipient.status == "sent"
    persisted_campaign = session.get(Campaign, campaign.id)
    assert persisted_campaign.status == "ended"
    assert persisted_campaign.scheduled_at is None
    assert session.get(Contact, contact.id).status == "approved"
    assert log.status == "sent"
    assert log.contact_id == contact.id
    assert log.gmail_message_id == "gmail-message"
    session.close()


def test_pre_send_failure_persists_attempts_and_stops_retrying(tmp_path, monkeypatch):
    session_factory = make_session_factory(tmp_path)
    session = session_factory()
    ensure_user(session, USER_ID)
    group = SenderGroup(user_id=USER_ID, name="Render failure")
    sender = Sender(
        user_id=USER_ID,
        group=group,
        email="sender@example.com",
        status="connected",
        daily_cap=10,
        encrypted_oauth_credentials="encrypted",
    )
    campaign = Campaign(
        user_id=USER_ID,
        selected_sender_group=group,
        name="Render failure",
        subject_template="Subject",
        body_template="Body",
        status="sending",
    )
    contact = Contact(
        user_id=USER_ID,
        email_normalized="lead@example.com",
        status="approved",
        custom_fields={},
    )
    session.add_all([group, sender, campaign, contact])
    session.flush()
    recipient = CampaignRecipient(campaign_id=campaign.id, contact_id=contact.id, status="queued")
    job = SendJob(
        user_id=USER_ID,
        campaign_id=campaign.id,
        recipient_id=contact.id,
        sender_id=sender.id,
        status="queued",
        max_attempts=2,
        scheduled_for=utcnow(),
        batch_id="render-failure",
        idempotency_key=f"campaign:{campaign.id}:recipient:{contact.id}",
    )
    session.add_all([recipient, job])
    session.commit()
    job_id = job.id
    campaign_id = campaign.id
    contact_id = contact.id
    session.close()

    monkeypatch.setattr(platform_jobs, "SessionLocal", session_factory)
    monkeypatch.setattr(platform_jobs, "_render", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("render failed")))

    platform_jobs.perform_send_job(job_id)
    session = session_factory()
    retry_job = session.get(SendJob, job_id)
    assert retry_job.status == "retry"
    assert retry_job.attempts == 1
    retry_job.scheduled_for = utcnow()
    retry_sender = session.get(Sender, retry_job.sender_id)
    retry_sender.last_error = None
    retry_sender.recent_error_at = None
    session.commit()
    session.close()

    platform_jobs.perform_send_job(job_id)
    session = session_factory()
    assert session.get(SendJob, job_id).status == "failed"
    assert session.get(SendJob, job_id).attempts == 2
    assert session.get(CampaignRecipient, {"campaign_id": campaign_id, "contact_id": contact_id}).status == "failed"
    session.close()


def test_missing_exact_template_header_skips_recipient_without_stopping_campaign(tmp_path, monkeypatch):
    session_factory = make_session_factory(tmp_path)
    session = session_factory()
    ensure_user(session, USER_ID)
    group = SenderGroup(user_id=USER_ID, name="Missing header")
    sender = Sender(
        user_id=USER_ID,
        group=group,
        email="sender@example.com",
        status="connected",
        daily_cap=10,
        encrypted_oauth_credentials="encrypted",
        last_error="expected token 'end of print statement', got 'Name'",
        recent_error_at=utcnow(),
    )
    campaign = Campaign(
        user_id=USER_ID,
        selected_sender_group=group,
        name="Missing header",
        subject_template="Hello {{ First Name }}",
        body_template="Body",
        status="sending",
    )
    contact = Contact(
        user_id=USER_ID,
        email_normalized="lead@example.com",
        status="approved",
        custom_fields={"First_Name": "Ada"},
    )
    session.add_all([group, sender, campaign, contact])
    session.flush()
    recipient = CampaignRecipient(campaign_id=campaign.id, contact_id=contact.id, status="queued")
    job = SendJob(
        user_id=USER_ID,
        campaign_id=campaign.id,
        recipient_id=contact.id,
        sender_id=sender.id,
        status="queued",
        scheduled_for=utcnow(),
        batch_id="missing-header",
        idempotency_key=f"campaign:{campaign.id}:recipient:{contact.id}",
    )
    session.add_all([recipient, job])
    session.commit()
    job_id = job.id
    campaign_id = campaign.id
    contact_id = contact.id
    sender_id = sender.id
    session.close()

    monkeypatch.setattr(platform_jobs, "SessionLocal", session_factory)

    result = platform_jobs.perform_send_job(job_id)

    assert result["status"] == "skipped"
    session = session_factory()
    assert session.get(SendJob, job_id) is None
    assert session.get(CampaignRecipient, {"campaign_id": campaign_id, "contact_id": contact_id}).status == "skipped"
    resumed_campaign = session.get(Campaign, campaign_id)
    assert resumed_campaign.status == "ended"
    assert resumed_campaign.send_settings.get("pause_reason") is None
    recovered_sender = session.get(Sender, sender_id)
    assert recovered_sender.last_error is None
    assert recovered_sender.recent_error_at is None
    skipped_log = session.scalar(select(SendLog).where(SendLog.campaign_id == campaign_id))
    assert skipped_log.status == "skipped"
    assert "First Name" in skipped_log.error_message
    session.close()


def test_settings_timezone_is_the_timezone_used_by_delivery(tmp_path):
    session_factory = make_session_factory(tmp_path)
    install_session_override(session_factory)
    try:
        client = TestClient(app)
        response = client.patch(
            "/api/settings",
            json={
                "timezone": "America/New_York",
                "max_daily_cap": 75,
                "bounce_rate_pause_threshold": 4.5,
                "max_consecutive_errors": 6,
            },
            headers=HEADERS,
        )
        assert response.status_code == 200

        session = session_factory()
        settings = session.get(UserSettings, USER_ID)
        assert settings.timezone == "America/New_York"
        assert settings.defaults["max_daily_cap"] == 75
        assert platform_services.user_zone(session, USER_ID).key == "America/New_York"
        session.close()

        invalid = client.patch(
            "/api/settings",
            json={
                "timezone": "Mars/Olympus",
                "max_daily_cap": 75,
                "bounce_rate_pause_threshold": 4.5,
                "max_consecutive_errors": 6,
            },
            headers=HEADERS,
        )
        assert invalid.status_code == 422
    finally:
        clear_session_override()


def test_campaign_timezone_is_saved_without_changing_account_default(tmp_path):
    session_factory = make_session_factory(tmp_path)
    install_session_override(session_factory)
    try:
        session = session_factory()
        ensure_user(session, USER_ID)
        session.get(UserSettings, USER_ID).timezone = "Africa/Casablanca"
        campaign = Campaign(user_id=USER_ID, name="US recipients", status="draft")
        session.add(campaign)
        session.commit()
        campaign_id = campaign.id
        session.close()

        response = TestClient(app).patch(
            f"/api/campaigns/{campaign_id}/send-settings",
            json={
                "mode": "schedule",
                "timezone": "America/New_York",
                "scheduled_at": "2026-08-31T09:00:00",
                "delay_minutes": 5,
            },
            headers=HEADERS,
        )
        assert response.status_code == 200

        session = session_factory()
        campaign = session.get(Campaign, campaign_id)
        assert campaign.timezone == "America/New_York"
        assert platform_services.campaign_zone(session, campaign).key == "America/New_York"
        assert session.get(UserSettings, USER_ID).timezone == "Africa/Casablanca"
        assert datetime.fromisoformat(campaign.send_settings["draft_scheduled_at"]) == datetime(
            2026,
            8,
            31,
            13,
            0,
            tzinfo=timezone.utc,
        )
        session.close()
    finally:
        clear_session_override()


def test_autopilot_window_uses_campaign_timezone_instead_of_account_timezone(tmp_path):
    session_factory = make_session_factory(tmp_path)
    session = session_factory()
    ensure_user(session, USER_ID)
    session.get(UserSettings, USER_ID).timezone = "Africa/Casablanca"
    campaign = Campaign(
        user_id=USER_ID,
        name="New York morning",
        status="autopilot",
        timezone="America/New_York",
        send_settings={"mode": "autopilot"},
    )
    session.add(campaign)
    session.flush()
    session.add(
        AutopilotDaySchedule(
            campaign_id=campaign.id,
            day_of_week="monday",
            daily_cap=10,
            start_time="09:00",
            end_time="17:00",
        )
    )
    session.commit()

    next_run = platform_services.next_autopilot_run(
        session,
        campaign,
        now=datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc),
    )
    assert next_run == datetime(2026, 8, 31, 13, 0, tzinfo=timezone.utc)
    session.close()


def test_account_timezone_change_does_not_rewrite_campaign_timezone(tmp_path, monkeypatch):
    fixed_now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    session_factory = make_session_factory(tmp_path)
    install_session_override(session_factory)
    try:
        session = session_factory()
        ensure_user(session, USER_ID)
        campaign = Campaign(
            user_id=USER_ID,
            name="US autopilot",
            status="autopilot",
            timezone="America/New_York",
            send_settings={"mode": "autopilot"},
            scheduled_at=fixed_now + timedelta(hours=1),
        )
        session.add(campaign)
        session.commit()
        campaign_id = campaign.id
        original_schedule = campaign.scheduled_at
        session.close()

        monkeypatch.setattr(settings_router, "utcnow", lambda: fixed_now)
        response = TestClient(app).patch(
            "/api/settings/timezone",
            json={"timezone": "Europe/Paris"},
            headers=HEADERS,
        )
        assert response.status_code == 200
        assert response.json()["rescheduled_campaigns"] == 0

        session = session_factory()
        campaign = session.get(Campaign, campaign_id)
        assert campaign.timezone == "America/New_York"
        assert campaign.scheduled_at.replace(tzinfo=timezone.utc) == original_schedule.astimezone(timezone.utc)
        assert session.get(UserSettings, USER_ID).timezone == "Europe/Paris"
        session.close()
    finally:
        clear_session_override()


def test_browser_timezone_initialization_preserves_a_saved_default(tmp_path):
    session_factory = make_session_factory(tmp_path)
    install_session_override(session_factory)
    try:
        session = session_factory()
        ensure_user(session, USER_ID)
        settings = session.get(UserSettings, USER_ID)
        settings.timezone = "Africa/Casablanca"
        session.commit()
        session.close()

        response = TestClient(app).patch(
            "/api/settings/timezone",
            json={"timezone": "America/New_York", "only_if_unset": True},
            headers=HEADERS,
        )
        assert response.status_code == 200
        assert response.json()["changed"] is False

        session = session_factory()
        settings = session.get(UserSettings, USER_ID)
        assert settings.timezone == "Africa/Casablanca"
        session.close()
    finally:
        clear_session_override()


def test_browser_timezone_sync_reschedules_active_autopilot(tmp_path, monkeypatch):
    fixed_now = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    session_factory = make_session_factory(tmp_path)
    install_session_override(session_factory)
    try:
        campaign_id, _, _ = _seed_delivery_campaign(session_factory, recipient_count=1)
        session = session_factory()
        campaign = session.get(Campaign, campaign_id)
        campaign.status = "autopilot"
        campaign.send_settings = {"mode": "autopilot", "delay_minutes": 5}
        campaign.scheduled_at = fixed_now + timedelta(days=1)
        session.add(
            AutopilotDaySchedule(
                campaign_id=campaign_id,
                day_of_week="monday",
                daily_cap=10,
                start_time="09:00",
                end_time="17:00",
            )
        )
        session.commit()
        session.close()

        monkeypatch.setattr(settings_router, "utcnow", lambda: fixed_now)
        client = TestClient(app)
        response = client.patch(
            "/api/settings/timezone",
            json={"timezone": "America/New_York"},
            headers=HEADERS,
        )
        assert response.status_code == 200
        assert response.json()["changed"] is True
        assert response.json()["rescheduled_campaigns"] == 1

        session = session_factory()
        assert session.get(UserSettings, USER_ID).timezone == "America/New_York"
        scheduled_at = session.get(Campaign, campaign_id).scheduled_at
        assert scheduled_at.replace(tzinfo=timezone.utc) == datetime(
            2026,
            7,
            13,
            13,
            0,
            tzinfo=timezone.utc,
        )
        session.close()

        unchanged = client.patch(
            "/api/settings/timezone",
            json={"timezone": "America/New_York"},
            headers=HEADERS,
        )
        assert unchanged.status_code == 200
        assert unchanged.json()["changed"] is False
    finally:
        clear_session_override()


def test_timezone_sync_keeps_exhausted_autopilot_on_next_local_day(tmp_path, monkeypatch):
    fixed_now = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    session_factory = make_session_factory(tmp_path)
    install_session_override(session_factory)
    try:
        campaign_id, sender_ids, contact_ids = _seed_delivery_campaign(
            session_factory,
            recipient_count=2,
        )
        session = session_factory()
        campaign = session.get(Campaign, campaign_id)
        campaign.status = "autopilot"
        campaign.send_settings = {"mode": "autopilot", "delay_minutes": 5}
        campaign.scheduled_at = fixed_now + timedelta(days=1)
        session.add_all(
            AutopilotDaySchedule(
                campaign_id=campaign_id,
                day_of_week=day,
                daily_cap=2,
                start_time="09:00",
                end_time="17:00",
            )
            for day in ("monday", "tuesday")
        )
        for contact_id in contact_ids:
            session.add(
                SendLog(
                    user_id=USER_ID,
                    campaign_id=campaign_id,
                    contact_id=contact_id,
                    recipient_id=contact_id,
                    sender_id=sender_ids[0],
                    recipient_email=f"lead{contact_id}@example.com",
                    sender_email="sender1@example.com",
                    subject="Subject",
                    status="sent",
                    sent_at=fixed_now - timedelta(minutes=5),
                )
            )
        session.commit()
        session.close()

        monkeypatch.setattr(settings_router, "utcnow", lambda: fixed_now)
        response = TestClient(app).patch(
            "/api/settings/timezone",
            json={"timezone": "Europe/Paris"},
            headers=HEADERS,
        )
        assert response.status_code == 200

        session = session_factory()
        campaign = session.get(Campaign, campaign_id)
        assert campaign.scheduled_at.replace(tzinfo=timezone.utc) == datetime(
            2026,
            7,
            14,
            7,
            0,
            tzinfo=timezone.utc,
        )
        assert campaign.send_settings["pause_reason"] == "campaign_daily_cap_reached"
        assert list(session.scalars(select(SendJob).where(SendJob.campaign_id == campaign_id))) == []
        session.close()
    finally:
        clear_session_override()


def test_send_logs_identify_retry_attempts(tmp_path):
    session_factory = make_session_factory(tmp_path)
    install_session_override(session_factory)
    try:
        campaign_id, sender_ids, contact_ids = _seed_delivery_campaign(
            session_factory,
            recipient_count=1,
        )
        session = session_factory()
        started_at = datetime(2026, 7, 14, 7, 0, tzinfo=timezone.utc)
        for index in range(3):
            session.add(
                SendLog(
                    user_id=USER_ID,
                    campaign_id=campaign_id,
                    contact_id=contact_ids[0],
                    recipient_id=contact_ids[0],
                    sender_id=sender_ids[0],
                    recipient_email="lead1@example.com",
                    sender_email="sender1@example.com",
                    subject="Subject",
                    status="failed",
                    error_message="Temporary Gmail error",
                    created_at=started_at + timedelta(minutes=index * 15),
                    updated_at=started_at + timedelta(minutes=index * 15),
                )
            )
        session.commit()
        session.close()

        response = TestClient(app).get(
            f"/api/campaigns/{campaign_id}/send-logs",
            headers=HEADERS,
        )
        assert response.status_code == 200
        items = response.json()["items"]
        assert [item["attempt_number"] for item in items] == [3, 2, 1]
        assert {item["attempt_count"] for item in items} == {3}
        assert {item["error_message"] for item in items} == {"Temporary Gmail error"}
    finally:
        clear_session_override()


def test_progress_does_not_count_recovered_attempt_as_terminal_failure(tmp_path):
    session_factory = make_session_factory(tmp_path)
    install_session_override(session_factory)
    try:
        campaign_id, sender_ids, contact_ids = _seed_delivery_campaign(
            session_factory,
            recipient_count=1,
        )
        session = session_factory()
        campaign = session.get(Campaign, campaign_id)
        campaign.status = "sending"
        recipient = session.get(
            CampaignRecipient,
            {"campaign_id": campaign_id, "contact_id": contact_ids[0]},
        )
        recipient.status = "sent"
        session.add_all(
            [
                SendLog(
                    user_id=USER_ID,
                    campaign_id=campaign_id,
                    contact_id=contact_ids[0],
                    recipient_id=contact_ids[0],
                    sender_id=sender_ids[0],
                    recipient_email="lead1@example.com",
                    sender_email="sender1@example.com",
                    subject="Subject",
                    status="failed",
                    error_message="Transient Gmail error",
                ),
                SendLog(
                    user_id=USER_ID,
                    campaign_id=campaign_id,
                    contact_id=contact_ids[0],
                    recipient_id=contact_ids[0],
                    sender_id=sender_ids[0],
                    recipient_email="lead1@example.com",
                    sender_email="sender1@example.com",
                    subject="Subject",
                    status="sent",
                    sent_at=utcnow(),
                ),
            ]
        )
        session.commit()
        session.close()

        response = TestClient(app).get(
            f"/api/campaigns/{campaign_id}/send-progress",
            headers=HEADERS,
        )
        assert response.status_code == 200
        assert response.json()["sent_count"] == 1
        assert response.json()["failed_count"] == 0
    finally:
        clear_session_override()


def test_delivery_rechecks_autopilot_cap_before_third_send(tmp_path, monkeypatch):
    fixed_now = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    session_factory = make_session_factory(tmp_path)
    campaign_id, sender_ids, contact_ids = _seed_delivery_campaign(
        session_factory,
        recipient_count=3,
    )
    session = session_factory()
    campaign = session.get(Campaign, campaign_id)
    campaign.status = "autopilot"
    campaign.send_settings = {"mode": "autopilot", "delay_minutes": 5}
    session.add_all(
        AutopilotDaySchedule(
            campaign_id=campaign_id,
            day_of_week=day,
            daily_cap=2,
            start_time="00:00",
            end_time="23:59",
        )
        for day in WEEKDAY_NAMES
    )
    for contact_id in contact_ids[:2]:
        session.get(
            CampaignRecipient,
            {"campaign_id": campaign_id, "contact_id": contact_id},
        ).status = "sent"
        session.add(
            SendLog(
                user_id=USER_ID,
                campaign_id=campaign_id,
                contact_id=contact_id,
                recipient_id=contact_id,
                sender_id=sender_ids[0],
                recipient_email=f"lead{contact_id}@example.com",
                sender_email="sender1@example.com",
                subject="Subject",
                status="sent",
                sent_at=fixed_now - timedelta(minutes=5),
            )
        )
    pending_contact_id = contact_ids[2]
    session.get(
        CampaignRecipient,
        {"campaign_id": campaign_id, "contact_id": pending_contact_id},
    ).status = "queued"
    job = SendJob(
        user_id=USER_ID,
        campaign_id=campaign_id,
        recipient_id=pending_contact_id,
        sender_id=sender_ids[0],
        status="running",
        scheduled_for=fixed_now - timedelta(days=1),
        locked_at=fixed_now,
        batch_id="cap-recheck",
        idempotency_key=f"campaign:{campaign_id}:recipient:{pending_contact_id}",
    )
    session.add(job)
    session.commit()
    job_id = job.id
    session.close()

    monkeypatch.setattr(platform_jobs, "SessionLocal", session_factory)
    monkeypatch.setattr(platform_jobs, "utcnow", lambda: fixed_now)
    send_email_mock = MagicMock()
    monkeypatch.setattr(platform_jobs, "send_email", send_email_mock)

    result = platform_jobs.perform_send_job(job_id, claimed=True)
    assert result["status"] == "deferred"
    assert result["reason_code"] == "campaign_daily_cap_reached"
    send_email_mock.assert_not_called()

    session = session_factory()
    assert session.get(SendJob, job_id) is None
    assert session.get(
        CampaignRecipient,
        {"campaign_id": campaign_id, "contact_id": pending_contact_id},
    ).status == "approved"
    scheduled_at = session.get(Campaign, campaign_id).scheduled_at
    assert scheduled_at.replace(tzinfo=timezone.utc) > fixed_now
    session.close()


def test_delivery_rechecks_autopilot_window_before_gmail(tmp_path, monkeypatch):
    fixed_now = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    session_factory = make_session_factory(tmp_path)
    campaign_id, sender_ids, contact_ids = _seed_delivery_campaign(
        session_factory,
        recipient_count=1,
    )
    session = session_factory()
    campaign = session.get(Campaign, campaign_id)
    campaign.status = "autopilot"
    campaign.send_settings = {"mode": "autopilot", "delay_minutes": 5}
    session.add(
        AutopilotDaySchedule(
            campaign_id=campaign_id,
            day_of_week="monday",
            daily_cap=10,
            start_time="09:00",
            end_time="10:00",
        )
    )
    recipient = session.get(
        CampaignRecipient,
        {"campaign_id": campaign_id, "contact_id": contact_ids[0]},
    )
    recipient.status = "queued"
    job = SendJob(
        user_id=USER_ID,
        campaign_id=campaign_id,
        recipient_id=contact_ids[0],
        sender_id=sender_ids[0],
        status="running",
        scheduled_for=fixed_now,
        locked_at=fixed_now,
        batch_id="window-recheck",
        idempotency_key=f"campaign:{campaign_id}:recipient:{contact_ids[0]}",
    )
    session.add(job)
    session.commit()
    job_id = job.id
    session.close()

    monkeypatch.setattr(platform_jobs, "SessionLocal", session_factory)
    monkeypatch.setattr(platform_jobs, "utcnow", lambda: fixed_now)
    send_email_mock = MagicMock()
    monkeypatch.setattr(platform_jobs, "send_email", send_email_mock)

    result = platform_jobs.perform_send_job(job_id, claimed=True)
    assert result["status"] == "deferred"
    assert result["reason_code"] == "autopilot_outside_window"
    send_email_mock.assert_not_called()

    session = session_factory()
    persisted_job = session.get(SendJob, job_id)
    assert persisted_job.status == "retry"
    assert persisted_job.scheduled_for.replace(tzinfo=timezone.utc) > fixed_now
    assert session.get(Campaign, campaign_id).status == "autopilot"
    session.close()


def test_retry_waits_for_sender_error_cooldown(tmp_path, monkeypatch):
    fixed_now = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    session_factory = make_session_factory(tmp_path)
    campaign_id, sender_ids, contact_ids = _seed_delivery_campaign(
        session_factory,
        recipient_count=1,
    )
    session = session_factory()
    campaign = session.get(Campaign, campaign_id)
    campaign.status = "sending"
    campaign.send_settings = {"mode": "send_now", "delay_minutes": 5}
    recipient = session.get(
        CampaignRecipient,
        {"campaign_id": campaign_id, "contact_id": contact_ids[0]},
    )
    recipient.status = "queued"
    job = SendJob(
        user_id=USER_ID,
        campaign_id=campaign_id,
        recipient_id=contact_ids[0],
        sender_id=sender_ids[0],
        status="queued",
        scheduled_for=fixed_now,
        batch_id="retry-cooldown",
        idempotency_key=f"campaign:{campaign_id}:recipient:{contact_ids[0]}",
    )
    session.add(job)
    session.commit()
    job_id = job.id
    session.close()

    monkeypatch.setattr(platform_jobs, "SessionLocal", session_factory)
    monkeypatch.setattr(platform_jobs, "utcnow", lambda: fixed_now)
    monkeypatch.setattr(platform_jobs, "gmail_service_for_sender", lambda *_args: object())
    monkeypatch.setattr(
        platform_jobs,
        "send_email",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("Temporary Gmail error")),
    )

    assert platform_jobs.perform_send_job(job_id)["status"] == "failed"
    session = session_factory()
    persisted_job = session.get(SendJob, job_id)
    assert persisted_job.status == "retry"
    assert persisted_job.scheduled_for.replace(tzinfo=timezone.utc) >= fixed_now + timedelta(minutes=15)
    session.close()


def test_claimed_job_observing_stop_restores_recipient(tmp_path, monkeypatch):
    session_factory = make_session_factory(tmp_path)
    campaign_id, sender_ids, contact_ids = _seed_delivery_campaign(
        session_factory,
        recipient_count=1,
    )
    session = session_factory()
    campaign = session.get(Campaign, campaign_id)
    campaign.status = "stopped"
    recipient = session.get(
        CampaignRecipient,
        {"campaign_id": campaign_id, "contact_id": contact_ids[0]},
    )
    recipient.status = "queued"
    job = SendJob(
        user_id=USER_ID,
        campaign_id=campaign_id,
        recipient_id=contact_ids[0],
        sender_id=sender_ids[0],
        status="running",
        scheduled_for=utcnow(),
        locked_at=utcnow(),
        batch_id="stopped-claim",
        idempotency_key=f"campaign:{campaign_id}:recipient:{contact_ids[0]}",
    )
    session.add(job)
    session.commit()
    job_id = job.id
    session.close()

    monkeypatch.setattr(platform_jobs, "SessionLocal", session_factory)
    result = platform_jobs.perform_send_job(job_id, claimed=True)
    assert result["status"] == "cancelled"

    session = session_factory()
    assert session.get(SendJob, job_id) is None
    assert session.get(
        CampaignRecipient,
        {"campaign_id": campaign_id, "contact_id": contact_ids[0]},
    ).status == "approved"
    session.close()


def test_dnc_added_after_queue_blocks_delivery(tmp_path, monkeypatch):
    session_factory = make_session_factory(tmp_path)
    campaign_id, sender_ids, contact_ids = _seed_delivery_campaign(
        session_factory,
        recipient_count=1,
    )
    session = session_factory()
    campaign = session.get(Campaign, campaign_id)
    campaign.status = "sending"
    contact = session.get(Contact, contact_ids[0])
    contact.status = "do_not_contact"
    recipient = session.get(
        CampaignRecipient,
        {"campaign_id": campaign_id, "contact_id": contact_ids[0]},
    )
    recipient.status = "queued"
    job = SendJob(
        user_id=USER_ID,
        campaign_id=campaign_id,
        recipient_id=contact_ids[0],
        sender_id=sender_ids[0],
        status="running",
        scheduled_for=utcnow(),
        locked_at=utcnow(),
        batch_id="dnc-recheck",
        idempotency_key=f"campaign:{campaign_id}:recipient:{contact_ids[0]}",
    )
    session.add(job)
    session.commit()
    job_id = job.id
    session.close()

    monkeypatch.setattr(platform_jobs, "SessionLocal", session_factory)
    send_email_mock = MagicMock()
    monkeypatch.setattr(platform_jobs, "send_email", send_email_mock)
    result = platform_jobs.perform_send_job(job_id, claimed=True)
    assert result["reason_code"] == "recipient_ineligible"
    send_email_mock.assert_not_called()

    session = session_factory()
    assert session.get(SendJob, job_id) is None
    assert session.get(
        CampaignRecipient,
        {"campaign_id": campaign_id, "contact_id": contact_ids[0]},
    ).status == "rejected"
    session.close()


def test_disconnected_assigned_sender_releases_job_for_group_reassignment(tmp_path, monkeypatch):
    session_factory = make_session_factory(tmp_path)
    campaign_id, sender_ids, contact_ids = _seed_delivery_campaign(
        session_factory,
        recipient_count=1,
    )
    session = session_factory()
    campaign = session.get(Campaign, campaign_id)
    campaign.status = "sending"
    first_sender = session.get(Sender, sender_ids[0])
    first_sender.status = "removed"
    first_sender.encrypted_oauth_credentials = None
    recipient = session.get(
        CampaignRecipient,
        {"campaign_id": campaign_id, "contact_id": contact_ids[0]},
    )
    recipient.status = "queued"
    job = SendJob(
        user_id=USER_ID,
        campaign_id=campaign_id,
        recipient_id=contact_ids[0],
        sender_id=sender_ids[0],
        status="running",
        scheduled_for=utcnow(),
        locked_at=utcnow(),
        batch_id="sender-reassignment",
        idempotency_key=f"campaign:{campaign_id}:recipient:{contact_ids[0]}",
    )
    session.add(job)
    session.commit()
    job_id = job.id
    session.close()

    monkeypatch.setattr(platform_jobs, "SessionLocal", session_factory)
    result = platform_jobs.perform_send_job(job_id, claimed=True)
    assert result["reason_code"] == "sender_unavailable"

    session = session_factory()
    replacement = create_send_jobs_for_next_batch(
        session,
        user_id=USER_ID,
        campaign_id=campaign_id,
    )
    session.commit()
    replacement_job = session.get(SendJob, replacement["job_ids"][0])
    assert replacement_job.sender_id == sender_ids[1]
    assert replacement_job.recipient_id == contact_ids[0]
    session.close()


def test_dry_run_never_builds_gmail_service_or_consumes_sender_cap(tmp_path, monkeypatch):
    session_factory = make_session_factory(tmp_path)
    install_session_override(session_factory)
    try:
        campaign_id, sender_ids, _ = _seed_delivery_campaign(
            session_factory,
            recipient_count=1,
        )
        response = TestClient(app).post(
            f"/api/campaigns/{campaign_id}/send-now",
            json={"delay_minutes": 0, "dry_run": True},
            headers=HEADERS,
        )
        assert response.status_code == 200

        monkeypatch.setattr(platform_jobs, "SessionLocal", session_factory)
        monkeypatch.setattr(platform_worker, "SessionLocal", session_factory)
        gmail_service_mock = MagicMock(side_effect=AssertionError("dry run touched Gmail"))
        monkeypatch.setattr(platform_jobs, "gmail_service_for_sender", gmail_service_mock)

        assert platform_worker.run_worker_cycle() == 1
        gmail_service_mock.assert_not_called()

        session = session_factory()
        log = session.scalar(select(SendLog).where(SendLog.campaign_id == campaign_id))
        assert log.status == "test_sent"
        assert platform_services.sender_sent_count_today(session, sender_ids[0]) == 0
        assert platform_services.campaign_sent_today(session, campaign_id) == 1
        session.close()
    finally:
        clear_session_override()


def test_final_send_failure_marks_recipient_failed(tmp_path, monkeypatch):
    session_factory = make_session_factory(tmp_path)
    session = session_factory()
    ensure_user(session, USER_ID)
    group = SenderGroup(user_id=USER_ID, name="Failed delivery")
    sender = Sender(
        user_id=USER_ID,
        group=group,
        email="failed-sender@example.com",
        status="connected",
        daily_cap=10,
        encrypted_oauth_credentials="encrypted",
    )
    campaign = Campaign(
        user_id=USER_ID,
        selected_sender_group=group,
        name="Failed delivery",
        subject_template="Subject",
        body_template="Body",
        status="sending",
    )
    contact = Contact(user_id=USER_ID, email_normalized="failed-lead@example.com", status="approved")
    session.add_all([group, sender, campaign, contact])
    session.flush()
    recipient = CampaignRecipient(campaign_id=campaign.id, contact_id=contact.id, status="queued")
    job = SendJob(
        user_id=USER_ID,
        campaign_id=campaign.id,
        recipient_id=contact.id,
        sender_id=sender.id,
        status="queued",
        max_attempts=1,
        scheduled_for=utcnow(),
        batch_id="failed-batch",
        idempotency_key=f"campaign:{campaign.id}:recipient:{contact.id}",
    )
    session.add_all([recipient, job])
    session.commit()
    job_id = job.id
    campaign_id = campaign.id
    contact_id = contact.id
    session.close()

    monkeypatch.setattr(platform_jobs, "SessionLocal", session_factory)
    monkeypatch.setattr(platform_jobs, "gmail_service_for_sender", lambda _session, _sender: object())
    monkeypatch.setattr(platform_jobs, "send_email", lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("Gmail failed")))

    result = platform_jobs.perform_send_job(job_id)
    assert result["status"] == "failed"
    session = session_factory()
    assert session.get(SendJob, job_id).status == "failed"
    assert session.get(CampaignRecipient, {"campaign_id": campaign_id, "contact_id": contact_id}).status == "failed"
    session.close()

def _seed_delivery_campaign(session_factory, *, recipient_count=4):
    session = session_factory()
    ensure_user(session, USER_ID)
    group = SenderGroup(user_id=USER_ID, name="Integration senders")
    session.add(group)
    session.flush()
    senders = [
        Sender(
            user_id=USER_ID,
            group_id=group.id,
            email=f"sender{index + 1}@example.com",
            display_name=f"Sender {index + 1}",
            status="connected",
            daily_cap=20,
            encrypted_oauth_credentials=f"encrypted-{index + 1}",
            scopes=["https://www.googleapis.com/auth/gmail.send"],
            connected_at=utcnow(),
        )
        for index in range(2)
    ]
    campaign = Campaign(
        user_id=USER_ID,
        selected_sender_group_id=group.id,
        name="HTTP delivery integration",
        subject_template="Hello {{ first_name }}",
        body_template="Message for {{ email }}",
        fallback_body_template="Fallback",
        status="draft",
    )
    session.add_all([*senders, campaign])
    session.flush()
    contacts = [
        Contact(
            user_id=USER_ID,
            email_normalized=f"lead{index + 1}@example.com",
            status="approved",
            custom_fields={"first_name": f"Lead {index + 1}"},
        )
        for index in range(recipient_count)
    ]
    session.add_all(contacts)
    session.flush()
    session.add_all(
        CampaignRecipient(campaign_id=campaign.id, contact_id=contact.id, status="approved")
        for contact in contacts
    )
    session.commit()
    result = campaign.id, [sender.id for sender in senders], [contact.id for contact in contacts]
    session.close()
    return result


def _install_fake_delivery(monkeypatch, session_factory, sent_requests):
    monkeypatch.setattr(platform_jobs, "SessionLocal", session_factory)
    monkeypatch.setattr(platform_worker, "SessionLocal", session_factory)
    monkeypatch.setattr(platform_jobs, "gmail_service_for_sender", lambda _session, _sender: object())

    def fake_send_email(**kwargs):
        sent_requests.append(kwargs)
        sequence = len(sent_requests)
        return SimpleNamespace(message_id=f"gmail-message-{sequence}", thread_id=f"gmail-thread-{sequence}")

    monkeypatch.setattr(platform_jobs, "send_email", fake_send_email)


def test_active_campaign_rejects_new_start_modes_and_invalid_controls(tmp_path):
    session_factory = make_session_factory(tmp_path)
    install_session_override(session_factory)
    try:
        campaign_id, _, _ = _seed_delivery_campaign(session_factory, recipient_count=1)
        session = session_factory()
        campaign = session.get(Campaign, campaign_id)
        campaign.status = "sending"
        session.commit()
        session.close()

        client = TestClient(app)
        assert client.post(f"/api/campaigns/{campaign_id}/send-now", json={}, headers=HEADERS).status_code == 409
        assert client.post(
            f"/api/campaigns/{campaign_id}/schedule",
            json={"scheduled_at": (utcnow() + timedelta(hours=1)).isoformat()},
            headers=HEADERS,
        ).status_code == 409
        assert client.post(
            f"/api/campaigns/{campaign_id}/autopilot/start",
            json={"schedule": all_day_schedule()},
            headers=HEADERS,
        ).status_code == 409
        assert client.post(f"/api/campaigns/{campaign_id}/resume", headers=HEADERS).status_code == 409

        assert client.post(f"/api/campaigns/{campaign_id}/pause", headers=HEADERS).status_code == 200
        assert client.post(f"/api/campaigns/{campaign_id}/pause", headers=HEADERS).status_code == 200
        assert client.post(f"/api/campaigns/{campaign_id}/resume", headers=HEADERS).status_code == 200
    finally:
        clear_session_override()


def test_resuming_a_completed_campaign_queues_only_new_recipients(tmp_path):
    session_factory = make_session_factory(tmp_path)
    install_session_override(session_factory)
    try:
        campaign_id, _, original_contact_ids = _seed_delivery_campaign(session_factory, recipient_count=2)
        session = session_factory()
        campaign = session.get(Campaign, campaign_id)
        campaign.status = "ended"
        campaign.send_settings = {"mode": "send_now", "delay_minutes": 5}
        for contact_id in original_contact_ids:
            session.get(
                CampaignRecipient,
                {"campaign_id": campaign_id, "contact_id": contact_id},
            ).status = "sent"
        added_contact = Contact(
            user_id=USER_ID,
            email_normalized="added-after-completion@example.com",
            status="approved",
            custom_fields={"first_name": "Added"},
        )
        session.add(added_contact)
        session.flush()
        session.add(
            CampaignRecipient(
                campaign_id=campaign_id,
                contact_id=added_contact.id,
                status="approved",
            )
        )
        session.commit()
        added_contact_id = added_contact.id
        session.close()

        client = TestClient(app)
        progress = client.get(
            f"/api/campaigns/{campaign_id}/send-progress",
            headers=HEADERS,
        ).json()
        assert progress["campaign_status"] == "ended"
        assert progress["resumable"] is True

        response = client.post(f"/api/campaigns/{campaign_id}/resume", headers=HEADERS)
        assert response.status_code == 200
        assert response.json()["status"] == "sending"
        assert response.json()["queued"] == 1

        session = session_factory()
        assert session.get(Campaign, campaign_id).status == "sending"
        jobs = list(session.scalars(select(SendJob).where(SendJob.campaign_id == campaign_id)))
        assert [job.recipient_id for job in jobs] == [added_contact_id]
        assert session.get(
            CampaignRecipient,
            {"campaign_id": campaign_id, "contact_id": added_contact_id},
        ).status == "queued"
        for contact_id in original_contact_ids:
            assert session.get(
                CampaignRecipient,
                {"campaign_id": campaign_id, "contact_id": contact_id},
            ).status == "sent"
        session.close()
    finally:
        clear_session_override()


def test_completed_campaign_without_new_recipients_cannot_resume(tmp_path):
    session_factory = make_session_factory(tmp_path)
    install_session_override(session_factory)
    try:
        campaign_id, _, contact_ids = _seed_delivery_campaign(session_factory, recipient_count=2)
        session = session_factory()
        campaign = session.get(Campaign, campaign_id)
        campaign.status = "ended"
        for contact_id in contact_ids:
            session.get(
                CampaignRecipient,
                {"campaign_id": campaign_id, "contact_id": contact_id},
            ).status = "sent"
        session.commit()
        session.close()

        client = TestClient(app)
        progress = client.get(
            f"/api/campaigns/{campaign_id}/send-progress",
            headers=HEADERS,
        ).json()
        assert progress["resumable"] is False

        response = client.post(f"/api/campaigns/{campaign_id}/resume", headers=HEADERS)
        assert response.status_code == 409
        assert "already has a delivery result" in response.json()["detail"]

        session = session_factory()
        assert session.get(Campaign, campaign_id).status == "ended"
        assert session.scalar(select(SendJob.id).where(SendJob.campaign_id == campaign_id)) is None
        session.close()
    finally:
        clear_session_override()


def test_blocked_sender_day_does_not_retire_campaign_that_still_has_recipients(tmp_path):
    session_factory = make_session_factory(tmp_path)
    install_session_override(session_factory)
    try:
        campaign_id, sender_ids, _ = _seed_delivery_campaign(session_factory, recipient_count=2)
        session = session_factory()
        campaign = session.get(Campaign, campaign_id)
        campaign.status = "sending"
        campaign.send_settings = {"mode": "send_now", "delay_minutes": 0}
        for sender_id in sender_ids:
            sender = session.get(Sender, sender_id)
            sender.daily_cap = 1
            sender.recent_error_at = utcnow()
        session.commit()
        session.close()

        response = TestClient(app).post(
            f"/api/campaigns/{campaign_id}/send-now",
            json={"dry_run": True},
            headers=HEADERS,
        )
        assert response.status_code == 409

        session = session_factory()
        campaign = session.get(Campaign, campaign_id)
        # The campaign must stay recoverable: it neither ends nor loses the
        # recipients the senders could not take today.
        assert campaign.status == "sending"
        assert session.scalar(select(SendJob.id).where(SendJob.campaign_id == campaign_id)) is None
        assert session.scalar(
            select(func.count())
            .select_from(CampaignRecipient)
            .where(
                CampaignRecipient.campaign_id == campaign_id,
                CampaignRecipient.status == "approved",
            )
        ) == 2
        session.close()
    finally:
        clear_session_override()


def test_batch_creation_only_reports_exhausted_without_eligible_recipients(tmp_path):
    """A blocked pass is not "exhausted"; one with work left queued is."""
    session_factory = make_session_factory(tmp_path)
    try:
        campaign_id, sender_ids, contact_ids = _seed_delivery_campaign(session_factory, recipient_count=2)
        session = session_factory()
        campaign = session.get(Campaign, campaign_id)
        campaign.status = "sending"
        for contact_id in contact_ids:
            session.get(
                CampaignRecipient,
                {"campaign_id": campaign_id, "contact_id": contact_id},
            ).status = "sent"
        session.add_all(
            [
                SendJob(
                    user_id=USER_ID,
                    campaign_id=campaign_id,
                    recipient_id=contact_ids[0],
                    sender_id=sender_ids[0],
                    status="sent",
                    scheduled_for=utcnow(),
                    batch_id="exhausted-sent",
                    idempotency_key=f"campaign:{campaign_id}:recipient:{contact_ids[0]}",
                ),
                SendJob(
                    user_id=USER_ID,
                    campaign_id=campaign_id,
                    recipient_id=contact_ids[1],
                    sender_id=sender_ids[1],
                    status="failed",
                    scheduled_for=utcnow(),
                    batch_id="exhausted-failed",
                    idempotency_key=f"campaign:{campaign_id}:recipient:{contact_ids[1]}",
                ),
            ]
        )
        session.commit()

        family_result = create_send_jobs_for_next_batch(
            session,
            user_id=USER_ID,
            campaign_id=campaign_id,
        )
        assert family_result["exhausted"] is True

        added_contact = Contact(
            user_id=USER_ID,
            email_normalized="added-later@example.com",
            status="approved",
            custom_fields={"first_name": "Later"},
        )
        session.add(added_contact)
        session.flush()
        added_contact_id = added_contact.id
        session.add(
            CampaignRecipient(
                campaign_id=campaign_id,
                contact_id=added_contact_id,
                status="approved",
            )
        )
        session.commit()

        added_result = create_send_jobs_for_next_batch(
            session,
            user_id=USER_ID,
            campaign_id=campaign_id,
        )
        assert added_result["exhausted"] is False
        assert added_result["created"] == 1
        assert added_result["job_ids"]
        session.commit()
        session.close()

        session = session_factory()
        queued_recipient = session.get(
            CampaignRecipient,
            {"campaign_id": campaign_id, "contact_id": added_contact_id},
        )
        assert queued_recipient.status == "queued"
        session.close()
    finally:
        clear_session_override()


def test_finished_campaign_stays_finished_while_senders_are_unavailable(tmp_path):
    """A blocked resume must not turn a completed campaign into a paused one."""
    session_factory = make_session_factory(tmp_path)
    install_session_override(session_factory)
    try:
        campaign_id, sender_ids, contact_ids = _seed_delivery_campaign(session_factory, recipient_count=1)
        session = session_factory()
        campaign = session.get(Campaign, campaign_id)
        campaign.status = "ended"
        campaign.send_settings = {"mode": "send_now", "delay_minutes": 5}
        session.get(
            CampaignRecipient,
            {"campaign_id": campaign_id, "contact_id": contact_ids[0]},
        ).status = "sent"
        added_contact = Contact(
            user_id=USER_ID,
            email_normalized="blocked-resume@example.com",
            status="approved",
            custom_fields={"first_name": "Blocked"},
        )
        session.add(added_contact)
        session.flush()
        session.add(
            CampaignRecipient(
                campaign_id=campaign_id,
                contact_id=added_contact.id,
                status="approved",
            )
        )
        for sender_id in sender_ids:
            sender = session.get(Sender, sender_id)
            sender.daily_cap = 1
            sender.recent_error_at = utcnow()
        session.commit()
        session.close()

        response = TestClient(app).post(f"/api/campaigns/{campaign_id}/resume", headers=HEADERS)
        assert response.status_code == 409

        session = session_factory()
        campaign = session.get(Campaign, campaign_id)
        assert campaign.status == "ended"
        assert campaign.send_settings["pause_reason"] == "senders_temporarily_unavailable"
        assert session.scalar(select(SendJob.id).where(SendJob.campaign_id == campaign_id)) is None
        session.close()
    finally:
        clear_session_override()


def test_stop_cancels_queued_jobs_but_preserves_claimed_job(tmp_path):
    session_factory = make_session_factory(tmp_path)
    install_session_override(session_factory)
    try:
        campaign_id, sender_ids, contact_ids = _seed_delivery_campaign(session_factory, recipient_count=2)
        session = session_factory()
        campaign = session.get(Campaign, campaign_id)
        campaign.status = "sending"
        for contact_id in contact_ids:
            session.get(
                CampaignRecipient,
                {"campaign_id": campaign_id, "contact_id": contact_id},
            ).status = "queued"
        session.add_all(
            [
                SendJob(
                    user_id=USER_ID,
                    campaign_id=campaign_id,
                    recipient_id=contact_ids[0],
                    sender_id=sender_ids[0],
                    status="queued",
                    scheduled_for=utcnow(),
                    batch_id="stop-queued",
                    idempotency_key=f"campaign:{campaign_id}:recipient:{contact_ids[0]}",
                ),
                SendJob(
                    user_id=USER_ID,
                    campaign_id=campaign_id,
                    recipient_id=contact_ids[1],
                    sender_id=sender_ids[1],
                    status="running",
                    scheduled_for=utcnow(),
                    locked_at=utcnow(),
                    batch_id="stop-running",
                    idempotency_key=f"campaign:{campaign_id}:recipient:{contact_ids[1]}",
                ),
            ]
        )
        session.commit()
        session.close()

        response = TestClient(app).post(f"/api/campaigns/{campaign_id}/stop", headers=HEADERS)
        assert response.status_code == 200
        assert response.json() == {"status": "stopped", "cancelled": 1, "in_flight": 1}

        session = session_factory()
        jobs = list(session.scalars(select(SendJob).where(SendJob.campaign_id == campaign_id)))
        assert len(jobs) == 1
        assert jobs[0].status == "running"
        assert session.get(Campaign, campaign_id).status == "stopped"
        assert session.get(
            CampaignRecipient,
            {"campaign_id": campaign_id, "contact_id": contact_ids[0]},
        ).status == "approved"
        session.close()
    finally:
        clear_session_override()


def test_resuming_future_schedule_keeps_original_start_time(tmp_path):
    session_factory = make_session_factory(tmp_path)
    install_session_override(session_factory)
    try:
        campaign_id, _, _ = _seed_delivery_campaign(session_factory, recipient_count=1)
        due = utcnow() + timedelta(hours=2)
        session = session_factory()
        campaign = session.get(Campaign, campaign_id)
        campaign.status = "paused"
        campaign.scheduled_at = due
        campaign.send_settings = {"mode": "schedule", "delay_minutes": 5}
        session.commit()
        session.close()

        response = TestClient(app).post(f"/api/campaigns/{campaign_id}/resume", headers=HEADERS)
        assert response.status_code == 200
        assert response.json()["status"] == "scheduled"
        assert response.json()["queued"] == 0

        session = session_factory()
        campaign = session.get(Campaign, campaign_id)
        assert campaign.status == "scheduled"
        assert campaign.scheduled_at.replace(tzinfo=due.tzinfo).timestamp() == due.timestamp()
        assert session.scalar(select(SendJob.id).where(SendJob.campaign_id == campaign_id)) is None
        session.close()
    finally:
        clear_session_override()


def test_send_now_http_runs_two_sender_batches_through_fake_gmail(tmp_path, monkeypatch):
    session_factory = make_session_factory(tmp_path)
    install_session_override(session_factory)
    sent_requests = []
    _install_fake_delivery(monkeypatch, session_factory, sent_requests)
    try:
        campaign_id, sender_ids, contact_ids = _seed_delivery_campaign(session_factory)
        session = session_factory()
        campaign = session.get(Campaign, campaign_id)
        campaign.attachment_metadata = {
            "storage": "database",
            "count": 2,
        }
        session.add_all(
            [
            CampaignAttachment(
                campaign_id=campaign_id,
                filename="resume.pdf",
                content_type="application/pdf",
                size_bytes=18,
                sha256="test-sha",
                content=b"%PDF stored resume",
            ),
            CampaignAttachment(
                campaign_id=campaign_id,
                filename="portfolio.pdf",
                content_type="application/pdf",
                size_bytes=21,
                sha256="portfolio-sha",
                content=b"%PDF stored portfolio",
            ),
            ]
        )
        session.commit()
        session.close()
        response = TestClient(app).post(
            f"/api/campaigns/{campaign_id}/send-now",
            json={"delay_minutes": 3},
            headers=HEADERS,
        )
        assert response.status_code == 200
        assert response.json()["queued"] == 2
        assert sent_requests == []
        assert platform_worker.run_worker_cycle() == 2
        assert [request["sender"] for request in sent_requests] == [
            "sender1@example.com",
            "sender2@example.com",
        ]
        assert all(
            [attachment.filename for attachment in request["attachments"]] == ["resume.pdf", "portfolio.pdf"]
            for request in sent_requests
        )

        progress = TestClient(app).get(
            f"/api/campaigns/{campaign_id}/send-progress",
            headers=HEADERS,
        )
        assert progress.status_code == 200
        progress_data = progress.json()
        assert progress_data["campaign_status"] == "sending"
        assert progress_data["is_active"] is True
        assert progress_data["is_waiting"] is True
        assert progress_data["is_sending"] is False
        assert progress_data["sent_count"] == 2
        assert progress_data["next_batch_at"] is not None
        assert progress_data["delay_minutes"] == 3
        assert [sender["campaign_sent"] for sender in progress_data["senders"]] == [1, 1]
        assert all(sender["remaining_today"] == 19 for sender in progress_data["senders"])

        session = session_factory()
        campaign = session.get(Campaign, campaign_id)
        campaign.scheduled_at = utcnow() - timedelta(seconds=1)
        session.commit()
        session.close()
        assert platform_worker.run_worker_cycle() == 2

        session = session_factory()

        jobs = list(session.scalars(select(SendJob).order_by(SendJob.id)))
        logs = list(session.scalars(select(SendLog).order_by(SendLog.id)))
        assert [(job.sender_id, job.recipient_id) for job in jobs] == [
            (sender_ids[0], contact_ids[0]),
            (sender_ids[1], contact_ids[1]),
            (sender_ids[0], contact_ids[2]),
            (sender_ids[1], contact_ids[3]),
        ]
        assert len(logs) == 4
        assert all(log.status == "sent" and log.gmail_message_id for log in logs)
        assert len(sent_requests) == 4
        campaign = session.get(Campaign, campaign_id)
        assert campaign.status == "ended"
        assert campaign.scheduled_at is None
        session.close()
    finally:
        clear_session_override()


def test_schedule_http_waits_for_scheduler_then_sends_through_fake_gmail(tmp_path, monkeypatch):
    session_factory = make_session_factory(tmp_path)
    install_session_override(session_factory)
    sent_requests = []
    _install_fake_delivery(monkeypatch, session_factory, sent_requests)
    try:
        campaign_id, _, _ = _seed_delivery_campaign(session_factory, recipient_count=2)
        due = utcnow() - timedelta(seconds=1)
        response = TestClient(app).post(
            f"/api/campaigns/{campaign_id}/schedule",
            json={"delay_minutes": 4, "scheduled_at": due.isoformat()},
            headers=HEADERS,
        )
        assert response.status_code == 200
        assert response.json()["status"] == "scheduled"
        assert sent_requests == []

        session = session_factory()
        session.close()
        assert platform_worker.run_worker_cycle() == 2

        session = session_factory()
        assert len(sent_requests) == 2
        assert session.scalar(select(Campaign).where(Campaign.id == campaign_id)).send_settings["delay_minutes"] == 4
        assert session.scalar(select(SendLog).where(SendLog.campaign_id == campaign_id).limit(1)).status == "sent"
        session.close()
    finally:
        clear_session_override()


def test_autopilot_http_starts_and_sends_due_batch_through_fake_gmail(tmp_path, monkeypatch):
    session_factory = make_session_factory(tmp_path)
    install_session_override(session_factory)
    sent_requests = []
    _install_fake_delivery(monkeypatch, session_factory, sent_requests)
    try:
        campaign_id, _, _ = _seed_delivery_campaign(session_factory, recipient_count=2)
        due = utcnow() - timedelta(seconds=1)
        response = TestClient(app).post(
            f"/api/campaigns/{campaign_id}/autopilot/start",
            json={
                "schedule": all_day_schedule(),
                "delay_minutes": 6,
                "pacing_mode": "spread_evenly",
                "scheduled_at": due.isoformat(),
            },
            headers=HEADERS,
        )
        assert response.status_code == 200
        assert response.json()["status"] == "autopilot"
        assert sent_requests == []

        session = session_factory()
        session.close()
        assert platform_worker.run_worker_cycle() == 2

        session = session_factory()
        campaign = session.get(Campaign, campaign_id)
        assert len(sent_requests) == 2
        assert campaign.status == "ended"
        assert campaign.scheduled_at is None
        assert campaign.send_settings["mode"] == "autopilot"
        assert campaign.send_settings["delay_minutes"] == 6
        assert campaign.send_settings["pacing_mode"] == "spread_evenly"
        assert len(list(session.scalars(select(SendLog).where(SendLog.campaign_id == campaign_id)))) == 2
        session.close()
    finally:
        clear_session_override()


def test_autopilot_rejects_empty_unknown_and_invalid_day_schedules(tmp_path):
    session_factory = make_session_factory(tmp_path)
    install_session_override(session_factory)
    try:
        campaign_id, _, _ = _seed_delivery_campaign(session_factory, recipient_count=1)
        client = TestClient(app)
        endpoint = f"/api/campaigns/{campaign_id}/autopilot/start"

        empty = client.post(endpoint, json={"schedule": {}}, headers=HEADERS)
        unknown = client.post(
            endpoint,
            json={"schedule": {"funday": {"cap": 2, "start": "09:00", "end": "17:00"}}},
            headers=HEADERS,
        )
        reversed_window = client.post(
            endpoint,
            json={"schedule": {"monday": {"cap": 2, "start": "17:00", "end": "09:00"}}},
            headers=HEADERS,
        )
        invalid_pacing = client.post(
            endpoint,
            json={"schedule": all_day_schedule(), "pacing_mode": "as_fast_as_possible"},
            headers=HEADERS,
        )

        assert empty.status_code == 422
        assert unknown.status_code == 422
        assert reversed_window.status_code == 422
        assert invalid_pacing.status_code == 422
    finally:
        clear_session_override()


def test_autopilot_daily_limits_update_live_and_report_capacity(tmp_path):
    session_factory = make_session_factory(tmp_path)
    install_session_override(session_factory)
    try:
        campaign_id, sender_ids, contact_ids = _seed_delivery_campaign(
            session_factory,
            recipient_count=2,
        )
        now = utcnow()
        today = WEEKDAY_NAMES[now.weekday()]
        session = session_factory()
        campaign = session.get(Campaign, campaign_id)
        campaign.status = "autopilot"
        campaign.send_settings = {"mode": "autopilot", "delay_minutes": 10}
        campaign.scheduled_at = now + timedelta(minutes=10)
        session.add(
            AutopilotDaySchedule(
                campaign_id=campaign_id,
                day_of_week=today,
                daily_cap=4,
                start_time="00:00",
                end_time="23:59",
            )
        )
        session.add(
            SendLog(
                user_id=USER_ID,
                campaign_id=campaign_id,
                contact_id=contact_ids[0],
                recipient_id=contact_ids[0],
                sender_id=sender_ids[0],
                recipient_email="lead1@example.com",
                sender_email="sender1@example.com",
                subject="Subject",
                status="sent",
                sent_at=now,
            )
        )
        session.add(
            SendJob(
                user_id=USER_ID,
                campaign_id=campaign_id,
                recipient_id=contact_ids[1],
                sender_id=sender_ids[0],
                status="queued",
                scheduled_for=now,
                batch_id="live-limit-test",
                idempotency_key=f"live-limit:{campaign_id}:{contact_ids[1]}",
            )
        )
        session.commit()
        session.close()

        client = TestClient(app)
        response = client.patch(
            f"/api/campaigns/{campaign_id}/autopilot/daily-limits",
            json={"daily_limits": {today: 7}},
            headers=HEADERS,
        )
        assert response.status_code == 200
        assert response.json()["daily_limits"][today] == 7
        assert response.json()["capacity"] == {
            "cap": 7,
            "sent": 1,
            "reserved": 1,
            "remaining": 5,
        }

        progress = client.get(
            f"/api/campaigns/{campaign_id}/send-progress",
            headers=HEADERS,
        )
        assert progress.status_code == 200
        assert {
            key: progress.json()[key]
            for key in (
                "campaign_daily_cap",
                "campaign_sent_today",
                "campaign_reserved_today",
                "campaign_remaining_today",
            )
        } == {
            "campaign_daily_cap": 7,
            "campaign_sent_today": 1,
            "campaign_reserved_today": 1,
            "campaign_remaining_today": 5,
        }

        session = session_factory()
        campaign = session.get(Campaign, campaign_id)
        assert campaign.status == "autopilot"
        assert campaign.scheduled_at.replace(tzinfo=timezone.utc) >= now
        assert session.scalar(
            select(AutopilotDaySchedule.daily_cap).where(
                AutopilotDaySchedule.campaign_id == campaign_id,
                AutopilotDaySchedule.day_of_week == today,
            )
        ) == 7
        session.close()

        # Pace can also be adjusted without pausing; launch mode stays protected.
        updated = client.patch(
            f"/api/campaigns/{campaign_id}/send-settings",
            json={"delay_minutes": 1},
            headers=HEADERS,
        )
        assert updated.status_code == 200
        blocked = client.patch(
            f"/api/campaigns/{campaign_id}/send-settings",
            json={"mode": "send_now"}, headers=HEADERS,
        )
        assert blocked.status_code == 409
    finally:
        clear_session_override()


def test_autopilot_stops_at_today_cap_then_resumes_next_day(tmp_path, monkeypatch):
    session_factory = make_session_factory(tmp_path)
    install_session_override(session_factory)
    sent_requests = []
    _install_fake_delivery(monkeypatch, session_factory, sent_requests)
    try:
        campaign_id, sender_ids, contact_ids = _seed_delivery_campaign(session_factory, recipient_count=3)
        session = session_factory()
        senders = list(session.scalars(select(Sender).where(Sender.id.in_(sender_ids)).order_by(Sender.id)))
        senders[0].daily_cap = 10
        senders[1].status = "removed"
        senders[1].encrypted_oauth_credentials = None
        session.commit()
        session.close()

        response = TestClient(app).post(
            f"/api/campaigns/{campaign_id}/autopilot/start",
            json={
                "schedule": all_day_schedule(cap=2),
                "delay_minutes": 0,
                "scheduled_at": (utcnow() - timedelta(seconds=1)).isoformat(),
            },
            headers=HEADERS,
        )
        assert response.status_code == 200

        assert platform_worker.run_worker_cycle() == 1
        assert platform_worker.run_worker_cycle() == 1

        session = session_factory()
        campaign = session.get(Campaign, campaign_id)
        assert campaign.send_settings["pause_reason"] == "campaign_daily_cap_reached"
        assert campaign.scheduled_at.replace(tzinfo=timezone.utc) > utcnow()
        session.close()

        assert platform_worker.run_worker_cycle() == 0

        session = session_factory()
        campaign = session.get(Campaign, campaign_id)
        statuses = list(
            session.scalars(
                select(CampaignRecipient.status)
                .where(CampaignRecipient.campaign_id == campaign_id)
                .order_by(CampaignRecipient.contact_id)
            )
        )
        next_day = campaign.scheduled_at + timedelta(seconds=1)
        assert campaign.status == "autopilot"
        assert campaign.send_settings["pause_reason"] == "campaign_daily_cap_reached"
        assert statuses == ["sent", "sent", "approved"]
        assert len(sent_requests) == 2
        session.close()

        monkeypatch.setattr(platform_scheduler, "utcnow", lambda: next_day)
        monkeypatch.setattr(platform_worker, "utcnow", lambda: next_day)
        monkeypatch.setattr(platform_services, "utcnow", lambda: next_day)
        monkeypatch.setattr(platform_jobs, "utcnow", lambda: next_day)
        assert platform_worker.run_worker_cycle() == 1
        assert len(sent_requests) == 3

        session = session_factory()
        campaign = session.get(Campaign, campaign_id)
        campaign.scheduled_at = next_day
        session.commit()
        session.close()
        assert platform_worker.run_worker_cycle() == 0

        session = session_factory()
        assert session.get(Campaign, campaign_id).status == "ended"
        assert {
            row.status
            for row in session.scalars(
                select(CampaignRecipient).where(CampaignRecipient.contact_id.in_(contact_ids))
            )
        } == {"sent"}
        session.close()
    finally:
        clear_session_override()


def test_autopilot_waits_full_three_minutes_between_fake_gmail_batches(tmp_path, monkeypatch):
    session_factory = make_session_factory(tmp_path)
    install_session_override(session_factory)
    sent_requests = []
    _install_fake_delivery(monkeypatch, session_factory, sent_requests)
    try:
        campaign_id, sender_ids, contact_ids = _seed_delivery_campaign(session_factory, recipient_count=4)
        response = TestClient(app).post(
            f"/api/campaigns/{campaign_id}/autopilot/start",
            json={
                "schedule": all_day_schedule(),
                "delay_minutes": 3,
                "scheduled_at": (utcnow() - timedelta(seconds=1)).isoformat(),
            },
            headers=HEADERS,
        )
        assert response.status_code == 200

        session = session_factory()
        session.close()
        assert platform_worker.run_worker_cycle() == 2

        session = session_factory()
        assert len(sent_requests) == 2
        next_batch_at = session.get(Campaign, campaign_id).scheduled_at

        monkeypatch.setattr(
            platform_scheduler,
            "utcnow",
            lambda: next_batch_at - timedelta(seconds=1),
        )
        monkeypatch.setattr(
            platform_worker,
            "utcnow",
            lambda: next_batch_at - timedelta(seconds=1),
        )
        session.close()
        assert platform_worker.run_worker_cycle() == 0
        assert len(sent_requests) == 2

        monkeypatch.setattr(
            platform_scheduler,
            "utcnow",
            lambda: next_batch_at + timedelta(seconds=1),
        )
        monkeypatch.setattr(
            platform_worker,
            "utcnow",
            lambda: next_batch_at + timedelta(seconds=1),
        )
        assert platform_worker.run_worker_cycle() == 2
        assert len(sent_requests) == 4

        session = session_factory()
        jobs = list(session.scalars(select(SendJob).order_by(SendJob.id)))
        assert [(job.sender_id, job.recipient_id) for job in jobs] == [
            (sender_ids[0], contact_ids[0]),
            (sender_ids[1], contact_ids[1]),
            (sender_ids[0], contact_ids[2]),
            (sender_ids[1], contact_ids[3]),
        ]
        assert len(list(session.scalars(select(SendLog).where(SendLog.campaign_id == campaign_id)))) == 4
        session.close()
    finally:
        clear_session_override()


def test_autopilot_spreads_fake_gmail_batches_across_daily_window(tmp_path, monkeypatch):
    session_factory = make_session_factory(tmp_path)
    install_session_override(session_factory)
    sent_requests = []
    _install_fake_delivery(monkeypatch, session_factory, sent_requests)
    clock = [datetime(2026, 7, 13, 9, 0, tzinfo=timezone.utc)]
    for module in (platform_jobs, platform_scheduler, platform_services, platform_worker):
        monkeypatch.setattr(module, "utcnow", lambda: clock[0])

    try:
        campaign_id, sender_ids, contact_ids = _seed_delivery_campaign(session_factory, recipient_count=6)
        schedule = {
            day: {"cap": 6, "start": "09:00", "end": "17:00"}
            for day in WEEKDAY_NAMES
        }
        response = TestClient(app).post(
            f"/api/campaigns/{campaign_id}/autopilot/start",
            json={
                "schedule": schedule,
                "pacing_mode": "spread_evenly",
                "scheduled_at": clock[0].isoformat(),
            },
            headers=HEADERS,
        )
        assert response.status_code == 200

        assert platform_worker.run_worker_cycle() == 2
        session = session_factory()
        first_next = session.get(Campaign, campaign_id).scheduled_at
        session.close()
        assert first_next.replace(tzinfo=timezone.utc) == datetime(2026, 7, 13, 11, 40, tzinfo=timezone.utc)

        clock[0] = first_next - timedelta(seconds=1)
        assert platform_worker.run_worker_cycle() == 0
        assert len(sent_requests) == 2

        clock[0] = first_next
        assert platform_worker.run_worker_cycle() == 2
        session = session_factory()
        second_next = session.get(Campaign, campaign_id).scheduled_at
        session.close()
        assert second_next.replace(tzinfo=timezone.utc) == datetime(2026, 7, 13, 14, 20, tzinfo=timezone.utc)

        clock[0] = second_next
        assert platform_worker.run_worker_cycle() == 2

        session = session_factory()
        campaign = session.get(Campaign, campaign_id)
        jobs = list(session.scalars(select(SendJob).order_by(SendJob.id)))
        assert campaign.status == "ended"
        assert campaign.scheduled_at is None
        assert [(job.sender_id, job.recipient_id) for job in jobs] == [
            (sender_ids[index % 2], contact_ids[index])
            for index in range(6)
        ]
        assert len(sent_requests) == 6
        assert len(list(session.scalars(select(SendLog).where(SendLog.campaign_id == campaign_id)))) == 6
        session.close()
    finally:
        clear_session_override()


def test_worker_recovers_interrupted_running_job(tmp_path, monkeypatch):
    session_factory = make_session_factory(tmp_path)
    monkeypatch.setattr(platform_worker, "SessionLocal", session_factory)
    campaign_id, sender_ids, contact_ids = _seed_delivery_campaign(session_factory, recipient_count=1)
    session = session_factory()
    recipient = session.get(
        CampaignRecipient,
        {"campaign_id": campaign_id, "contact_id": contact_ids[0]},
    )
    recipient.status = "queued"
    job = SendJob(
        user_id=USER_ID,
        campaign_id=campaign_id,
        recipient_id=contact_ids[0],
        sender_id=sender_ids[0],
        status="running",
        scheduled_for=utcnow() - timedelta(minutes=20),
        locked_at=utcnow() - timedelta(minutes=20),
        batch_id="interrupted-batch",
        idempotency_key=f"campaign:{campaign_id}:recipient:{contact_ids[0]}",
    )
    session.add(job)
    session.commit()
    job_id = job.id
    session.close()

    assert platform_worker.recover_stale_jobs(stale_after_minutes=10) == 1

    session = session_factory()
    recovered = session.get(SendJob, job_id)
    assert recovered.status == "retry"
    assert recovered.locked_at is None
    assert recovered.error_message == "Recovered after worker interruption"
    session.close()


def test_autopilot_compressed_day_resumes_after_five_minutes(tmp_path, monkeypatch):
    session_factory = make_session_factory(tmp_path)
    install_session_override(session_factory)
    sent_requests = []
    _install_fake_delivery(monkeypatch, session_factory, sent_requests)
    try:
        campaign_id, sender_ids, _ = _seed_delivery_campaign(session_factory, recipient_count=2)
        session = session_factory()
        senders = list(session.scalars(select(Sender).where(Sender.id.in_(sender_ids)).order_by(Sender.id)))
        senders[0].daily_cap = 1
        senders[1].status = "removed"
        senders[1].encrypted_oauth_credentials = None
        session.commit()
        session.close()

        start = utcnow()
        response = TestClient(app).post(
            f"/api/campaigns/{campaign_id}/autopilot/start",
            json={
                "schedule": all_day_schedule(),
                "delay_minutes": 0,
                "scheduled_at": (start - timedelta(seconds=1)).isoformat(),
            },
            headers=HEADERS,
        )
        assert response.status_code == 200
        assert platform_worker.run_worker_cycle() == 1
        assert len(sent_requests) == 1

        compressed_next_day = start + timedelta(minutes=5)
        monkeypatch.setattr(
            platform_scheduler,
            "next_autopilot_run",
            lambda _session, _campaign, **_kwargs: compressed_next_day,
        )
        session = session_factory()
        campaign = session.get(Campaign, campaign_id)
        campaign.scheduled_at = start - timedelta(seconds=1)
        session.commit()
        session.close()
        assert platform_worker.run_worker_cycle() == 0

        session = session_factory()
        campaign = session.get(Campaign, campaign_id)
        assert campaign.status == "autopilot"
        assert campaign.scheduled_at.replace(tzinfo=compressed_next_day.tzinfo).timestamp() == compressed_next_day.timestamp()
        session.close()

        before_next_day = compressed_next_day - timedelta(seconds=1)
        monkeypatch.setattr(platform_scheduler, "utcnow", lambda: before_next_day)
        monkeypatch.setattr(platform_worker, "utcnow", lambda: before_next_day)
        assert platform_worker.run_worker_cycle() == 0
        assert len(sent_requests) == 1

        after_next_day = compressed_next_day + timedelta(seconds=1)
        monkeypatch.setattr(platform_scheduler, "utcnow", lambda: after_next_day)
        monkeypatch.setattr(platform_worker, "utcnow", lambda: after_next_day)
        monkeypatch.setattr(platform_services, "utcnow", lambda: start + timedelta(days=1, seconds=1))
        monkeypatch.setattr(platform_jobs, "utcnow", lambda: start + timedelta(days=1, seconds=1))
        assert platform_worker.run_worker_cycle() == 1
        assert len(sent_requests) == 2

        session = session_factory()
        completed_campaign = session.get(Campaign, campaign_id)
        assert completed_campaign.status == "ended"
        assert completed_campaign.scheduled_at is None
        assert len(list(session.scalars(select(SendLog).where(SendLog.campaign_id == campaign_id)))) == 2
        session.close()
    finally:
        clear_session_override()


def test_database_url_uses_postgres_in_development(monkeypatch):
    monkeypatch.delenv("APP_DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@localhost/outreach")
    monkeypatch.setenv("APP_ENV", "development")
    assert platform_db.get_database_url() == "postgresql+psycopg2://user:pass@localhost/outreach"


def test_credential_decryption_reports_key_mismatch(monkeypatch):
    monkeypatch.setenv("APP_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    encrypted = encrypt_text("oauth-credentials")
    monkeypatch.setenv("APP_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))

    try:
        decrypt_text(encrypted)
        assert False, "Expected a key mismatch"
    except RuntimeError as exc:
        assert "APP_ENCRYPTION_KEY does not match" in str(exc)


def test_empty_delivery_exception_gets_actionable_detail():
    assert platform_jobs._exception_detail(RuntimeError()) == "RuntimeError: RuntimeError()"


def test_send_logs_filter_by_engagement_and_include_gmail_thread_ids(tmp_path):
    session_factory = make_session_factory(tmp_path)
    install_session_override(session_factory)
    try:
        campaign_id, sender_ids, contact_ids = _seed_delivery_campaign(
            session_factory,
            recipient_count=4,
        )
        session = session_factory()
        session.add_all(
            [
                SendLog(
                    user_id=USER_ID,
                    campaign_id=campaign_id,
                    contact_id=contact_id,
                    recipient_id=contact_id,
                    sender_id=sender_ids[0],
                    recipient_email=f"lead{index + 1}@example.com",
                    sender_email="sender1@example.com",
                    subject="Campaign subject",
                    status="sent",
                    gmail_thread_id=f"gmail-thread-{index + 1}",
                    open_count=1 if index in {0, 2} else 0,
                    click_count=1 if index in {1, 2} else 0,
                    response_status=(
                        "replied" if index == 2 else "automated_response" if index == 3 else None
                    ),
                    sent_at=utcnow(),
                )
                for index, contact_id in enumerate(contact_ids)
            ]
        )
        session.commit()
        session.close()

        client = TestClient(app)
        all_logs = client.get(
            f"/api/campaigns/{campaign_id}/send-logs",
            headers=HEADERS,
        ).json()
        clicked = client.get(
            f"/api/campaigns/{campaign_id}/send-logs?engagement=clicked&page_size=1",
            headers=HEADERS,
        ).json()
        opened = client.get(
            f"/api/campaigns/{campaign_id}/send-logs?engagement=opened",
            headers=HEADERS,
        ).json()
        replied = client.get(
            f"/api/campaigns/{campaign_id}/send-logs?engagement=replied",
            headers=HEADERS,
        ).json()

        assert all_logs["total"] == 4
        assert {item["gmail_thread_id"] for item in all_logs["items"]} == {
            f"gmail-thread-{index + 1}" for index in range(4)
        }
        assert clicked["total"] == 2
        assert clicked["pages"] == 2
        assert {item["recipient_email"] for item in clicked["items"]} <= {
            "lead2@example.com",
            "lead3@example.com",
        }
        assert {item["recipient_email"] for item in opened["items"]} == {
            "lead1@example.com",
            "lead3@example.com",
        }
        assert {item["recipient_email"] for item in replied["items"]} == {
            "lead3@example.com",
            "lead4@example.com",
        }
    finally:
        clear_session_override()


def test_stopped_campaign_resume_reuses_queued_jobs_after_an_error(tmp_path):
    session_factory = make_session_factory(tmp_path)
    install_session_override(session_factory)
    try:
        campaign_id, sender_ids, contact_ids = _seed_delivery_campaign(
            session_factory,
            recipient_count=3,
        )
        session = session_factory()
        campaign = session.get(Campaign, campaign_id)
        campaign.status = "stopped"
        campaign.send_settings = {"mode": "send_now", "delay_minutes": 5}
        session.get(
            CampaignRecipient,
            {"campaign_id": campaign_id, "contact_id": contact_ids[0]},
        ).status = "sent"
        session.get(
            CampaignRecipient,
            {"campaign_id": campaign_id, "contact_id": contact_ids[1]},
        ).status = "failed"
        session.get(
            CampaignRecipient,
            {"campaign_id": campaign_id, "contact_id": contact_ids[2]},
        ).status = "queued"
        session.add(
            SendJob(
                user_id=USER_ID,
                campaign_id=campaign_id,
                recipient_id=contact_ids[2],
                sender_id=sender_ids[0],
                status="queued",
                scheduled_for=utcnow(),
                batch_id="stopped-resume",
                idempotency_key=f"campaign:{campaign_id}:recipient:{contact_ids[2]}",
            )
        )
        session.commit()
        session.close()

        client = TestClient(app)
        progress = client.get(
            f"/api/campaigns/{campaign_id}/send-progress",
            headers=HEADERS,
        ).json()
        assert progress["resumable"] is True
        assert progress["queued_count"] == 1
        response = client.post(
            f"/api/campaigns/{campaign_id}/resume",
            headers=HEADERS,
        )
        assert response.status_code == 200
        assert response.json()["status"] == "sending"
        assert response.json()["queued"] == 1

        session = session_factory()
        assert session.get(Campaign, campaign_id).status == "sending"
        jobs = list(session.scalars(select(SendJob).where(SendJob.campaign_id == campaign_id)))
        assert len(jobs) == 1
        assert jobs[0].recipient_id == contact_ids[2]
        assert session.get(
            CampaignRecipient,
            {"campaign_id": campaign_id, "contact_id": contact_ids[0]},
        ).status == "sent"
        assert session.get(
            CampaignRecipient,
            {"campaign_id": campaign_id, "contact_id": contact_ids[1]},
        ).status == "failed"
        session.close()
    finally:
        clear_session_override()


def test_stopped_autopilot_resume_schedules_next_window(tmp_path, monkeypatch):
    fixed_now = datetime(2026, 7, 13, 7, 0, tzinfo=timezone.utc)
    next_window = datetime(2026, 7, 13, 9, 0, tzinfo=timezone.utc)
    session_factory = make_session_factory(tmp_path)
    install_session_override(session_factory)
    try:
        campaign_id, _, contact_ids = _seed_delivery_campaign(
            session_factory,
            recipient_count=2,
        )
        session = session_factory()
        campaign = session.get(Campaign, campaign_id)
        campaign.status = "stopped"
        campaign.timezone = "UTC"
        campaign.send_settings = {
            "mode": "autopilot",
            "delay_minutes": 5,
            "pacing_mode": "spread_evenly",
        }
        session.get(
            CampaignRecipient,
            {"campaign_id": campaign_id, "contact_id": contact_ids[0]},
        ).status = "sent"
        session.add(
            AutopilotDaySchedule(
                campaign_id=campaign_id,
                day_of_week="monday",
                daily_cap=40,
                start_time="09:00",
                end_time="16:30",
            )
        )
        session.commit()
        session.close()

        monkeypatch.setattr(campaign_delivery, "utcnow", lambda: fixed_now)
        monkeypatch.setattr(platform_jobs, "utcnow", lambda: fixed_now)

        response = TestClient(app).post(
            f"/api/campaigns/{campaign_id}/resume",
            headers=HEADERS,
        )

        assert response.status_code == 200
        assert response.json()["status"] == "autopilot"
        assert response.json()["queued"] == 0
        assert datetime.fromisoformat(response.json()["scheduled_at"]) == next_window

        session = session_factory()
        campaign = session.get(Campaign, campaign_id)
        assert campaign.status == "autopilot"
        assert campaign.scheduled_at.replace(tzinfo=timezone.utc) == next_window
        assert campaign.send_settings["pause_reason"] == "autopilot_outside_window"
        assert session.scalar(
            select(SendJob.id).where(SendJob.campaign_id == campaign_id)
        ) is None
        session.close()
    finally:
        clear_session_override()
