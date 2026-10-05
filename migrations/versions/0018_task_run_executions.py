"""Bind task requests to managed executions."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0018_task_run_executions"
down_revision = "0017_task_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "task_run_executions",
        sa.Column(
            "request_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("task_run_requests.request_id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "bound_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("run_id", name="uq_task_run_execution_run"),
    )


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(sa.text("LOCK TABLE task_run_executions IN ACCESS EXCLUSIVE MODE"))
    if connection.scalar(sa.text("SELECT count(*) FROM task_run_executions")):
        raise RuntimeError("preserve durable task-run execution evidence; use a forward migration")
    op.drop_table("task_run_executions")
