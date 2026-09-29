"""Provision one personal MCP token from a trusted workstation with DB access.

The raw token is saved to a private local file and never printed. The token can
later be revoked from the app's Settings page.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import secrets
import sys
from datetime import timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from outreach_mcp.server import default_token_file  # noqa: E402
from src.platform.db import SessionLocal  # noqa: E402
from src.platform.models import User, WorkspaceAccessToken  # noqa: E402
from src.platform.time import utcnow  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", required=True, help="Current production Clerk user ID")
    parser.add_argument("--name", default="Codex campaign manager")
    parser.add_argument("--token-file", type=Path, default=default_token_file())
    args = parser.parse_args()

    token_path = args.token_file.resolve()
    if token_path.exists():
        raise SystemExit(f"Token file already exists: {token_path}. Revoke the old token before replacing it.")
    raw_token = "outreach_mcp_" + secrets.token_urlsafe(32)
    token_path.parent.mkdir(parents=True, exist_ok=True)

    with SessionLocal() as session:
        if session.get(User, args.user_id) is None:
            raise SystemExit("User ID does not exist in the configured database")
        token = WorkspaceAccessToken(
            user_id=args.user_id,
            name=args.name.strip(),
            token_hash=hashlib.sha256(raw_token.encode("utf-8")).hexdigest(),
            token_prefix=raw_token[:22],
            expires_at=utcnow() + timedelta(days=365),
        )
        session.add(token)
        created_file = False
        try:
            descriptor = os.open(str(token_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            created_file = True
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(raw_token + "\n")
            session.commit()
        except BaseException:
            session.rollback()
            if created_file:
                token_path.unlink()
            raise

    print(f"Provisioned token {token.id} ({token.token_prefix}…) for {args.user_id}.")
    print(f"Saved token to {token_path}.")


if __name__ == "__main__":
    main()
