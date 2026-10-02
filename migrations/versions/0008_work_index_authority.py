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

    # Compact zero-Asana persistence lands beside the still-live Stage tables first.
    # Runtime wiring and removal of the Stage tables are separate reviewed slices.
    op.create_table(
        "canonical_work",
        sa.Column("work_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("normalized_title", sa.Text(), nullable=False),
        sa.Column("completed", sa.Boolean(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False),
        sa.Column("assignee", sa.Text()),
        sa.Column("priority", sa.Text()),
        sa.Column("work_type", sa.Text()),
        sa.Column("lifecycle_state", sa.Text()),
        sa.Column("review_next_action", sa.Text()),
        sa.Column("wait_kind", sa.Text()),
        sa.Column("unblock_condition", sa.Text()),
        sa.Column("next_due", sa.Text()),
        sa.Column("row_version", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("title <> ''", name="ck_canonical_work_title"),
        sa.CheckConstraint(
            "normalized_title <> ''", name="ck_canonical_work_normalized_title"
        ),
        sa.CheckConstraint("row_version >= 1", name="ck_canonical_work_version"),
    )
    op.create_index(
        "ix_canonical_work_page", "canonical_work", ["normalized_title", "work_id"]
    )
    op.create_table(
        "legacy_work_aliases",
        sa.Column("asana_task_gid", sa.Text(), primary_key=True),
        sa.Column(
            "work_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("canonical_work.work_id", ondelete="RESTRICT"), nullable=False,
        ),
        sa.CheckConstraint("asana_task_gid <> ''", name="ck_legacy_work_alias_gid"),
    )
    op.create_index("ix_legacy_work_aliases_work_id", "legacy_work_aliases", ["work_id"])
    op.create_table(
        "work_dependencies",
        sa.Column(
            "work_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("canonical_work.work_id", ondelete="RESTRICT"), primary_key=True,
        ),
        sa.Column(
            "depends_on_work_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("canonical_work.work_id", ondelete="RESTRICT"), primary_key=True,
        ),
        sa.CheckConstraint(
            "work_id <> depends_on_work_id", name="ck_work_dependency_not_self"
        ),
    )
    op.create_table(
        "work_parents",
        sa.Column(
            "child_work_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("canonical_work.work_id", ondelete="RESTRICT"), primary_key=True,
        ),
        sa.Column(
            "parent_work_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("canonical_work.work_id", ondelete="RESTRICT"), nullable=False,
        ),
        sa.CheckConstraint("child_work_id <> parent_work_id", name="ck_work_parent_not_self"),
    )
    op.create_index("ix_work_parents_parent_work_id", "work_parents", ["parent_work_id"])
    op.create_table(
        "projects",
        sa.Column("project_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("asana_project_gid", sa.Text(), unique=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "asana_project_gid IS NULL OR asana_project_gid <> ''",
            name="ck_project_legacy_gid",
        ),
        sa.CheckConstraint("name <> ''", name="ck_project_name"),
    )
    op.create_table(
        "project_memberships",
        sa.Column(
            "project_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.project_id", ondelete="RESTRICT"), primary_key=True,
        ),
        sa.Column(
            "work_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("canonical_work.work_id", ondelete="RESTRICT"), primary_key=True,
        ),
        sa.Column("section_name", sa.Text()),
        sa.CheckConstraint(
            "section_name IS NULL OR section_name <> ''", name="ck_project_section"
        ),
    )
    op.create_table(
        "work_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "work_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("canonical_work.work_id", ondelete="RESTRICT"), nullable=False,
        ),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("result_version", sa.BigInteger(), nullable=False),
        sa.Column("subtype", sa.Text(), nullable=False),
        sa.Column("text", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor", sa.Text()),
        sa.Column("asana_story_gid", sa.Text()),
        sa.Column("operation_id", postgresql.UUID(as_uuid=True)),
        sa.UniqueConstraint("work_id", "sequence"),
        sa.CheckConstraint("sequence >= 1", name="ck_work_event_sequence"),
        sa.CheckConstraint("result_version >= 1", name="ck_work_event_result_version"),
    )
    op.create_index("ix_work_events_page", "work_events", ["work_id", "sequence"])
    op.create_index(
        "uq_work_events_story", "work_events", ["asana_story_gid"], unique=True,
        postgresql_where=sa.text("asana_story_gid IS NOT NULL"),
    )
    op.create_index(
        "uq_work_events_operation", "work_events", ["operation_id"], unique=True,
        postgresql_where=sa.text("operation_id IS NOT NULL"),
    )


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(sa.text("LOCK TABLE work_authority_cutovers IN ACCESS EXCLUSIVE MODE"))
    if connection.scalar(sa.text("SELECT count(*) FROM work_authority_cutovers")):
        raise RuntimeError("POSTGRES_AUTHORITY is irreversible; use a forward migration")
    if connection.scalar(sa.text("SELECT count(*) FROM work_authority")):
        raise RuntimeError("preserve Stage 1 authority truth; use a forward migration")
    op.drop_index("uq_work_events_operation", table_name="work_events")
    op.drop_index("uq_work_events_story", table_name="work_events")
    op.drop_index("ix_work_events_page", table_name="work_events")
    op.drop_table("work_events")
    op.drop_table("project_memberships")
    op.drop_table("projects")
    op.drop_index("ix_work_parents_parent_work_id", table_name="work_parents")
    op.drop_table("work_parents")
    op.drop_table("work_dependencies")
    op.drop_index("ix_legacy_work_aliases_work_id", table_name="legacy_work_aliases")
    op.drop_table("legacy_work_aliases")
    op.drop_index("ix_canonical_work_page", table_name="canonical_work")
    op.drop_table("canonical_work")
    op.drop_index("ix_work_index_page", table_name="work_index")
    op.drop_index("ix_work_index_title_search", table_name="work_index")
    op.drop_table("work_index")
    op.drop_table("work_authority_cutovers")
    op.drop_table("work_authority")
