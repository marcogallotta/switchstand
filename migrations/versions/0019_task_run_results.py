"""Persist task-run results and fence terminal selection."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0019_task_run_results"
down_revision = "0018_task_run_executions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "task_run_results",
        sa.Column("result_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("evidence_refs", postgresql.JSONB(), nullable=False),
        sa.Column("content_digest", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ("request_id", "run_id"),
            ("task_run_executions.request_id", "task_run_executions.run_id"),
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("outcome <> ''", name="ck_task_run_result_outcome"),
        sa.CheckConstraint("summary <> ''", name="ck_task_run_result_summary"),
        sa.CheckConstraint("length(content_digest) = 64", name="ck_task_run_result_digest"),
    )
    op.add_column(
        "task_run_requests",
        sa.Column("terminal_result_id", postgresql.UUID(as_uuid=True)),
    )
    op.create_foreign_key(
        "fk_task_run_terminal_result",
        "task_run_requests",
        "task_run_results",
        ("terminal_result_id",),
        ("result_id",),
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(sa.text("LOCK TABLE task_run_results IN ACCESS EXCLUSIVE MODE"))
    if connection.scalar(sa.text("SELECT count(*) FROM task_run_results")):
        raise RuntimeError("preserve durable task-run result evidence; use a forward migration")
    op.drop_constraint("fk_task_run_terminal_result", "task_run_requests", type_="foreignkey")
    op.drop_column("task_run_requests", "terminal_result_id")
    op.drop_table("task_run_results")
