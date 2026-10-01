"""Add the inert append-only human trajectory substrate."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0009_human_trajectory"
down_revision = "0008_work_index_authority"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "human_trajectory_revisions",
        sa.Column("trajectory_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("append_request_id", postgresql.UUID(as_uuid=True), nullable=False, unique=True),
        sa.Column(
            "work_id_ref", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("work_handles.id", ondelete="RESTRICT"), nullable=False,
        ),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column(
            "predecessor_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("human_trajectory_revisions.trajectory_id", ondelete="RESTRICT"),
        ),
        sa.Column("source_kind", sa.Text(), nullable=False),
        sa.Column("source_ref", sa.Text(), nullable=False),
        sa.Column("source_revision", sa.Text()),
        sa.Column("content_digest", sa.Text(), nullable=False),
        sa.Column("trajectory_data", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.UniqueConstraint("work_id_ref", "generation"),
        sa.CheckConstraint("generation >= 1", name="ck_human_trajectory_generation"),
        sa.CheckConstraint(
            "source_kind IN ('HUMAN_INPUT', 'HUMAN_REVIEW', 'HUMAN_STEERING')",
            name="ck_human_trajectory_source_kind",
        ),
        sa.CheckConstraint(
            "length(source_ref) BETWEEN 1 AND 512", name="ck_human_trajectory_source_ref",
        ),
        sa.CheckConstraint(
            "source_revision IS NULL OR length(source_revision) BETWEEN 1 AND 512",
            name="ck_human_trajectory_source_revision",
        ),
        sa.CheckConstraint("length(content_digest) = 64", name="ck_human_trajectory_digest"),
    )


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(sa.text(
        "LOCK TABLE human_trajectory_revisions IN ACCESS EXCLUSIVE MODE"
    ))
    if connection.scalar(sa.text("SELECT count(*) FROM human_trajectory_revisions")):
        raise RuntimeError("preserve durable human trajectory truth; use a forward migration")
    op.drop_table("human_trajectory_revisions")
