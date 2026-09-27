"""Add durable ordinary-agent names and replacement generations."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0006_agent_bindings"
down_revision = "0005_work_event_handles"
branch_labels = None
depends_on = None


agent_bindings = sa.table(
    "agent_bindings",
    sa.column("agent_id", postgresql.UUID(as_uuid=True)),
)


def upgrade() -> None:
    op.create_table(
        "agent_bindings",
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False, unique=True),
        sa.Column(
            "mailbox_work_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("work_handles.id", ondelete="RESTRICT"),
            nullable=False, unique=True,
        ),
        sa.Column("principal_key", sa.Text(), nullable=False),
        sa.Column("session_generation", sa.Text(), nullable=False),
        sa.Column("binding_generation", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("principal_key", "session_generation"),
        sa.CheckConstraint("binding_generation >= 1"),
        sa.CheckConstraint("name <> ''"),
        sa.CheckConstraint("principal_key <> ''"),
        sa.CheckConstraint("session_generation <> ''"),
    )


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(sa.text("LOCK TABLE agent_bindings IN ACCESS EXCLUSIVE MODE"))
    if connection.scalar(sa.select(sa.func.count()).select_from(agent_bindings)):
        raise RuntimeError("preserve durable agent identities; use a forward migration")
    op.drop_table("agent_bindings")
