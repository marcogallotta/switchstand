"""Persist PR identities and their canonical task links."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0027_pull_requests"
down_revision = "0026_agent_message_ack"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pull_requests",
        sa.Column("repository", sa.Text(), primary_key=True),
        sa.Column("number", sa.BigInteger(), primary_key=True),
        sa.Column("owner_key", sa.Text(), nullable=False),
        sa.Column("row_version", sa.BigInteger(), nullable=False),
        sa.Column("acceptance_ref", sa.Text(), nullable=False),
        sa.CheckConstraint("number > 0 AND row_version > 0", name="ck_pr_positive"),
    )
    op.create_table(
        "work_pull_requests",
        sa.Column("work_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("canonical_work.work_id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("repository", sa.Text(), primary_key=True),
        sa.Column("number", sa.BigInteger(), primary_key=True),
        sa.ForeignKeyConstraint(["repository", "number"],
                                ["pull_requests.repository", "pull_requests.number"],
                                ondelete="RESTRICT"),
    )


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(sa.text("LOCK TABLE pull_requests, work_pull_requests IN ACCESS EXCLUSIVE MODE"))
    if connection.scalar(sa.text("SELECT count(*) FROM pull_requests")):
        raise RuntimeError("preserve durable PR ownership and links; use a forward migration")
    op.drop_table("work_pull_requests")
    op.drop_table("pull_requests")
