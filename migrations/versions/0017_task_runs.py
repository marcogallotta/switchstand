"""Add inert investigation and validation task-run state."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0017_task_runs"
down_revision = "0016_agent_mailbox_transfers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "task_run_requests",
        sa.Column("request_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("operation_id", postgresql.UUID(as_uuid=True), nullable=False, unique=True),
        sa.Column(
            "requester_work_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("work_handles.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "execution_work_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("work_handles.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("observed_revision", sa.Text(), nullable=False),
        sa.Column("task_kind", sa.Text(), nullable=False),
        sa.Column("continuation", sa.Text(), nullable=False),
        sa.Column("candidate_ref", sa.Text()),
        sa.Column("objective", sa.Text(), nullable=False),
        sa.Column("result_contract", postgresql.JSONB(), nullable=False),
        sa.Column("content_digest", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("observed_revision <> ''", name="ck_task_run_request_revision"),
        sa.CheckConstraint(
            "task_kind IN ('INVESTIGATION', 'VALIDATION')", name="ck_task_run_request_kind"
        ),
        sa.CheckConstraint(
            "continuation IN ('START', 'CONTINUE', 'TAKEOVER')",
            name="ck_task_run_request_continuation",
        ),
        sa.CheckConstraint(
            "candidate_ref IS NULL OR candidate_ref <> ''", name="ck_task_run_candidate_ref"
        ),
        sa.CheckConstraint("objective <> ''", name="ck_task_run_objective"),
        sa.CheckConstraint("length(content_digest) = 64", name="ck_task_run_request_digest"),
    )


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(sa.text("LOCK TABLE task_run_requests IN ACCESS EXCLUSIVE MODE"))
    if connection.scalar(sa.text("SELECT count(*) FROM task_run_requests")):
        raise RuntimeError("preserve durable task-run evidence; use a forward migration")
    op.drop_table("task_run_requests")
