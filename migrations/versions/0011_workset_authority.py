"""Add inert Stage 3 workset and structure authority storage."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0011_workset_authority"
down_revision = "0010_work_metadata_authority"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workset_authority",
        sa.Column("scope", sa.Text(), primary_key=True),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("cutover_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("scope = 'workspace'", name="ck_workset_authority_scope"),
        sa.CheckConstraint("state = 'POSTGRES_AUTHORITY'", name="ck_workset_authority_state"),
        sa.CheckConstraint("generation >= 1", name="ck_workset_authority_generation"),
    )
    op.create_table(
        "workset_cutovers",
        sa.Column("scope", sa.Text(), primary_key=True),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("cutover_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("scope = 'workspace'", name="ck_workset_cutover_scope"),
        sa.CheckConstraint("generation >= 1", name="ck_workset_cutover_generation"),
    )
    op.create_table(
        "worksets",
        sa.Column("workset_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("workset_key", sa.Text(), nullable=False, unique=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("role_identity", sa.Text(), unique=True),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("row_version", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("workset_key <> ''", name="ck_workset_key"),
        sa.CheckConstraint("name <> ''", name="ck_workset_name"),
        sa.CheckConstraint("kind <> ''", name="ck_workset_kind"),
        sa.CheckConstraint("role_identity IS NULL OR role_identity <> ''", name="ck_workset_role"),
        sa.CheckConstraint("state IN ('ACTIVE', 'RETIRED')", name="ck_workset_state"),
        sa.CheckConstraint("row_version >= 1", name="ck_workset_version"),
    )
    op.create_table(
        "workset_memberships",
        sa.Column("workset_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("worksets.workset_id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("work_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("work_index.work_id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("semantics", sa.Text(), nullable=False),
        sa.Column("member_role", sa.Text(), nullable=False),
        sa.Column("row_version", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("semantics IN ('AUTHORITATIVE', 'RELATED')", name="ck_workset_membership_semantics"),
        sa.CheckConstraint("member_role IN ('MASTER', 'MEMBER')", name="ck_workset_membership_role"),
        sa.CheckConstraint("member_role <> 'MASTER' OR semantics = 'AUTHORITATIVE'", name="ck_workset_master_authoritative"),
        sa.CheckConstraint("row_version >= 1", name="ck_workset_membership_version"),
    )
    op.create_index("uq_workset_authoritative_work", "workset_memberships", ["work_id"],
                    unique=True, postgresql_where=sa.text("semantics = 'AUTHORITATIVE'"))
    op.create_index("uq_workset_master", "workset_memberships", ["workset_id"],
                    unique=True, postgresql_where=sa.text("member_role = 'MASTER'"))
    op.create_table(
        "work_parent_edges",
        sa.Column("child_work_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("work_index.work_id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("parent_work_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("work_index.work_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("row_version", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("child_work_id <> parent_work_id", name="ck_work_parent_not_self"),
        sa.CheckConstraint("row_version >= 1", name="ck_work_parent_version"),
    )


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(sa.text("LOCK TABLE workset_cutovers IN ACCESS EXCLUSIVE MODE"))
    if connection.scalar(sa.text("SELECT count(*) FROM workset_cutovers")):
        raise RuntimeError("POSTGRES_AUTHORITY is irreversible; use a forward migration")
    if connection.scalar(sa.text("SELECT count(*) FROM workset_authority")):
        raise RuntimeError("preserve Stage 3 authority truth; use a forward migration")
    op.drop_table("work_parent_edges")
    op.drop_index("uq_workset_master", table_name="workset_memberships")
    op.drop_index("uq_workset_authoritative_work", table_name="workset_memberships")
    op.drop_table("workset_memberships")
    op.drop_table("worksets")
    op.drop_table("workset_cutovers")
    op.drop_table("workset_authority")
