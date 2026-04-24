"""add shuffle_seed to game_night_polls

Revision ID: 7c4e9b6d2a11
Revises: b80ab880db99
Create Date: 2026-04-24 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "7c4e9b6d2a11"
down_revision: str | Sequence[str] | None = "b80ab880db99"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add shuffle_seed column for deterministic custom-poll shuffle."""
    with op.batch_alter_table("game_night_polls", schema=None) as batch_op:
        batch_op.add_column(sa.Column("shuffle_seed", sa.Integer(), nullable=True))


def downgrade() -> None:
    """Drop shuffle_seed column."""
    with op.batch_alter_table("game_night_polls", schema=None) as batch_op:
        batch_op.drop_column("shuffle_seed")
