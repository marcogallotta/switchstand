"""Add inert durable priority-claim occurrences."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0017_priority_claims"
down_revision = "0016_agent_mailbox_transfers"
branch_labels = None
depends_on = None
def upgrade() -> None:
    op.create_table(
        "priority_claims",
        sa.Column("claim_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("claim_kind", sa.Text(), nullable=False),
        sa.Column("subject_kind", sa.Text(), nullable=False),
        sa.Column("subject_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("relation_kind", sa.Text(), nullable=False),
        sa.Column("relation_target_id", postgresql.UUID(as_uuid=True)),
        sa.Column("band", sa.Text()),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("source_label", sa.Text(), nullable=False),
        sa.Column("source_ref", sa.Text(), nullable=False),
        sa.Column("source_observed_revision", sa.Text()),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column(
            "supersedes_claim_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("priority_claims.claim_id", ondelete="RESTRICT"), unique=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.CheckConstraint(
            "claim_kind IN ('HUMAN_PRIORITY', 'AGENT_RECOMMENDATION')",
            name="ck_priority_claim_kind",
        ),
        sa.CheckConstraint(
            "subject_kind IN ('WORK', 'PROJECT')", name="ck_priority_subject_kind",
        ),
        sa.CheckConstraint(
            "relation_kind IN ('BAND', 'BEFORE', 'HOLD')",
            name="ck_priority_relation_kind",
        ),
        sa.CheckConstraint("state IN ('CURRENT', 'SUPERSEDED')", name="ck_priority_state"),
        sa.CheckConstraint(
            "(relation_kind = 'BAND' AND band IS NOT NULL AND band IN ('HIGH', 'NORMAL') "
            "AND relation_target_id IS NULL) OR "
            "(relation_kind = 'BEFORE' AND band IS NULL AND relation_target_id IS NOT NULL) OR "
            "(relation_kind = 'HOLD' AND band IS NULL AND relation_target_id IS NULL)",
            name="ck_priority_relation_shape",
        ),
        sa.CheckConstraint(
            "length(rationale) BETWEEN 1 AND 300", name="ck_priority_rationale",
        ),
        sa.CheckConstraint(
            "length(source_label) BETWEEN 1 AND 512", name="ck_priority_source_label",
        ),
        sa.CheckConstraint(
            "length(source_ref) BETWEEN 1 AND 512", name="ck_priority_source_ref",
        ),
        sa.CheckConstraint(
            "source_observed_revision IS NULL OR "
            "length(source_observed_revision) BETWEEN 1 AND 512",
            name="ck_priority_source_revision",
        ),
        sa.CheckConstraint(
            "supersedes_claim_id IS NULL OR supersedes_claim_id <> claim_id",
            name="ck_priority_supersedes_not_self",
        ),
    )
    op.create_index(
        "ix_priority_claims_current_subject", "priority_claims",
        ["subject_kind", "subject_id", "created_at"],
        postgresql_where=sa.text("state = 'CURRENT'"),
    )
def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(sa.text("LOCK TABLE priority_claims IN ACCESS EXCLUSIVE MODE"))
    if connection.scalar(sa.text("SELECT count(*) FROM priority_claims")):
        raise RuntimeError("preserve durable priority claims; use a forward migration")
    op.drop_index("ix_priority_claims_current_subject", table_name="priority_claims")
    op.drop_table("priority_claims")
