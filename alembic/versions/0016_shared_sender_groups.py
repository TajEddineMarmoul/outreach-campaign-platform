"""Link saved sender accounts to multiple groups without copying credentials."""

from alembic import op
import sqlalchemy as sa

revision = "0016_shared_sender_groups"
down_revision = "0015_workspace_tokens"
branch_labels = None
depends_on = None


def upgrade():
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("senders") as batch:
            batch.alter_column("group_id", existing_type=sa.Integer(), nullable=True)
    else:
        op.alter_column("senders", "group_id", existing_type=sa.Integer(), nullable=True)
    op.create_table(
        "sender_group_memberships",
        sa.Column("group_id", sa.Integer(), sa.ForeignKey("sender_groups.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("sender_id", sa.Integer(), sa.ForeignKey("senders.id", ondelete="CASCADE"), primary_key=True),
    )
    op.create_index("ix_sender_group_memberships_sender_id", "sender_group_memberships", ["sender_id"])
    op.execute(sa.text(
        "INSERT INTO sender_group_memberships (group_id, sender_id) "
        "SELECT group_id, id FROM senders WHERE group_id IS NOT NULL"
    ))


def downgrade():
    op.drop_table("sender_group_memberships")
    # Leave the legacy column nullable: saved accounts with no group must not
    # be deleted or assigned elsewhere merely to roll back the group UI.
