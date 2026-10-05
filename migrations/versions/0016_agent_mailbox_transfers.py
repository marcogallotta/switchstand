import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0016_agent_mailbox_transfers"
down_revision = "0015_work_admission_time"
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_mailbox_transfer_requests",
        sa.Column("request_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name_key", sa.Text(), nullable=False),
        sa.Column("endpoint_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("expected_generation", sa.Integer(), nullable=False),
        sa.Column("source_principal_key", sa.Text(), nullable=False),
        sa.Column("source_session_key", sa.Text(), nullable=False),
        sa.Column("destination_principal_key", sa.Text(), nullable=False),
        sa.Column("destination_session_key", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text()),
        sa.Column("resulting_generation", sa.Integer()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("expected_generation >= 1"),
        sa.CheckConstraint("resulting_generation IS NULL OR resulting_generation >= 2"),
        sa.CheckConstraint("status IN ('PENDING', 'APPROVED', 'STALE', 'CONFLICT')"),
    )


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(sa.text(
        "LOCK TABLE agent_mailbox_transfer_requests IN ACCESS EXCLUSIVE MODE"
    ))
    if connection.scalar(sa.text(
        "SELECT EXISTS (SELECT 1 FROM agent_mailbox_transfer_requests)"
    )):
        raise RuntimeError("preserve mailbox transfer audit evidence; use a forward migration")
    op.drop_table("agent_mailbox_transfer_requests")
