"""Add the inert compact zero-Asana work and event model."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0013_canonical_work"
down_revision = "0012_outcome_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "canonical_work",
        sa.Column("work_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("normalized_title", sa.Text(), nullable=False),
        sa.Column("completed", sa.Boolean(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False),
        sa.Column("priority", sa.Text()),
        sa.Column("work_type", sa.Text()),
        sa.Column("lifecycle_state", sa.Text()),
        sa.Column("review_next_action", sa.Text()),
        sa.Column("wait_kind", sa.Text()),
        sa.Column("unblock_condition", sa.Text()),
        sa.Column("next_due", sa.Text()),
        sa.Column("row_version", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("title <> ''", name="ck_canonical_work_title"),
        sa.CheckConstraint("normalized_title <> ''", name="ck_canonical_work_normalized_title"),
        sa.CheckConstraint("row_version >= 1", name="ck_canonical_work_version"),
    )
    op.create_index("ix_canonical_work_page", "canonical_work", ["normalized_title", "work_id"])
    op.create_table(
        "legacy_work_aliases",
        sa.Column("asana_task_gid", sa.Text(), primary_key=True),
        sa.Column("work_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("canonical_work.work_id", ondelete="RESTRICT"), nullable=False),
        sa.CheckConstraint("asana_task_gid <> ''", name="ck_legacy_work_alias_gid"),
    )
    op.create_index("ix_legacy_work_aliases_work_id", "legacy_work_aliases", ["work_id"])
    op.create_table(
        "canonical_dependencies",
        sa.Column("work_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("canonical_work.work_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("depends_on_work_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("canonical_work.work_id", ondelete="RESTRICT"), primary_key=True),
        sa.CheckConstraint("work_id <> depends_on_work_id", name="ck_canonical_dependency_not_self"),
    )
    op.create_table(
        "canonical_parents",
        sa.Column("child_work_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("canonical_work.work_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("parent_work_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("canonical_work.work_id", ondelete="RESTRICT"), nullable=False),
        sa.CheckConstraint("child_work_id <> parent_work_id", name="ck_canonical_parent_not_self"),
    )
    op.create_table(
        "canonical_projects",
        sa.Column("project_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("asana_project_gid", sa.Text(), unique=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.CheckConstraint("name <> ''", name="ck_canonical_project_name"),
    )
    op.create_table(
        "canonical_project_memberships",
        sa.Column("project_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("canonical_projects.project_id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("work_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("canonical_work.work_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("section_name", sa.Text()),
    )
    op.create_table(
        "work_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("work_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("canonical_work.work_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("subtype", sa.Text(), nullable=False),
        sa.Column("text", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor", sa.Text()),
        sa.Column("asana_story_gid", sa.Text()),
        sa.Column("operation_id", postgresql.UUID(as_uuid=True)),
        sa.UniqueConstraint("work_id", "sequence"),
        sa.CheckConstraint("sequence >= 1", name="ck_work_event_sequence"),
    )
    op.create_index("ix_work_events_page", "work_events", ["work_id", "sequence"])
    op.create_index("uq_work_events_story", "work_events", ["asana_story_gid"], unique=True,
                    postgresql_where=sa.text("asana_story_gid IS NOT NULL"))
    op.create_index("uq_work_events_operation", "work_events", ["operation_id"], unique=True,
                    postgresql_where=sa.text("operation_id IS NOT NULL"))


def downgrade() -> None:
    op.drop_table("work_events")
    op.drop_table("canonical_project_memberships")
    op.drop_table("canonical_projects")
    op.drop_table("canonical_parents")
    op.drop_table("canonical_dependencies")
    op.drop_table("legacy_work_aliases")
    op.drop_table("canonical_work")
