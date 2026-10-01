"""Add Stage 1 DB-authoritative title, completion, and admitted-work index."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0008_work_index_authority"
down_revision = "0007_agent_chat_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "work_authority",
        sa.Column("scope", sa.Text(), primary_key=True),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("cutover_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("scope = 'workspace'", name="ck_work_authority_scope"),
        sa.CheckConstraint("state = 'POSTGRES_AUTHORITY'", name="ck_work_authority_state"),
        sa.CheckConstraint("generation >= 1", name="ck_work_authority_generation"),
    )
    op.create_table(
        "work_authority_cutovers",
        sa.Column("scope", sa.Text(), primary_key=True),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("cutover_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("scope = 'workspace'", name="ck_work_cutover_scope"),
        sa.CheckConstraint("generation >= 1", name="ck_work_cutover_generation"),
    )
    op.create_table(
        "work_index",
        sa.Column(
            "work_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("work_handles.id", ondelete="RESTRICT"), primary_key=True,
        ),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("normalized_title", sa.Text(), nullable=False),
        sa.Column("completed", sa.Boolean(), nullable=False),
        sa.Column("provider_revision", sa.Text(), nullable=False),
        sa.Column("row_version", sa.BigInteger(), nullable=False),
        sa.Column("routing", postgresql.JSONB(), nullable=False),
        sa.Column("context", postgresql.JSONB(), nullable=False),
        sa.CheckConstraint("title <> ''", name="ck_work_index_title"),
        sa.CheckConstraint("normalized_title <> ''", name="ck_work_index_normalized_title"),
        sa.CheckConstraint("provider_revision <> ''", name="ck_work_index_provider_revision"),
        sa.CheckConstraint("row_version >= 1", name="ck_work_index_row_version"),
    )
    op.create_index(
        "ix_work_index_title_search", "work_index",
        [sa.text("to_tsvector('simple', normalized_title)")], postgresql_using="gin",
    )
    op.create_index(
        "ix_work_index_page", "work_index", ["normalized_title", "work_id"],
    )


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(sa.text("LOCK TABLE work_authority_cutovers IN ACCESS EXCLUSIVE MODE"))
    if connection.scalar(sa.text("SELECT count(*) FROM work_authority_cutovers")):
        raise RuntimeError("POSTGRES_AUTHORITY is irreversible; use a forward migration")
    if connection.scalar(sa.text("SELECT count(*) FROM work_authority")):
        raise RuntimeError("preserve Stage 1 authority truth; use a forward migration")
    op.drop_index("ix_work_index_page", table_name="work_index")
    op.drop_index("ix_work_index_title_search", table_name="work_index")
    op.drop_table("work_index")
    op.drop_table("work_authority_cutovers")
    op.drop_table("work_authority")
