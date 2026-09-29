"""Add revocable user-scoped tokens for local MCP clients.

Revision ID: 0015_workspace_tokens
Revises: 0014_email_engagement
"""

from alembic import op
import sqlalchemy as sa


revision = "0015_workspace_tokens"
down_revision = "0014_email_engagement"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workspace_access_tokens",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(length=255), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False, unique=True),
        sa.Column("token_prefix", sa.String(length=32), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_workspace_access_tokens_user", "workspace_access_tokens", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_workspace_access_tokens_user", table_name="workspace_access_tokens")
    op.drop_table("workspace_access_tokens")
