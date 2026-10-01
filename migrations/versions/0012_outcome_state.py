"""Add inert owner-local outcome-action snapshots."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0012_outcome_state"
down_revision = "0011_workset_authority"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "outcome_state_revisions",
        sa.Column("state_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("operation_id", postgresql.UUID(as_uuid=True), nullable=False, unique=True),
        sa.Column("owner_work_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("work_handles.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("predecessor_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("outcome_state_revisions.state_id", ondelete="RESTRICT")),
        sa.Column("schema_version", sa.BigInteger(), nullable=False),
        sa.Column("items", postgresql.JSONB(), nullable=False),
        sa.Column("owner_currentness_token", sa.Text(), nullable=False),
        sa.Column("content_digest", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("owner_work_id", "generation"),
        sa.CheckConstraint("generation >= 1", name="ck_outcome_state_generation"),
        sa.CheckConstraint("schema_version = 1", name="ck_outcome_state_schema"),
        sa.CheckConstraint("owner_currentness_token <> ''", name="ck_outcome_state_currentness"),
        sa.CheckConstraint("length(content_digest) = 64", name="ck_outcome_state_digest"),
    )


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(sa.text("LOCK TABLE outcome_state_revisions IN ACCESS EXCLUSIVE MODE"))
    if connection.scalar(sa.text("SELECT count(*) FROM outcome_state_revisions")):
        raise RuntimeError("preserve durable outcome state; use a forward migration")
    op.drop_table("outcome_state_revisions")
