"""Add current caller grants and durable append intents/receipts."""

from alembic import op
from sqlalchemy import func, select

from switchstand.grant_state import effect_intents, work_grants

revision = "0002_grants_and_effects"
down_revision = "0001_work_handles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    work_grants.create(op.get_bind())
    effect_intents.create(op.get_bind())


def downgrade() -> None:
    connection = op.get_bind()
    if any(connection.scalar(select(func.count()).select_from(table))
           for table in (effect_intents, work_grants)):
        raise RuntimeError("preserve durable grant/effect truth; use a forward recovery migration")
    effect_intents.drop(connection)
    work_grants.drop(connection)
