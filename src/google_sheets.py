from __future__ import annotations

import os
import re
from html import unescape
from io import StringIO
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlparse

import pandas as pd
import requests
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from . import db

os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"
from .importer import detect_columns, import_dataframe
from .models import ImportResult

SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
PUBLIC_SHEET_TIMEOUT = (5, 120)
MAX_PUBLIC_SHEET_BYTES = 100 * 1024 * 1024


@dataclass(frozen=True)
class SheetUrl:
    sheet_id: str
    gid: str | None = None
    published: bool = False


@dataclass(frozen=True)
class GoogleSheetsConnectionStatus:
    connected: bool
    status: str
    detail: str = ""
    token_path: str = ""


def parse_google_sheet_url(url: str) -> str:
    parsed = parse_google_sheet_url_details(url)
    return parsed.sheet_id


def parse_google_sheet_url_details(url: str) -> SheetUrl:
    text = url.strip()
    if re.fullmatch(r"[a-zA-Z0-9-_]{20,}", text):
        return SheetUrl(text)

    parsed = urlparse(text)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() != "docs.google.com":
        raise ValueError("Use a Google Sheets link from docs.google.com or a Google Sheet ID")

    match = re.fullmatch(
        r"/spreadsheets/(?:u/\d+/)?d/(e/)?([A-Za-z0-9_-]+)(?:/.*)?",
        parsed.path,
    )
    if not match:
        raise ValueError("Could not find a Google Sheet ID in the URL")

    query = parse_qs(parsed.query)
    gid = query.get("gid", [None])[0]
    if gid is None and "gid=" in parsed.fragment:
        gid = parse_qs(parsed.fragment).get("gid", [None])[0]
    if gid is not None and not re.fullmatch(r"\d{1,20}", gid):
        raise ValueError("The Google Sheet tab identifier is invalid")
    return SheetUrl(match.group(2), gid, published=bool(match.group(1)))


def get_public_sheet_csv(
    sheet_id: str,
    gid: str | None = None,
    header_row: int = 1,
    sheet_name: str | None = None,
) -> pd.DataFrame:
    if sheet_name:
        url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?tqx=out:csv&sheet={quote(sheet_name)}"
    else:
        params = f"&gid={gid}" if gid else ""
        url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv{params}"
    return _read_public_csv(url, header_row)


def get_published_csv(sheet_id: str, gid: str | None = None, header_row: int = 1) -> pd.DataFrame:
    params = {"output": "csv"}
    if gid:
        params["gid"] = gid
    url = f"https://docs.google.com/spreadsheets/d/e/{sheet_id}/pub?{urlencode(params)}"
    return _read_public_csv(url, header_row)


def _read_public_csv(url: str, header_row: int) -> pd.DataFrame:
    # Every caller constructs this URL from a validated sheet ID. Never follow
    # redirects: imports must not become requests to a caller-controlled host.
    response = requests.get(url, timeout=PUBLIC_SHEET_TIMEOUT, allow_redirects=False)
    response.raise_for_status()
    if len(response.content) > MAX_PUBLIC_SHEET_BYTES:
        raise ValueError("The Google Sheet export exceeds the 100 MB per-sheet limit")
    content_type = response.headers.get("content-type", "").lower()
    if "text/html" in content_type:
        raise ValueError("Google returned an HTML page instead of CSV. Make the sheet publicly readable.")
    frame = pd.read_csv(StringIO(response.text), header=max(header_row - 1, 0))
    frame.attrs["source_bytes"] = len(response.content)
    return frame


def list_public_sheet_tabs(sheet_url: str) -> list[dict[str, str | None]]:
    sheet = parse_google_sheet_url_details(sheet_url)
    if sheet.published:
        return []
    feed_url = f"https://spreadsheets.google.com/feeds/worksheets/{sheet.sheet_id}/public/basic?alt=json"
    try:
        response = requests.get(feed_url, timeout=20)
        if response.ok:
            feed = response.json().get("feed", {})
            tabs = []
            for entry in feed.get("entry", []):
                title = entry.get("title", {}).get("$t", "").strip()
                if title:
                    tabs.append({"title": title, "gid": None})
            if tabs:
                return tabs
    except Exception:
        pass

    page_url = f"https://docs.google.com/spreadsheets/d/{sheet.sheet_id}/edit"
    response = requests.get(page_url, timeout=20)
    response.raise_for_status()
    tabs_by_title: dict[str, dict[str, str | None]] = {}
    patterns = [
        r'"sheetId":(\d+).*?"title":"([^"]+)"',
        r'\{"gid":(\d+),"name":"(.*?)"',
    ]
    for pattern in patterns:
        for gid, raw_title in re.findall(pattern, response.text):
            title = bytes(raw_title, "utf-8").decode("unicode_escape").strip()
            if title:
                tabs_by_title[title] = {"title": title, "gid": gid}
    for raw_title in re.findall(r'docs-sheet-tab-caption">([^<]+)</div>', response.text):
        title = unescape(raw_title).strip()
        if title and title not in tabs_by_title:
            tabs_by_title[title] = {"title": title, "gid": None}
    return list(tabs_by_title.values())


