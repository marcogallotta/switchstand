"""Add current caller grants and durable append intents/receipts."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002_grants_and_effects"
down_revision = "0001_work_handles"
branch_labels = None
depends_on = None

work_grants = sa.table("work_grants", sa.column("principal_key", sa.Text()))
effect_intents = sa.table("effect_intents", sa.column("operation_id", sa.Text()))


def upgrade() -> None:
    op.create_table(
        "work_grants",
        sa.Column("principal_key", sa.Text(), primary_key=True),
        sa.Column("document", postgresql.JSONB(), nullable=True),
    )
    op.create_table(
        "effect_intents",
        sa.Column("operation_id", sa.Text(), primary_key=True),
        sa.Column("fingerprint", sa.Text(), nullable=False),
        sa.Column("principal_key", sa.Text(), nullable=False),
        sa.Column("work_id", sa.Text(), nullable=False),
        sa.Column("grant_id", sa.Text(), nullable=False),
        sa.Column("grant_version", sa.Integer(), nullable=False),
        sa.Column("intent", postgresql.JSONB(), nullable=False),
        sa.Column("outcome", postgresql.JSONB(), nullable=False),
    )
    op.create_index("ix_effect_intents_work_id", "effect_intents", ["work_id"])


def downgrade() -> None:
    connection = op.get_bind()
    if any(
        connection.scalar(sa.select(sa.func.count()).select_from(table))
        for table in (effect_intents, work_grants)
    ):
        raise RuntimeError("preserve durable grant/effect truth; use a forward recovery migration")
    op.drop_index("ix_effect_intents_work_id", table_name="effect_intents")
    op.drop_table("effect_intents")
    op.drop_table("work_grants")
