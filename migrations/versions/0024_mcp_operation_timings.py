"""Retain privacy-safe MCP operation timings."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0024_mcp_operation_timings"
down_revision = "0023_activation_continuity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mcp_operation_timings",
        sa.Column("call_id", sa.Text(), primary_key=True),
        sa.Column("tool", sa.Text(), nullable=False),
        sa.Column("target_work_id", postgresql.UUID(as_uuid=True)),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_ms", sa.Float(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("error_class", sa.Text()),
        sa.Column("db_count", sa.Integer(), nullable=False),
        sa.Column("db_total_ms", sa.Float(), nullable=False),
        sa.Column("db_max_ms", sa.Float(), nullable=False),
        sa.Column("child_union_ms", sa.Float(), nullable=False),
        sa.Column("server_residual_ms", sa.Float(), nullable=False),
        sa.Column("runtime_generation", sa.Text()),
        sa.Column("schema_generation", sa.Text(), nullable=False),
        sa.CheckConstraint("status IN ('ok','error')"),
        sa.CheckConstraint(
            "duration_ms>=0 AND db_count>=0 AND db_total_ms>=0 AND db_max_ms>=0 "
            "AND child_union_ms>=0 AND server_residual_ms>=0"
        ),
    )
    op.create_index(
        "ix_mcp_operation_timings_work_started",
        "mcp_operation_timings", ["target_work_id", "started_at"],
    )


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(sa.text("LOCK TABLE mcp_operation_timings IN ACCESS EXCLUSIVE MODE"))
    if connection.scalar(sa.text("SELECT count(*) FROM mcp_operation_timings")):
        raise RuntimeError("preserve durable MCP operation timings; use a forward migration")
    op.drop_index("ix_mcp_operation_timings_work_started")
    op.drop_table("mcp_operation_timings")
