"""Add the first durable Lifecycle required-result obligation profile."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004_required_result_persistence"
down_revision = "0003_messages_and_deliveries"
branch_labels = None
depends_on = None

lifecycle_obligations = sa.table(
    "lifecycle_obligations",
    sa.column("obligation_id", postgresql.UUID(as_uuid=True)),
)


def upgrade() -> None:
    op.create_table(
        "lifecycle_obligations",
        sa.Column("obligation_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("profile_type", sa.Text(), nullable=False),
        sa.Column("profile_version", sa.Integer(), nullable=False),
        sa.Column(
            "work_id_ref",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("work_handles.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("currentness_token", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.Column("destination_ref", sa.Text()),
        sa.Column("result_correlation", sa.Text()),
        sa.Column("authoritative_readback_evidence", sa.Text()),
        sa.Column("unknown_reason", sa.Text()),
        sa.Column("unknown_evidence", sa.Text()),
        sa.CheckConstraint(
            "profile_type = 'REQUIRED_RESULT_PERSISTENCE' AND profile_version = 1",
            name="ck_lifecycle_obligations_profile",
        ),
        sa.CheckConstraint("currentness_token <> ''", name="ck_lifecycle_currentness_token"),
        sa.CheckConstraint("row_version >= 1", name="ck_lifecycle_row_version"),
        sa.CheckConstraint(
            "(destination_ref IS NULL) = (result_correlation IS NULL)",
            name="ck_lifecycle_result_pair",
        ),
        sa.CheckConstraint(
            "destination_ref IS NULL OR destination_ref <> ''",
            name="ck_lifecycle_destination_ref",
        ),
        sa.CheckConstraint(
            "result_correlation IS NULL OR result_correlation <> ''",
            name="ck_lifecycle_result_correlation",
        ),
        sa.CheckConstraint(
            "authoritative_readback_evidence IS NULL "
            "OR char_length(authoritative_readback_evidence) BETWEEN 1 AND 8000",
            name="ck_lifecycle_readback_evidence",
        ),
        sa.CheckConstraint(
            "unknown_reason IS NULL OR unknown_reason IN "
            "('PERSIST_OUTCOME_AMBIGUOUS', 'CURRENTNESS_STALE', 'CURRENTNESS_UNKNOWN')",
            name="ck_lifecycle_unknown_reason",
        ),
        sa.CheckConstraint(
            "unknown_evidence IS NULL OR char_length(unknown_evidence) BETWEEN 1 AND 8000",
            name="ck_lifecycle_unknown_evidence",
        ),
        sa.CheckConstraint(
            "(state = 'PENDING_RESULT' "
            "AND destination_ref IS NULL AND authoritative_readback_evidence IS NULL "
            "AND unknown_reason IS NULL AND unknown_evidence IS NULL) OR "
            "(state = 'PERSIST_REQUIRED' "
            "AND destination_ref IS NOT NULL AND authoritative_readback_evidence IS NULL "
            "AND unknown_reason IS NULL AND unknown_evidence IS NULL) OR "
            "(state = 'UNKNOWN' "
            "AND authoritative_readback_evidence IS NULL AND unknown_reason IS NOT NULL "
            "AND unknown_reason <> '' AND unknown_evidence IS NOT NULL) OR "
            "(state = 'TERMINAL' "
            "AND destination_ref IS NOT NULL "
            "AND authoritative_readback_evidence IS NOT NULL "
            "AND unknown_reason IS NULL AND unknown_evidence IS NULL)",
            name="ck_lifecycle_state_fields",
        ),
    )
    op.create_index(
        "ix_lifecycle_obligations_work_id_ref",
        "lifecycle_obligations",
        ["work_id_ref"],
    )


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(sa.text("LOCK TABLE lifecycle_obligations IN ACCESS EXCLUSIVE MODE"))
    if connection.scalar(sa.select(sa.func.count()).select_from(lifecycle_obligations)):
        raise RuntimeError(
            "preserve durable lifecycle obligation truth; use a forward recovery migration"
        )
    op.drop_index(
        "ix_lifecycle_obligations_work_id_ref",
        table_name="lifecycle_obligations",
    )
    op.drop_table("lifecycle_obligations")
