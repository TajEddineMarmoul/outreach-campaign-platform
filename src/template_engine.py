from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from .models import RenderedEmail


TEMPLATE_VARIABLE_PATTERN = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")


class MissingTemplateVariablesError(ValueError):
    def __init__(self, variables: list[str]):
        self.variables = tuple(variables)
        super().__init__(f"Missing CSV values for template variables: {', '.join(variables)}")


def extract_template_variables(template: str) -> list[str]:
    return [
        name
        for match in TEMPLATE_VARIABLE_PATTERN.finditer(template or "")
        if (name := match.group(1).strip())
    ]


def missing_template_variables(template: str, context: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    for name in extract_template_variables(template):
        value = context.get(name)
        if (name not in context or value is None or str(value).strip() == "") and name not in missing:
            missing.append(name)
    return missing


def row_to_dict(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    return dict(row)


def contact_context(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    contact = row_to_dict(row)
    context: dict[str, Any] = {}
    custom_str = contact.get("custom_fields") or "{}"
    try:
        custom_data = custom_str if isinstance(custom_str, dict) else json.loads(custom_str)
        context.update(custom_data)
    except Exception:
        pass

    context.setdefault("email", contact.get("email") or contact.get("email_normalized") or "")

    return context


def render_template(template: str, context: dict[str, Any], *, strict: bool = False) -> str:
    missing = missing_template_variables(template, context)
    if strict and missing:
        raise MissingTemplateVariablesError(missing)

    def replace_variable(match: re.Match[str]) -> str:
        name = match.group(1).strip()
        value = context.get(name)
        if name not in context or value is None or str(value).strip() == "":
            return f"[missing {name}]"
        return str(value)

    return TEMPLATE_VARIABLE_PATTERN.sub(replace_variable, template or "").strip()


def render_email(
    contact: sqlite3.Row | dict[str, Any],
    campaign: sqlite3.Row | dict[str, Any],
    *,
    strict: bool = False,
) -> RenderedEmail:
    contact_dict = row_to_dict(contact)
    campaign_dict = row_to_dict(campaign)
    context = contact_context(contact_dict)
    subject = str(campaign_dict.get("subject_template") or "")
    body = str(
        campaign_dict.get("body_template") or campaign_dict.get("fallback_body_template") or ""
    )
    missing = list(
        dict.fromkeys(
            missing_template_variables(subject, context) + missing_template_variables(body, context)
        )
    )
    if strict and missing:
        raise MissingTemplateVariablesError(missing)
    return RenderedEmail(
        recipient_email=str(contact_dict.get("email") or contact_dict.get("email_normalized") or ""),
        subject=render_template(subject, context),
        body=render_template(body, context),
        used_fallback=False,
        missing_variables=missing,
    )
