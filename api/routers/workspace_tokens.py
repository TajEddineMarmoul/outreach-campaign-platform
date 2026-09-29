from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api.auth import require_interactive_user
from src.platform.db import get_session
from src.platform.models import User, WorkspaceAccessToken
from src.platform.time import utcnow


router = APIRouter(prefix="/api/workspace-tokens", tags=["workspace-tokens"])


class TokenCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    expires_in_days: int = Field(default=365, ge=1, le=365)


def _metadata(token: WorkspaceAccessToken) -> dict:
    return {
        "id": token.id,
        "name": token.name,
        "prefix": token.token_prefix,
        "created_at": token.created_at.isoformat(),
        "expires_at": token.expires_at.isoformat(),
        "revoked_at": token.revoked_at.isoformat() if token.revoked_at else None,
    }


@router.get("")
def list_tokens(
    session: Session = Depends(get_session),
    user_id: str = Depends(require_interactive_user),
):
    return [
        _metadata(token)
        for token in session.scalars(
            select(WorkspaceAccessToken)
            .where(WorkspaceAccessToken.user_id == user_id)
            .order_by(WorkspaceAccessToken.created_at.desc(), WorkspaceAccessToken.id.desc())
        )
    ]


@router.post("")
def create_token(
    req: TokenCreate,
    session: Session = Depends(get_session),
    user_id: str = Depends(require_interactive_user),
):
    name = req.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Give the token a name")
    active_count = session.scalar(
        select(func.count()).select_from(WorkspaceAccessToken).where(
            WorkspaceAccessToken.user_id == user_id,
            WorkspaceAccessToken.revoked_at.is_(None),
            WorkspaceAccessToken.expires_at > utcnow(),
        )
    ) or 0
    if active_count >= 10:
        raise HTTPException(status_code=409, detail="Revoke an existing token before creating another")
    if session.get(User, user_id) is None:
        session.add(User(id=user_id))
    raw_token = "outreach_mcp_" + secrets.token_urlsafe(32)
    token = WorkspaceAccessToken(
        user_id=user_id,
        name=name,
        token_hash=hashlib.sha256(raw_token.encode("utf-8")).hexdigest(),
        token_prefix=raw_token[:22],
        expires_at=utcnow() + timedelta(days=req.expires_in_days),
    )
    session.add(token)
    session.commit()
    return {**_metadata(token), "token": raw_token}


@router.delete("/{token_id}")
def revoke_token(
    token_id: int,
    session: Session = Depends(get_session),
    user_id: str = Depends(require_interactive_user),
):
    token = session.scalar(
        select(WorkspaceAccessToken).where(
            WorkspaceAccessToken.id == token_id,
            WorkspaceAccessToken.user_id == user_id,
        )
    )
    if not token:
        raise HTTPException(status_code=404, detail="Token not found")
    if token.revoked_at is None:
        token.revoked_at = utcnow()
        session.commit()
    return {"status": "revoked", "id": token_id}
