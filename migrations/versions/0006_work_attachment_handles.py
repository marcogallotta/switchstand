"""Add durable opaque work-attachment identities."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0006_work_attachment_handles"
down_revision = "0005_work_event_handles"
branch_labels = None
depends_on = None

work_attachment_handles = sa.table(
    "work_attachment_handles",
    sa.column("id", postgresql.UUID(as_uuid=True)),
)


def upgrade() -> None:
    op.create_table(
        "work_attachment_handles",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "work_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("work_handles.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("provider_work_id", sa.Text(), nullable=False),
        sa.Column("provider_attachment_id", sa.Text(), nullable=False),
        sa.UniqueConstraint(
            "work_id", "provider", "provider_work_id", "provider_attachment_id",
            name="uq_work_attachment_handles_source",
        ),
        sa.CheckConstraint("provider <> ''", name="ck_work_attachment_provider"),
        sa.CheckConstraint(
            "provider_work_id <> ''", name="ck_work_attachment_provider_work"
        ),
        sa.CheckConstraint(
            "provider_attachment_id <> ''", name="ck_work_attachment_provider_attachment"
        ),
    )
    op.create_index(
        "ix_work_attachment_handles_work_id",
        "work_attachment_handles",
        ["work_id"],
    )


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(sa.text(
        "LOCK TABLE work_attachment_handles IN ACCESS EXCLUSIVE MODE"
    ))
    if connection.scalar(sa.select(sa.func.count()).select_from(work_attachment_handles)):
        raise RuntimeError(
            "preserve durable opaque work-attachment identities; use a forward migration"
        )
    op.drop_index(
        "ix_work_attachment_handles_work_id", table_name="work_attachment_handles"
    )
    op.drop_table("work_attachment_handles")
