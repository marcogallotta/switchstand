"""Add durable opaque work-event identities."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0005_work_event_handles"
down_revision = "0004_required_result_persistence"
branch_labels = None
depends_on = None

work_event_handles = sa.table(
    "work_event_handles",
    sa.column("id", postgresql.UUID(as_uuid=True)),
)


def upgrade() -> None:
    op.create_table(
        "work_event_handles",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "work_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("work_handles.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("provider_work_id", sa.Text(), nullable=False),
        sa.Column("provider_event_id", sa.Text(), nullable=False),
        sa.UniqueConstraint(
            "work_id", "provider", "provider_work_id", "provider_event_id",
            name="uq_work_event_handles_source",
        ),
        sa.CheckConstraint("provider <> ''", name="ck_work_event_provider"),
        sa.CheckConstraint("provider_work_id <> ''", name="ck_work_event_provider_work"),
        sa.CheckConstraint("provider_event_id <> ''", name="ck_work_event_provider_event"),
    )
    op.create_index("ix_work_event_handles_work_id", "work_event_handles", ["work_id"])


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(sa.text("LOCK TABLE work_event_handles IN ACCESS EXCLUSIVE MODE"))
    if connection.scalar(sa.select(sa.func.count()).select_from(work_event_handles)):
        raise RuntimeError("preserve durable opaque work-event identities; use a forward migration")
    op.drop_index("ix_work_event_handles_work_id", table_name="work_event_handles")
    op.drop_table("work_event_handles")
