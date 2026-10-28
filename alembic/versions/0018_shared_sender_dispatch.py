"""Persist account-level pacing clocks, weighted queues and window boundaries."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0018_shared_sender_dispatch"
down_revision = "0017_oauth_return_path"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("campaigns", sa.Column("shared_dispatch", sa.Boolean(), server_default=sa.false(), nullable=False))
    op.add_column("send_jobs", sa.Column("dispatch_token", sa.String(64), nullable=True))
    op.create_index("ix_campaigns_dispatch_bootstrap", "campaigns", ["shared_dispatch", "status", "id"])
    op.create_index("ix_campaigns_due", "campaigns", ["shared_dispatch", "status", "scheduled_at", "id"])
    op.create_index("ix_send_jobs_sender_pending", "send_jobs", ["sender_id", "campaign_id", "status", "scheduled_for", "id"])
    op.create_index("ix_send_jobs_sender_reserved", "send_jobs", ["sender_id", "status", "scheduled_for", "id"])
    op.create_index("ix_send_jobs_campaign_reserved", "send_jobs", ["campaign_id", "status", "scheduled_for"])
    op.create_index("ix_send_jobs_recipient_state", "send_jobs", ["campaign_id", "recipient_id", "status"])
    op.create_index("ix_send_log_sender_day", "send_log", ["sender_id", "sent_at", "status"])
    op.create_index("ix_send_log_campaign_day", "send_log", ["campaign_id", "sent_at", "status"])
    op.create_index("ix_campaign_recipients_ready", "campaign_recipients", ["campaign_id", "status", "created_at", "contact_id"])
    op.create_table(
        "sender_dispatch",
        sa.Column("sender_id", sa.Integer(), sa.ForeignKey("senders.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("user_id", sa.String(255), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("rate", sa.Float(), nullable=False),
        sa.Column("active_lanes", sa.Integer(), nullable=False),
        sa.Column("virtual_time", sa.Float(), nullable=False),
        sa.Column("credit", sa.Float(), nullable=False),
        sa.Column("clock_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idle_since", sa.DateTime(timezone=True)),
        sa.Column("last_started_at", sa.DateTime(timezone=True)),
        sa.Column("next_at", sa.DateTime(timezone=True)),
        sa.Column("blocked_until", sa.DateTime(timezone=True)),
        sa.Column("lease_token", sa.String(64)),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("leased_campaign_id", sa.Integer()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_sender_dispatch_user_id", "sender_dispatch", ["user_id"])
    op.create_index("ix_sender_dispatch_due", "sender_dispatch", ["next_at", "sender_id"])
    op.create_index("ix_sender_dispatch_leases", "sender_dispatch", ["lease_until", "sender_id"])
    op.create_table(
        "sender_campaign_lanes",
        sa.Column("sender_id", sa.Integer(), sa.ForeignKey("senders.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("campaign_id", sa.Integer(), sa.ForeignKey("campaigns.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("selectable", sa.Boolean(), nullable=False),
        sa.Column("rate", sa.Float(), nullable=False),
        sa.Column("finish_tag", sa.Float(), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True)),
        sa.Column("window_end", sa.DateTime(timezone=True)),
        sa.Column("transition_at", sa.DateTime(timezone=True)),
        sa.Column("calendar", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False),
        sa.Column("calendar_key", sa.String(64), server_default="", nullable=False),
    )
    op.create_index("ix_sender_lanes_fair", "sender_campaign_lanes", ["sender_id", "active", "selectable", "finish_tag", "window_end", "campaign_id"])
    op.create_index("ix_sender_lanes_transition", "sender_campaign_lanes", ["transition_at", "sender_id", "campaign_id"])
    op.create_index("ix_sender_lanes_sender_transition", "sender_campaign_lanes", ["sender_id", "transition_at"])
    op.create_index("ix_sender_lanes_calendar_transition", "sender_campaign_lanes", ["sender_id", "calendar_key", "transition_at"])
    op.create_index("ix_sender_lanes_campaign", "sender_campaign_lanes", ["campaign_id", "sender_id"])


def downgrade():
    op.drop_table("sender_campaign_lanes")
    op.drop_table("sender_dispatch")
    op.drop_index("ix_send_jobs_sender_pending", table_name="send_jobs")
    op.drop_index("ix_send_jobs_sender_reserved", table_name="send_jobs")
    op.drop_index("ix_send_jobs_campaign_reserved", table_name="send_jobs")
    op.drop_index("ix_send_jobs_recipient_state", table_name="send_jobs")
    op.drop_index("ix_send_log_sender_day", table_name="send_log")
    op.drop_index("ix_send_log_campaign_day", table_name="send_log")
    op.drop_index("ix_campaign_recipients_ready", table_name="campaign_recipients")
    op.drop_index("ix_campaigns_due", table_name="campaigns")
    op.drop_index("ix_campaigns_dispatch_bootstrap", table_name="campaigns")
    op.drop_column("send_jobs", "dispatch_token")
    op.drop_column("campaigns", "shared_dispatch")
