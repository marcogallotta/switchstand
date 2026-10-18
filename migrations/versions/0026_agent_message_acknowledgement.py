"""Add the processed-notification acknowledgement delivery state."""

import sqlalchemy as sa
from alembic import op

revision = "0026_agent_message_ack"
down_revision = "0025_task_control_checkpoints"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "message_deliveries_state_check", "message_deliveries", type_="check"
    )
    op.create_check_constraint(
        "message_deliveries_state_check",
        "message_deliveries",
        "state IN ('AVAILABLE', 'RECEIVED', 'ACKNOWLEDGED', 'DISPOSITIONED')",
    )


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(sa.text(
        "LOCK TABLE message_deliveries IN ACCESS EXCLUSIVE MODE"
    ))
    if connection.scalar(sa.text(
        "SELECT count(*) FROM message_deliveries WHERE state = 'ACKNOWLEDGED'"
    )):
        raise RuntimeError(
            "preserve acknowledged agent notifications; use a forward migration"
        )
    op.drop_constraint(
        "message_deliveries_state_check", "message_deliveries", type_="check"
    )
    op.create_check_constraint(
        "message_deliveries_state_check",
        "message_deliveries",
        "state IN ('AVAILABLE', 'RECEIVED', 'DISPOSITIONED')",
    )
