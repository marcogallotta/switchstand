"""Add append-only failure records and resolutions."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0013_failure_journal"
down_revision = "0012_outcome_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "work_migration_receipts",
        sa.Column("name", sa.Text(), primary_key=True),
        sa.Column("source_digest", sa.Text(), nullable=False),
        sa.Column(
            "completed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "length(source_digest) = 64", name="ck_work_migration_receipt_digest"
        ),
    )
    op.create_table(
        "failure_records",
        sa.Column("attempt_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("operation_id", postgresql.UUID(as_uuid=True), nullable=False, unique=True),
        sa.Column("schema_version", sa.BigInteger(), nullable=False),
        sa.Column("owner", sa.Text(), nullable=False),
        sa.Column("attempted_claim", sa.Text(), nullable=False),
        sa.Column("observed_result", sa.Text(), nullable=False),
        sa.Column("clearing_action", sa.Text(), nullable=False),
        sa.Column("effect_state", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("evidence", postgresql.JSONB(), nullable=False),
        sa.Column("content_digest", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("schema_version = 1", name="ck_failure_record_schema"),
        sa.CheckConstraint(
            "effect_state IN ('NOT_SENT', 'APPLIED', 'UNKNOWN')", name="ck_failure_effect_state"
        ),
        sa.CheckConstraint("length(content_digest) = 64", name="ck_failure_record_digest"),
    )
    op.create_table(
        "failure_resolutions",
        sa.Column("resolution_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("operation_id", postgresql.UUID(as_uuid=True), nullable=False, unique=True),
        sa.Column(
            "attempt_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("failure_records.attempt_id", ondelete="RESTRICT"),
            nullable=False,
            unique=True,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("evidence", postgresql.JSONB(), nullable=False),
        sa.Column("content_digest", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("length(content_digest) = 64", name="ck_failure_resolution_digest"),
    )


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(
        sa.text(
            "LOCK TABLE failure_resolutions, failure_records, work_migration_receipts "
            "IN ACCESS EXCLUSIVE MODE"
        )
    )
    if (
        connection.scalar(sa.text("SELECT count(*) FROM failure_records"))
        or connection.scalar(sa.text("SELECT count(*) FROM work_migration_receipts"))
    ):
        raise RuntimeError("preserve durable failure or migration evidence; use a forward migration")
    op.drop_table("failure_resolutions")
    op.drop_table("failure_records")
    op.drop_table("work_migration_receipts")
