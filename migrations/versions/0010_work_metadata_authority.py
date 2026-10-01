"""Add inert Stage 2 structured work metadata authority."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0010_work_metadata_authority"
down_revision = "0009_human_trajectory"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "work_metadata_authority",
        sa.Column("scope", sa.Text(), primary_key=True),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("cutover_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("scope = 'workspace'", name="ck_work_metadata_authority_scope"),
        sa.CheckConstraint("state = 'POSTGRES_AUTHORITY'", name="ck_work_metadata_authority_state"),
        sa.CheckConstraint("generation >= 1", name="ck_work_metadata_authority_generation"),
    )
    op.create_table(
        "work_metadata_cutovers",
        sa.Column("scope", sa.Text(), primary_key=True),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("cutover_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("scope = 'workspace'", name="ck_work_metadata_cutover_scope"),
        sa.CheckConstraint("generation >= 1", name="ck_work_metadata_cutover_generation"),
    )
    op.create_table(
        "work_edges",
        sa.Column("work_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("work_index.work_id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("depends_on_work_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("work_index.work_id", ondelete="RESTRICT"), primary_key=True),
        sa.CheckConstraint("work_id <> depends_on_work_id", name="ck_work_edge_not_self"),
    )


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(sa.text("LOCK TABLE work_metadata_cutovers IN ACCESS EXCLUSIVE MODE"))
    if connection.scalar(sa.text("SELECT count(*) FROM work_metadata_cutovers")):
        raise RuntimeError("POSTGRES_AUTHORITY is irreversible; use a forward migration")
    if connection.scalar(sa.text("SELECT count(*) FROM work_metadata_authority")):
        raise RuntimeError("preserve Stage 2 authority truth; use a forward migration")
    op.drop_table("work_edges")
    op.drop_table("work_metadata_cutovers")
    op.drop_table("work_metadata_authority")
