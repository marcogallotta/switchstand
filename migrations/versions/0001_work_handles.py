"""Create the sole Bootstrap table."""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_work_handles"
down_revision = None
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.create_table(
        "work_handles",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("provider_work_id", sa.Text(), nullable=False),
        sa.UniqueConstraint("provider", "provider_work_id"),
    )

def downgrade() -> None:
    op.drop_table("work_handles")
