"""drop current_page from game_night_polls

Revision ID: c3f81b2e07da
Revises: a1c7e0f4d92b
Create Date: 2026-10-29 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c3f81b2e07da"
down_revision: str | Sequence[str] | None = "a1c7e0f4d92b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Drop current_page, unused since the paginated keyboard was reverted."""
    with op.batch_alter_table("game_night_polls", schema=None) as batch_op:
        batch_op.drop_column("current_page")


def downgrade() -> None:
    """Restore current_page."""
    with op.batch_alter_table("game_night_polls", schema=None) as batch_op:
        batch_op.add_column(sa.Column("current_page", sa.Integer(), nullable=True))
