"""drop poll_type from sessions

Revision ID: d5a92c4f18e6
Revises: c3f81b2e07da
Create Date: 2026-11-11 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d5a92c4f18e6"
down_revision: str | Sequence[str] | None = "c3f81b2e07da"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Drop poll_type: native Telegram polls were removed, leaving one poll kind."""
    with op.batch_alter_table("sessions", schema=None) as batch_op:
        batch_op.drop_column("poll_type")


def downgrade() -> None:
    """Restore poll_type, with every session on the custom poll (0)."""
    with op.batch_alter_table("sessions", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("poll_type", sa.Integer(), nullable=False, server_default="0")
        )
