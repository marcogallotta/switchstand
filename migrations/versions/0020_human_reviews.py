"""Persist exact Human Review consequences and decisions."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0020_human_reviews"
down_revision = "0019_task_run_results"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "human_review_consequences",
        sa.Column("consequence_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "package_work_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("canonical_work.work_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("package_revision", sa.Text(), nullable=False),
        sa.Column("consequence_digest", sa.Text(), nullable=False),
        sa.Column("consequence", postgresql.JSONB(), nullable=False),
        sa.Column("decision", sa.Text()),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("package_revision <> ''", name="ck_human_review_package_revision"),
        sa.CheckConstraint("length(consequence_digest) = 64", name="ck_human_review_digest"),
        sa.CheckConstraint(
            "decision IS NULL OR decision IN ('APPROVED', 'WAIT', 'HOLD', 'NO_DISPATCH')",
            name="ck_human_review_decision",
        ),
        sa.CheckConstraint(
            "state IN ('PENDING', 'READY_FOR_IMPLEMENTATION', 'WAIT', 'HOLD', 'NO_DISPATCH')",
            name="ck_human_review_state",
        ),
        sa.CheckConstraint(
            "(decision IS NULL AND state = 'PENDING' AND decided_at IS NULL) OR "
            "(decision = 'APPROVED' AND state = 'READY_FOR_IMPLEMENTATION' "
            "AND decided_at IS NOT NULL) OR "
            "(decision IN ('WAIT', 'HOLD', 'NO_DISPATCH') AND state = decision "
            "AND decided_at IS NOT NULL)",
            name="ck_human_review_terminal_shape",
        ),
    )
    op.create_index(
        "ix_human_review_consequences_package_work_id",
        "human_review_consequences",
        ("package_work_id",),
    )


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(sa.text("LOCK TABLE human_review_consequences IN ACCESS EXCLUSIVE MODE"))
    if connection.scalar(sa.text("SELECT count(*) FROM human_review_consequences")):
        raise RuntimeError("preserve durable Human Review decisions; use a forward migration")
    op.drop_index(
        "ix_human_review_consequences_package_work_id",
        table_name="human_review_consequences",
    )
    op.drop_table("human_review_consequences")
