from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import secrets
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from . import db

os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"


GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
USERINFO_EMAIL_SCOPE = "https://www.googleapis.com/auth/userinfo.email"

SCOPES = [
    GMAIL_SEND_SCOPE,
    GMAIL_READONLY_SCOPE,
    USERINFO_EMAIL_SCOPE,
]


@dataclass(frozen=True)
class GmailSendResult:
    message_id: str
    thread_id: str


@dataclass(frozen=True)
class EmailAttachment:
    filename: str
    content_type: str
    content: bytes


@dataclass(frozen=True)
class GmailConnectionStatus:
    connected: bool
    status: str
    email: str = ""
    detail: str = ""
    token_path: str = ""


def credentials_paths() -> tuple[Path, Path]:
    load_dotenv()
    credentials_path = db.resolve_project_path(os.getenv("GMAIL_CREDENTIALS_PATH", "credentials.json"))
    token_path = db.resolve_project_path(os.getenv("GMAIL_TOKEN_PATH", "token.json"))
    return credentials_path, token_path


def credentials_file_path() -> Path:
    configured_path = credentials_paths()[0]
    credentials_json = os.getenv("GMAIL_CREDENTIALS_JSON")
    if not credentials_json:
        return configured_path

    # Vercel functions have a read-only application filesystem and a writable
    # temporary directory. Recreate the OAuth client file for each cold start.
    path = configured_path
    if os.getenv("VERCEL") and not path.as_posix().startswith("/tmp/"):
        path = Path("/tmp/outreach-google-oauth-credentials.json")
    try:
        normalized = json.dumps(json.loads(credentials_json), separators=(",", ":"))
    except json.JSONDecodeError as exc:
        raise ValueError("GMAIL_CREDENTIALS_JSON is not valid JSON") from exc
    if not path.exists() or path.read_text(encoding="utf-8") != normalized:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(normalized, encoding="utf-8")
    return path


def default_token_path() -> Path:
    return credentials_paths()[1]


def resolve_token_path(token_path: str | Path | None = None) -> Path:
    if token_path:
        return db.resolve_project_path(token_path)
    return default_token_path()


def sanitize_email_for_path(email: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9._-]+", "_", email.strip().lower())
    return safe or "sender"


def sender_token_path_for_email(email: str, user_id: str) -> Path:
    safe_user = re.sub(r"[^a-zA-Z0-9._-]+", "_", user_id)
    return db.resolve_project_path(Path("tokens") / safe_user / f"gmail_{sanitize_email_for_path(email)}.json")


def clear_gmail_token() -> None:
    _, token_path = credentials_paths()
    if token_path.exists():
        token_path.unlink()


def verify_required_scopes(creds: Credentials, required_scopes: list[str]) -> None:
    granted_scopes = set(creds.scopes or [])
    missing = [s for s in required_scopes if s not in granted_scopes]
    if missing:
        raise ValueError(
            "Required permissions were not granted: "
            f"'{', '.join(missing)}'. "
            "Please log in again and make sure to check the permission box for Gmail Send."
        )


