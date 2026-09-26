"""Add transitional durable agent mailboxes."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0006_agent_mailboxes"
down_revision = "0005_work_event_handles"
branch_labels = None
depends_on = None

agent_mailboxes = sa.table(
    "agent_mailboxes",
    sa.column("name_key", sa.Text()),
)


def upgrade() -> None:
    op.create_table(
        "agent_mailboxes",
        sa.Column("name_key", sa.Text(), primary_key=True),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column(
            "work_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("work_handles.id", ondelete="RESTRICT"), nullable=False,
        ),
        sa.Column("principal_key", sa.Text(), nullable=False, unique=True),
        sa.UniqueConstraint("work_id", name="uq_agent_mailbox_work_id"),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.CheckConstraint("generation >= 1", name="ck_agent_mailbox_generation"),
    )


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(sa.text("LOCK TABLE agent_mailboxes IN ACCESS EXCLUSIVE MODE"))
    if connection.scalar(sa.select(sa.func.count()).select_from(agent_mailboxes)):
        raise RuntimeError("preserve durable agent mailbox bindings; use a forward migration")
    op.drop_table("agent_mailboxes")
