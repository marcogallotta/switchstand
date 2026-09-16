"""Add durable message envelopes, deliveries and projection intents."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003_messages_and_deliveries"
down_revision = "0002_grants_and_effects"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "messages",
        sa.Column("sender_work_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("message_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("route_ref", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("digest", sa.Text(), nullable=False),
        sa.Column("in_reply_to_delivery_id", postgresql.UUID(as_uuid=True), unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "message_deliveries",
        sa.Column("delivery_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("sender_work_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("message_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("recipient_work_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("state", sa.Text(), server_default="AVAILABLE", nullable=False),
        sa.Column("recipient_grant_version", sa.Integer(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True)),
        sa.Column("receiving_generation", sa.Text()),
        sa.Column("dispositioned_at", sa.DateTime(timezone=True)),
        sa.Column("disposition_digest", sa.Text()),
        sa.Column("evidence", postgresql.JSONB()),
        sa.ForeignKeyConstraint(
            ["sender_work_id", "message_id"],
            ["messages.sender_work_id", "messages.message_id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("sender_work_id", "message_id", "recipient_work_id"),
        sa.CheckConstraint("state IN ('AVAILABLE', 'RECEIVED', 'DISPOSITIONED')"),
    )
    op.create_index(
        "ix_message_deliveries_pending",
        "message_deliveries",
        ["recipient_work_id", "state", "delivery_id"],
    )
    op.create_table(
        "message_projection",
        sa.Column("projection_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("sender_work_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("message_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("operation_id", postgresql.UUID(as_uuid=True), unique=True, nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("target", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), server_default="PENDING", nullable=False),
        sa.Column("receipt", postgresql.JSONB()),
        sa.ForeignKeyConstraint(
            ["sender_work_id", "message_id"],
            ["messages.sender_work_id", "messages.message_id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("sender_work_id", "message_id", "provider", "target"),
        sa.CheckConstraint("state IN ('PENDING', 'CONFIRMED', 'UNKNOWN')"),
    )


def downgrade() -> None:
    connection = op.get_bind()
    for name in ("message_projection", "message_deliveries", "messages"):
        if connection.scalar(sa.text(f"SELECT count(*) FROM {name}")):
            raise RuntimeError("preserve durable message truth; use a forward recovery migration")
    op.drop_table("message_projection")
    op.drop_index("ix_message_deliveries_pending", table_name="message_deliveries")
    op.drop_table("message_deliveries")
    op.drop_table("messages")
