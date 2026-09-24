from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator


class ContactStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    SENT = "sent"
    REPLIED = "replied"
    BOUNCED = "bounced"
    FAILED = "failed"
    DO_NOT_CONTACT = "do_not_contact"


DEFAULT_SUBJECT_TEMPLATE = ""

DEFAULT_BODY_TEMPLATE = ""

DEFAULT_FALLBACK_BODY_TEMPLATE = ""

CSV_REQUIRED_COLUMNS = ["Email", "First Name", "Company Name"]

STATUS_VALUES = tuple(status.value for status in ContactStatus)


class SendingConfig(BaseModel):
    days: list[str] = Field(
        default_factory=lambda: ["monday", "tuesday", "wednesday", "thursday", "friday"]
    )
    start_time: str = "09:00"
    end_time: str = "17:00"
    delay_minutes: int = Field(default=10, ge=1)
    daily_cap: int = Field(default=10, ge=1)
    max_daily_cap_allowed_without_manual_override: int = Field(default=50, ge=1)
    max_consecutive_errors: int = Field(default=3, ge=1)
    bounce_rate_pause_threshold: float = Field(default=5, ge=0)

    @field_validator("days")
    @classmethod
    def normalize_days(cls, value: list[str]) -> list[str]:
        return [day.strip().lower() for day in value if day.strip()]


class CampaignConfig(BaseModel):
    attachment_path: str = ""
    tracking_enabled: bool = False
    followups_enabled: bool = False


class AppConfig(BaseModel):
    timezone: str = "Europe/Paris"
    sending: SendingConfig = Field(default_factory=SendingConfig)
    campaign: CampaignConfig = Field(default_factory=CampaignConfig)


class ImportResult(BaseModel):
    imported: int = 0
    duplicates: int = 0
    skipped_missing_email: int = 0
    skipped_missing_required: int = 0
    do_not_contact: int = 0
    errors: list[str] = Field(default_factory=list)

    @property
    def skipped_rows(self) -> int:
        return self.skipped_missing_email + self.skipped_missing_required


class RenderedEmail(BaseModel):
    recipient_email: str
    subject: str
    body: str
    used_fallback: bool = False


def default_config_dict() -> dict[str, Any]:
    return AppConfig().model_dump()


def load_config(path: str | Path = "config.yaml") -> AppConfig:
    config_path = Path(path)
    if not config_path.exists():
        return AppConfig()
    with config_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    return AppConfig.model_validate(raw)


def save_config(config: AppConfig, path: str | Path = "config.yaml") -> None:
    config_path = Path(path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with config_path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(config.model_dump(), handle, sort_keys=False)
