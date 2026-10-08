"""Add durable canonical task-control checkpoints."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0025_task_control_checkpoints"
down_revision = "0024_mcp_operation_timings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "task_control_checkpoints",
        sa.Column("checkpoint_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("operation_id", postgresql.UUID(as_uuid=True), nullable=False, unique=True),
        sa.Column("work_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("canonical_work.work_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("observed_work_revision", sa.Text(), nullable=False),
        sa.Column("control_basis_digest", sa.Text(), nullable=False),
        sa.Column("capsule", postgresql.JSONB(), nullable=False),
        sa.Column("content_digest", sa.Text(), nullable=False),
        sa.Column("request_digest", sa.Text(), nullable=False),
        sa.Column("principal_key", sa.Text(), nullable=False),
        sa.Column("grant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("grant_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("work_id", "generation", name="uq_task_control_work_generation"),
        sa.CheckConstraint("generation >= 1", name="ck_task_control_generation"),
        sa.CheckConstraint(
            "length(control_basis_digest) = 64 AND length(content_digest) = 64 "
            "AND length(request_digest) = 64", name="ck_task_control_digests",
        ),
        sa.CheckConstraint(
            "observed_work_revision <> '' AND principal_key <> ''",
            name="ck_task_control_nonempty",
        ),
    )


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(sa.text("LOCK TABLE task_control_checkpoints IN ACCESS EXCLUSIVE MODE"))
    if connection.scalar(sa.text("SELECT count(*) FROM task_control_checkpoints")):
        raise RuntimeError("preserve durable task-control checkpoints; use a forward migration")
    op.drop_table("task_control_checkpoints")
