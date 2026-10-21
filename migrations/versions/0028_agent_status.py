"""Keep one optional agent task-list report on its existing named mailbox."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0028_agent_status"
down_revision = "0027_pull_requests"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agent_mailboxes", sa.Column("agent_status", JSONB, nullable=True))


def downgrade() -> None:
    raise RuntimeError("Preserve agent reports; roll back code while retaining the optional column")
