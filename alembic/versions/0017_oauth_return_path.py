"""Remember the initiating page through the Gmail authorization round trip."""

from alembic import op
import sqlalchemy as sa

revision = "0017_oauth_return_path"
down_revision = "0016_shared_sender_groups"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("oauth_states", sa.Column("return_path", sa.String(2048), nullable=True))


def downgrade():
    op.drop_column("oauth_states", "return_path")
