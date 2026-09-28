"""Bind durable agent endpoints to owner and ChatGPT chat identity."""

import sqlalchemy as sa
from alembic import op

revision = "0007_agent_chat_identity"
down_revision = "0006_agent_mailboxes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("agent_mailboxes_work_id_fkey", "agent_mailboxes", type_="foreignkey")
    op.drop_constraint("agent_mailboxes_principal_key_key", "agent_mailboxes", type_="unique")
    op.alter_column("agent_mailboxes", "work_id", new_column_name="endpoint_id")
    op.add_column("agent_mailboxes", sa.Column("session_key", sa.Text(), nullable=True))
    # Existing endpoints and deliveries remain intact and are recoverable by same-owner takeover.
    op.execute("UPDATE agent_mailboxes SET session_key = 'legacy:' || name_key")
    op.alter_column("agent_mailboxes", "session_key", nullable=False)
    op.create_unique_constraint(
        "uq_agent_mailbox_principal_session", "agent_mailboxes",
        ["principal_key", "session_key"],
    )


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(sa.text("LOCK TABLE agent_mailboxes IN ACCESS EXCLUSIVE MODE"))
    if connection.scalar(sa.text("SELECT count(*) FROM agent_mailboxes")):
        raise RuntimeError("preserve durable agent endpoint bindings; use a forward migration")
    op.drop_constraint("uq_agent_mailbox_principal_session", "agent_mailboxes", type_="unique")
    op.drop_column("agent_mailboxes", "session_key")
    op.alter_column("agent_mailboxes", "endpoint_id", new_column_name="work_id")
    op.create_foreign_key(
        "agent_mailboxes_work_id_fkey", "agent_mailboxes", "work_handles", ["work_id"], ["id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "agent_mailboxes_principal_key_key", "agent_mailboxes", ["principal_key"],
    )