def sheets_credentials_paths() -> tuple[Path, Path]:
    load_dotenv()
    credentials_path = db.resolve_project_path(os.getenv("GOOGLE_SHEETS_CREDENTIALS_PATH", "credentials.json"))
    token_path = db.resolve_project_path(os.getenv("GOOGLE_SHEETS_TOKEN_PATH", "sheets_token.json"))
    return credentials_path, token_path


def sheets_connection_status() -> GoogleSheetsConnectionStatus:
    _, token_path = sheets_credentials_paths()
    if not token_path.exists():
        return GoogleSheetsConnectionStatus(
            False,
            "Token missing",
            detail=f"Missing {token_path}",
            token_path=str(token_path),
        )
    try:
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    except Exception as exc:
        return GoogleSheetsConnectionStatus(False, "Token invalid", detail=str(exc), token_path=str(token_path))
    if creds.expired and not creds.refresh_token:
        return GoogleSheetsConnectionStatus(
            False,
            "Token expired",
            detail="Reconnect Google Sheets to refresh the token.",
            token_path=str(token_path),
        )
    if not creds.valid:
        try:
            creds.refresh(Request())
            token_path.write_text(creds.to_json(), encoding="utf-8")
        except Exception as exc:
            return GoogleSheetsConnectionStatus(False, "Token expired", detail=str(exc), token_path=str(token_path))
    return GoogleSheetsConnectionStatus(True, "Connected", token_path=str(token_path))


def connect_google_sheets_oauth():
    credentials_path, token_path = sheets_credentials_paths()
    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not credentials_path.exists():
                raise FileNotFoundError(f"Google Sheets OAuth credentials not found at {credentials_path}")
            flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
            creds = flow.run_local_server(port=0, prompt="select_account consent")
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json(), encoding="utf-8")
    return build("sheets", "v4", credentials=creds)


def list_sheet_tabs(sheet_id: str, service=None) -> list[dict[str, Any]]:
    sheets = service or connect_google_sheets_oauth()
    metadata = sheets.spreadsheets().get(spreadsheetId=sheet_id).execute()
    tabs = []
    for sheet in metadata.get("sheets", []):
        props = sheet.get("properties", {})
        tabs.append({"title": props.get("title", ""), "sheet_id": props.get("sheetId")})
    return tabs


def read_sheet_rows(sheet_id: str, tab_name: str, header_row: int = 1, service=None) -> pd.DataFrame:
    sheets = service or connect_google_sheets_oauth()
    escaped_tab = tab_name.replace("'", "''")
    result = (
        sheets.spreadsheets()
        .values()
        .get(spreadsheetId=sheet_id, range=f"'{escaped_tab}'")
        .execute()
    )
    values = result.get("values", [])
    if not values:
        return pd.DataFrame()
    zero_based = max(header_row - 1, 0)
    header = values[zero_based]
    rows = values[zero_based + 1 :]
    return pd.DataFrame(rows, columns=header)


def dataframe_with_header_row(frame: pd.DataFrame, header_row: int) -> pd.DataFrame:
    if frame.empty:
        return frame
    zero_based = max(header_row - 1, 0)
    header = [str(value).strip() for value in frame.iloc[zero_based].tolist()]
    rows = frame.iloc[zero_based + 1 :].reset_index(drop=True)
    rows.columns = header
    return rows


def sync_sheet_to_contacts(
    conn,
    sheet_id: str,
    tab_name: str,
    column_mapping: dict[str, str],
    frame: pd.DataFrame | None = None,
    source_url: str = "",
) -> ImportResult:
    data = frame if frame is not None else read_sheet_rows(sheet_id, tab_name)
    if not column_mapping:
        column_mapping = detect_columns(list(data.columns))
    return import_dataframe(
        data,
        conn,
        column_mapping=column_mapping,
        source_type="google_sheet",
        source_url=source_url,
        sheet_id=sheet_id,
        sheet_name=tab_name,
    )


def write_back_status_to_sheet(*_args, **_kwargs) -> None:
    raise NotImplementedError("Writing statuses back to Google Sheets is reserved for a later version.")
