"""Persist canonical root, owner, and next-action routing."""

import sqlalchemy as sa
from alembic import op

revision = "0014_canonical_routing"
down_revision = "0013_failure_journal"
branch_labels = None
depends_on = None

_COLUMNS = ("canonical_root", "owner_key", "next_action_class", "next_action_ref")


def upgrade() -> None:
    for name in _COLUMNS:
        op.add_column("canonical_work", sa.Column(name, sa.Text(), nullable=True))


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(sa.text("LOCK TABLE canonical_work IN ACCESS EXCLUSIVE MODE"))
    populated = " OR ".join(f"{name} IS NOT NULL" for name in _COLUMNS)
    if connection.scalar(sa.text(
        f"SELECT EXISTS (SELECT 1 FROM canonical_work WHERE {populated})"
    )):
        raise RuntimeError("preserve canonical routing state; use a forward migration")
    for name in reversed(_COLUMNS):
        op.drop_column("canonical_work", name)
