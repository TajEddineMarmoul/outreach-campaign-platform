from __future__ import annotations

import os
os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"

import sys
from pathlib import Path
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

load_dotenv(PROJECT_ROOT / ".env")

from src import db
from src.db.core import utcnow_iso, resolve_project_path
from api.auth import get_current_user_id, require_admin_user


def get_db_path() -> str:
    return str(resolve_project_path("data/outreach.db"))


def config_path() -> Path:
    return db.resolve_project_path(os.getenv("OUTREACH_CONFIG_PATH", "config.yaml"), PROJECT_ROOT)


def get_db():
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()
