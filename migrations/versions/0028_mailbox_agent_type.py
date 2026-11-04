"""Record current mailbox runtime type without guessing legacy bindings."""
import sqlalchemy as sa
from alembic import op

revision = "0028_mailbox_agent_type"
down_revision = "0027_pull_requests"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agent_mailboxes", sa.Column("agent_type", sa.Text(), nullable=True))
    op.create_check_constraint("ck_mailbox_agent_type", "agent_mailboxes",
        "agent_type IS NULL OR agent_type IN ('codex', 'claude', 'chatgpt')")
    op.add_column("agent_mailbox_transfer_requests",
                  sa.Column("destination_agent_type", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_mailbox_transfer_requests", "destination_agent_type")
    op.drop_constraint("ck_mailbox_agent_type", "agent_mailboxes", type_="check")
    op.drop_column("agent_mailboxes", "agent_type")
