"""Authorize implementation task-run requests without rewriting prior migrations."""

import sqlalchemy as sa
from alembic import op

revision = "0022_implementation_requests"
down_revision = "0021_human_reviews"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("task_run_requests", sa.Column("authorization_ref", sa.Text()))
    op.add_column("task_run_requests", sa.Column("send_authority_ref", sa.Text()))
    op.drop_constraint("ck_task_run_request_kind", "task_run_requests", type_="check")
    op.create_check_constraint(
        "ck_task_run_request_kind", "task_run_requests",
        "task_kind IN ('INVESTIGATION', 'VALIDATION', 'IMPLEMENTATION')",
    )
    op.create_check_constraint(
        "ck_task_run_request_authority", "task_run_requests",
        "(task_kind = 'IMPLEMENTATION' AND authorization_ref IS NOT NULL "
        "AND send_authority_ref IS NOT NULL) OR "
        "(task_kind <> 'IMPLEMENTATION' AND authorization_ref IS NULL "
        "AND send_authority_ref IS NULL)",
    )


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(sa.text("LOCK TABLE task_run_requests IN ACCESS EXCLUSIVE MODE"))
    if connection.scalar(sa.text(
        "SELECT count(*) FROM task_run_requests WHERE task_kind = 'IMPLEMENTATION' "
        "OR authorization_ref IS NOT NULL OR send_authority_ref IS NOT NULL"
    )):
        raise RuntimeError("preserve durable implementation requests; use a forward migration")
    op.drop_constraint("ck_task_run_request_authority", "task_run_requests", type_="check")
    op.drop_constraint("ck_task_run_request_kind", "task_run_requests", type_="check")
    op.create_check_constraint(
        "ck_task_run_request_kind", "task_run_requests",
        "task_kind IN ('INVESTIGATION', 'VALIDATION')",
    )
    op.drop_column("task_run_requests", "send_authority_ref")
    op.drop_column("task_run_requests", "authorization_ref")
