"""add community_unplayable_counts to games

Revision ID: 9f2d8c4a1e3b
Revises: 7c4e9b6d2a11
Create Date: 2026-04-28 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "9f2d8c4a1e3b"
down_revision: str | Sequence[str] | None = "7c4e9b6d2a11"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add community_unplayable_counts column for BGG suggested-numplayers blocklist."""
    with op.batch_alter_table("games", schema=None) as batch_op:
        batch_op.add_column(sa.Column("community_unplayable_counts", sa.String(), nullable=True))


def downgrade() -> None:
    """Drop community_unplayable_counts column."""
    with op.batch_alter_table("games", schema=None) as batch_op:
        batch_op.drop_column("community_unplayable_counts")
