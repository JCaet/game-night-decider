"""add current_page to game_night_polls

Revision ID: a1c7e0f4d92b
Revises: 9f2d8c4a1e3b
Create Date: 2026-06-25 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a1c7e0f4d92b"
down_revision: str | Sequence[str] | None = "9f2d8c4a1e3b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add current_page column tracking the displayed page of a paginated poll."""
    with op.batch_alter_table("game_night_polls", schema=None) as batch_op:
        batch_op.add_column(sa.Column("current_page", sa.Integer(), nullable=True))


def downgrade() -> None:
    """Drop current_page column."""
    with op.batch_alter_table("game_night_polls", schema=None) as batch_op:
        batch_op.drop_column("current_page")
