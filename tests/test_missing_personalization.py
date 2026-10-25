from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from api.routers import campaigns
from api.schemas import TestSendRequest as SendTestRequest
from src import preview, scheduler
from src.models import AppConfig
from src.template_engine import MissingTemplateVariablesError, render_email


USER_ID = "personalization-test"


@pytest.fixture
def campaign():
    return {
        "id": 42,
        "subject_template": "For {{company}}",
        "body_template": "Hi {{first_name}}, welcome to {{company}}.",
        "fallback_body_template": "",
    }


@pytest.fixture
def contact():
    return {
        "id": 7,
        "email": "lead@example.test",
        "first_name": "",
        "custom_fields": {"company": "Acme"},
    }


@pytest.fixture
def conn(campaign):
    connection = MagicMock()
    connection.execute.return_value.fetchone.return_value = campaign
    return connection


@pytest.mark.parametrize("value", [None, "", " \t\n", "absent"])
def test_render_reports_missing_values_and_strict_mode_rejects_them(campaign, contact, value):
    if value != "absent":
        contact["custom_fields"]["first_name"] = value
    rendered = render_email(contact, campaign)
    assert rendered.missing_variables == ["first_name"]
    assert rendered.body == "Hi [missing first_name], welcome to Acme."
    with pytest.raises(MissingTemplateVariablesError) as error:
        render_email(contact, campaign, strict=True)
    assert error.value.variables == ("first_name",)


def test_render_collects_missing_fields_in_subject_and_body_once(campaign, contact):
    contact["custom_fields"] = {}
    assert render_email(contact, campaign).missing_variables == ["company", "first_name"]


def test_render_validates_the_fallback_body_when_the_main_body_is_empty(campaign, contact):
    campaign["body_template"] = ""
    campaign["fallback_body_template"] = "Hi {{first_name}}"
    with pytest.raises(MissingTemplateVariablesError):
        render_email(contact, campaign, strict=True)


def test_zero_and_false_are_present_template_values(campaign, contact):
    contact["custom_fields"] = {"company": 0, "first_name": False}
    rendered = render_email(contact, campaign, strict=True)
    assert rendered.missing_variables == []
    assert rendered.body == "Hi False, welcome to 0."


def test_summary_counts_absent_headers_and_blank_cells_across_imports(monkeypatch, campaign):
    campaign["body_template"] += " {{unknown}} {{email}}"
    contacts = [
        {"email": "a@example.test", "custom_fields": {"first_name": "Alex", "company": "Acme"}},
        {"email": "b@example.test", "custom_fields": json.dumps({"company": "Beta", "first_name": " \t"})},
        {"email": "c@example.test", "custom_fields": {"company": None}},
        {"email": "d@example.test", "custom_fields": {"other": "value"}},
    ]
    monkeypatch.setattr(campaigns.db, "get_campaign", lambda *_: campaign)
    monkeypatch.setattr(campaigns.db, "campaign_contacts", lambda *_: contacts)

    result = campaigns.get_campaign_validation_summary(42, conn=object(), user_id=USER_ID)

    assert result == {
        "total_contacts": 4,
        "used_warnings": [
            {"column": "company", "empty_count": 2},
            {"column": "first_name", "empty_count": 3},
            {"column": "unknown", "empty_count": 4},
        ],
        "other_warnings": [{"column": "other", "empty_count": 3}],
        "all_columns": ["company", "email", "first_name", "other"],
    }


@pytest.mark.parametrize("fields,missing", [
    ({"company": "Acme"}, ["first_name"]),
    ({"company": "Acme", "first_name": "Alex"}, []),
    ({"company": "Acme", "First Name": "Alex"}, ["first_name"]),
])
def test_preview_exposes_explicit_send_eligibility(monkeypatch, campaign, contact, fields, missing):
    contact["custom_fields"] = fields
    monkeypatch.setattr(campaigns.db, "get_campaign", lambda *_: campaign)
    monkeypatch.setattr(campaigns.db, "campaign_contact_count", lambda *_: 1)
    conn = MagicMock()
    conn.execute.return_value.fetchall.return_value = [contact]
    session = MagicMock()
    session.scalars.return_value.all.return_value = []

    result = campaigns.get_campaign_preview(42, offset=0, limit=1, conn=conn, platform_session=session, user_id=USER_ID)

    assert result["items"][0]["missing_variables"] == missing
    assert result["items"][0]["can_send"] is (not missing)


@pytest.fixture
def mocked_delivery(monkeypatch, campaign, contact):
    monkeypatch.setattr(scheduler.db, "get_campaign", lambda *_: campaign)
    monkeypatch.setattr(scheduler.db, "fetch_contact", lambda *_: contact)
    sender_lookup = MagicMock(return_value=None)
    monkeypatch.setattr(scheduler.db, "get_campaign_sender", sender_lookup)
    gmail = MagicMock()
    monkeypatch.setattr(scheduler, "send_email", gmail)
    log = MagicMock(return_value=1)
    monkeypatch.setattr(scheduler.db, "create_send_attempt", log)
    monkeypatch.setattr(scheduler.db, "update_send_log", MagicMock())
    return sender_lookup, gmail, log


@pytest.mark.parametrize("sender_email", ["", "sender@example.test"])
def test_test_send_rejects_missing_values_before_sender_lookup_or_gmail(mocked_delivery, sender_email, conn):
    sender_lookup, gmail, log = mocked_delivery
    success, reason = scheduler.send_test_email(
        conn, 7, "test@example.test", AppConfig(), sender_email=sender_email,
        campaign_id=42, service=object(), user_id=USER_ID,
    )
    assert success is False
    assert "first_name" in reason
    sender_lookup.assert_not_called()
    gmail.assert_not_called()
    log.assert_not_called()


def test_test_send_api_rejects_missing_values_even_when_called_directly(monkeypatch, mocked_delivery, conn):
    monkeypatch.setattr(campaigns, "require_delivery_enabled", lambda: None)
    monkeypatch.setattr(campaigns, "load_config", lambda *_: AppConfig())
    with pytest.raises(HTTPException) as error:
        campaigns.post_test_send(
            42, SendTestRequest(recipient_email="test@example.test", preview_contact_id=7),
            conn=conn, user_id=USER_ID,
        )
    assert error.value.status_code == 400
    assert "first_name" in error.value.detail
    mocked_delivery[1].assert_not_called()


def test_complete_test_email_still_sends(monkeypatch, campaign, contact, mocked_delivery, conn):
    contact["custom_fields"]["first_name"] = "Alex"
    monkeypatch.setattr(scheduler, "pre_send_attachment_only", lambda *_: (True, ""))
    success, _ = scheduler.send_test_email(
        conn, 7, "test@example.test", AppConfig(), sender_email="sender@example.test",
        campaign_id=42, service=object(), user_id=USER_ID,
    )
    assert success is True
    assert mocked_delivery[1].call_args.kwargs["body"] == "Hi Alex, welcome to Acme."
    assert mocked_delivery[1].call_args.kwargs["recipient"] == "test@example.test"


def test_strict_preview_does_not_mark_invalid_data_as_previewed(monkeypatch, mocked_delivery, conn):
    mark = MagicMock()
    monkeypatch.setattr(preview.db, "mark_preview_generated", mark)
    with pytest.raises(MissingTemplateVariablesError):
        preview.generate_preview(conn, 7, USER_ID, campaign_id=42, strict=True)
    mark.assert_not_called()
