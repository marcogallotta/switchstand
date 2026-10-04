"""Record server admission time for newly created canonical work."""

import sqlalchemy as sa
from alembic import op

revision = "0015_work_admission_time"
down_revision = "0014_canonical_routing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "canonical_work", sa.Column("admitted_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(sa.text("LOCK TABLE canonical_work IN ACCESS EXCLUSIVE MODE"))
    if connection.scalar(sa.text(
        "SELECT EXISTS (SELECT 1 FROM canonical_work WHERE admitted_at IS NOT NULL)"
    )):
        raise RuntimeError("preserve canonical work admission evidence; use a forward migration")
    op.drop_column("canonical_work", "admitted_at")