def get_google_credentials(
    force_reauth: bool = False,
    token_path: str | Path | None = None,
    prompt: str | None = None,
):
    credentials_path = credentials_file_path()
    final_token_path = resolve_token_path(token_path)
    # The legacy local app can continue sending with an existing token. The
    # platform checks the separately stored granted scopes before it reads
    # delivery notices, and asks the user to reconnect when read access is
    # missing.
    required = [GMAIL_SEND_SCOPE, USERINFO_EMAIL_SCOPE]
    
    if force_reauth and final_token_path.exists():
        final_token_path.unlink()
    creds = None
    if final_token_path.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(final_token_path), SCOPES)
            verify_required_scopes(creds, required)
        except Exception:
            if final_token_path.exists():
                final_token_path.unlink()
            creds = None
            
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                verify_required_scopes(creds, required)
            except Exception:
                if final_token_path.exists():
                    final_token_path.unlink()
                creds = None
                
        if not creds:
            if not credentials_path.exists():
                raise FileNotFoundError(
                    f"Gmail OAuth credentials not found at {credentials_path}. "
                    "Create OAuth desktop credentials and save them there."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
            kwargs = {"prompt": prompt} if prompt else {}
            creds = flow.run_local_server(port=0, **kwargs)
            verify_required_scopes(creds, required)
            
        final_token_path.parent.mkdir(parents=True, exist_ok=True)
        final_token_path.write_text(creds.to_json(), encoding="utf-8")
    return creds


def get_gmail_service(
    force_reauth: bool = False,
    token_path: str | Path | None = None,
    prompt: str | None = None,
):
    creds = get_google_credentials(force_reauth=force_reauth, token_path=token_path, prompt=prompt)
    return build("gmail", "v1", credentials=creds)


def get_connected_email(creds: Credentials) -> str:
    profile = build("oauth2", "v2", credentials=creds).userinfo().get().execute()
    return str(profile.get("email", ""))


def gmail_connection_status(token_path: str | Path | None = None) -> GmailConnectionStatus:
    try:
        final_token_path = resolve_token_path(token_path)
        creds = get_google_credentials(token_path=final_token_path)
        email = get_connected_email(creds)
        return GmailConnectionStatus(
            True, "Connected", email=email, token_path=str(final_token_path)
        )
    except Exception as exc:
        return GmailConnectionStatus(
            False, str(exc), token_path=str(token_path or "tokens/missing.json")
        )


def connect_and_get_profile(
    force_reauth: bool = False,
    token_path: str | Path | None = None,
    prompt: str | None = None,
) -> GmailConnectionStatus:
    final_token_path = resolve_token_path(token_path)
    creds = get_google_credentials(force_reauth=force_reauth, token_path=final_token_path, prompt=prompt)
    return GmailConnectionStatus(
        True,
        "Connected",
        email=get_connected_email(creds),
        token_path=str(final_token_path),
    )


def connect_sender_account(user_id: str, force_reauth: bool = True) -> GmailConnectionStatus:
    safe_user = re.sub(r"[^a-zA-Z0-9._-]+", "_", user_id)
    pending_path = db.resolve_project_path(Path("tokens") / safe_user / "gmail_pending.json")
    if pending_path.exists():
        try:
            pending_path.unlink()
        except Exception:
            pass
    pending_path.parent.mkdir(parents=True, exist_ok=True)
    status = connect_and_get_profile(
        force_reauth=force_reauth,
        token_path=pending_path,
        prompt="select_account consent",
    )
    final_path = sender_token_path_for_email(status.email, user_id)
    final_path.parent.mkdir(parents=True, exist_ok=True)
    if pending_path != final_path:
        shutil.move(str(pending_path), str(final_path))
    return GmailConnectionStatus(
        True,
        "Connected",
        email=status.email,
        token_path=str(final_path),
    )


def build_message(
    sender: str,
    recipient: str,
    subject: str,
    body: str,
    attachment_path: str | Path | None = None,
    attachment: EmailAttachment | None = None,
    attachments: Sequence[EmailAttachment] | None = None,
) -> dict[str, str]:
    message = EmailMessage()
    message["To"] = recipient
    if sender:
        message["From"] = sender
    message["Subject"] = subject
    import re
    if re.search(r'<[a-z][\s\S]*>', body, re.IGNORECASE):
        message.set_content(body, subtype="html")
    else:
        message.set_content(body)
    stored_attachments = list(attachments or ())
    if attachment:
        stored_attachments.append(attachment)
    for stored_attachment in stored_attachments:
        mime_type = stored_attachment.content_type or mimetypes.guess_type(stored_attachment.filename)[0]
        maintype, subtype = (mime_type or "application/octet-stream").split("/", 1)
        message.add_attachment(
            stored_attachment.content,
            maintype=maintype,
            subtype=subtype,
            filename=stored_attachment.filename,
        )
    # attachment_path is retained only for call compatibility with the legacy
    # scheduler. It is intentionally ignored: attachments must be supplied as
    # stored bytes after ownership checks, never read from the server disk.

    encoded = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
    return {"raw": encoded}


def send_email(
    sender: str,
    recipient: str,
    subject: str,
    body: str,
    attachment_path: str | Path | None = None,
    token_path: str | Path | None = None,
    service=None,
    attachment: EmailAttachment | None = None,
    attachments: Sequence[EmailAttachment] | None = None,
) -> GmailSendResult:
    if service is None:
        service = get_gmail_service(token_path=token_path)
    message = build_message(
        sender,
        recipient,
        subject,
        body,
        attachment_path=attachment_path,
        attachment=attachment,
        attachments=attachments,
    )
    sent = service.users().messages().send(userId="me", body=message).execute()
    return GmailSendResult(
        message_id=sent["id"],
        thread_id=sent.get("threadId", ""),
    )


def fake_send_email(
    sender: str,
    recipient: str,
    subject: str,
    body: str,
    attachment_path: str | Path | None = None,
    token_path: str | Path | None = None,
    service=None,
    attachment: EmailAttachment | None = None,
    attachments: Sequence[EmailAttachment] | None = None,
) -> GmailSendResult:
    return GmailSendResult(
        message_id=f"fake_{secrets.token_hex(8)}",
        thread_id=f"fake_thread_{secrets.token_hex(8)}",
    )
