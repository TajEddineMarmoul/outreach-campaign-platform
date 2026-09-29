from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0003_platform_delivery_compat"
down_revision = "0002_add_sender_is_default"
branch_labels = None
depends_on = None


def _columns(table_name: str) -> dict[str, dict]:
    return {
        column["name"]: column
        for column in sa.inspect(op.get_bind()).get_columns(table_name)
    }


def _add_missing(table_name: str, columns: list[sa.Column]) -> None:
    existing = _columns(table_name)
    for column in columns:
        if column.name not in existing:
            op.add_column(table_name, column)


def _convert_text_timestamp(table_name: str, column_name: str, *, nullable: bool) -> None:
    column = _columns(table_name).get(column_name)
    if not column or isinstance(column["type"], sa.DateTime):
        return
    fallback = "NULL" if nullable else "CURRENT_TIMESTAMP"
    op.alter_column(
        table_name,
        column_name,
        type_=sa.DateTime(timezone=True),
        nullable=nullable,
        postgresql_using=(
            f"COALESCE(NULLIF({column_name}::text, '')::timestamptz, {fallback})"
        ),
    )


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        raise RuntimeError("The application schema migration requires PostgreSQL.")

    _add_missing(
        "campaigns",
        [
            sa.Column("selected_sender_group_id", sa.Integer(), nullable=True),
            sa.Column(
                "send_settings",
                postgresql.JSONB(),
                nullable=False,
                server_default=sa.text("'{}'::jsonb"),
            ),
            sa.Column(
                "attachment_metadata",
                postgresql.JSONB(),
                nullable=False,
                server_default=sa.text("'{}'::jsonb"),
            ),
            sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True),
        ],
    )
    op.execute(
        """
        UPDATE campaigns
        SET attachment_metadata = jsonb_build_object('path', attachment_path)
        WHERE COALESCE(attachment_path, '') <> ''
          AND attachment_metadata = '{}'::jsonb
        """
    )
    op.execute("UPDATE campaigns SET attachment_path = '' WHERE attachment_path IS NULL")
    op.alter_column("campaigns", "attachment_path", server_default="", nullable=False)

    contact_columns = _columns("contacts")
    email_normalized_was_missing = "email_normalized" not in contact_columns
    if email_normalized_was_missing:
        op.add_column(
            "contacts",
            sa.Column("email_normalized", sa.String(length=320), nullable=True),
        )
    # Older installs stored the address in `email`; new installs created from
    # the current metadata already have `email_normalized` and no `email`.
    # Only backfill when the legacy source column actually exists.
    if email_normalized_was_missing and "email" in contact_columns:
        op.execute(
            "UPDATE contacts SET email_normalized = lower(trim(email)) "
            "WHERE email_normalized IS NULL OR email_normalized = ''"
        )
    op.alter_column("contacts", "email_normalized", nullable=False)
    custom_fields = _columns("contacts").get("custom_fields")
    if custom_fields and not isinstance(custom_fields["type"], postgresql.JSONB):
        op.alter_column("contacts", "custom_fields", server_default=None)
        op.alter_column(
            "contacts",
            "custom_fields",
            type_=postgresql.JSONB(),
            nullable=False,
            postgresql_using=(
                "CASE WHEN custom_fields IS NULL OR btrim(custom_fields::text) = '' "
                "THEN '{}'::jsonb ELSE custom_fields::jsonb END"
            ),
        )
        op.alter_column(
            "contacts",
            "custom_fields",
            server_default=sa.text("'{}'::jsonb"),
        )

    _add_missing(
        "senders",
        [
            sa.Column("group_id", sa.Integer(), nullable=True),
            sa.Column("encrypted_oauth_credentials", sa.Text(), nullable=True),
            sa.Column(
                "scopes",
                postgresql.JSONB(),
                nullable=False,
                server_default=sa.text("'[]'::jsonb"),
            ),
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("recent_error_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
        ],
    )
    if "token_path" in _columns("senders"):
        op.alter_column("senders", "token_path", nullable=True, server_default="")

    _add_missing(
        "campaign_recipients",
        [
            sa.Column(
                "status",
                sa.String(length=40),
                nullable=False,
                server_default="approved",
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
        ],
    )
    op.execute(
        "UPDATE campaign_recipients SET status = 'approved' "
        "WHERE status IS NULL OR btrim(status) = ''"
    )
    op.alter_column(
        "campaign_recipients",
        "status",
        nullable=False,
        server_default="approved",
    )

    _add_missing(
        "send_log",
        [sa.Column("recipient_id", sa.Integer(), nullable=True)],
    )
    op.execute("UPDATE send_log SET recipient_id = contact_id WHERE recipient_id IS NULL")
    op.execute("UPDATE send_log SET sender_email = '' WHERE sender_email IS NULL")
    op.alter_column("send_log", "sender_email", nullable=False, server_default="")

    for table_name, column_name, nullable in (
        ("campaigns", "created_at", False),
        ("campaigns", "updated_at", False),
        ("contacts", "created_at", False),
        ("contacts", "updated_at", False),
        ("senders", "connected_at", True),
        ("campaign_recipients", "created_at", False),
        ("send_log", "created_at", False),
        ("send_log", "updated_at", False),
        ("send_log", "sent_at", True),
    ):
        _convert_text_timestamp(table_name, column_name, nullable=nullable)

    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM contacts
                GROUP BY user_id, email_normalized
                HAVING COUNT(*) > 1
            ) THEN
                CREATE UNIQUE INDEX IF NOT EXISTS uq_contacts_user_email
                    ON contacts (user_id, email_normalized);
            ELSE
                CREATE INDEX IF NOT EXISTS ix_contacts_user_email_normalized
                    ON contacts (user_id, email_normalized);
            END IF;
        END
        $$
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_campaign_recipients_status "
        "ON campaign_recipients (campaign_id, status)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_campaigns_sender_group "
        "ON campaigns (selected_sender_group_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_senders_user_group_status "
        "ON senders (user_id, group_id, status)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_send_log_user_campaign "
        "ON send_log (user_id, campaign_id, created_at)"
    )


def downgrade() -> None:
    # This migration adopts existing production data. Destructive rollback is
    # intentionally unsupported.
    pass
