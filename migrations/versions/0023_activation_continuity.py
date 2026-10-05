"""Persist activation acceptance/adoption continuity.

Revision ID: 0023_activation_continuity
Revises: 0022_implementation_requests
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0023_activation_continuity"
down_revision = "0022_implementation_requests"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "activation_obligation_revisions",
        sa.Column("obligation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("operation_id", postgresql.UUID(as_uuid=True), nullable=False, unique=True),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("record", postgresql.JSONB(), nullable=False),
        sa.PrimaryKeyConstraint("obligation_id", "generation"),
    )


def downgrade() -> None:
    op.drop_table("activation_obligation_revisions")
