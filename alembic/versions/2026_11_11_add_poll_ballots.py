"""add poll_ballots and game_night_polls.chat_title

Revision ID: e7b3d1a96c40
Revises: d5a92c4f18e6
Create Date: 2026-11-11 00:00:01.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e7b3d1a96c40"
down_revision: str | Sequence[str] | None = "d5a92c4f18e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Track each voter's ballot widget, and the group name shown on it."""
    op.create_table(
        "poll_ballots",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("poll_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.Integer(), nullable=False),
        sa.Column("catalog_hash", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["poll_id"], ["game_night_polls.poll_id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("poll_id", "user_id", name="uq_poll_ballot_user"),
    )
    with op.batch_alter_table("poll_ballots", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_poll_ballots_poll_id"), ["poll_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_poll_ballots_user_id"), ["user_id"], unique=False)
    with op.batch_alter_table("game_night_polls", schema=None) as batch_op:
        batch_op.add_column(sa.Column("chat_title", sa.String(), nullable=True))


def downgrade() -> None:
    """Drop ballot tracking."""
    with op.batch_alter_table("game_night_polls", schema=None) as batch_op:
        batch_op.drop_column("chat_title")
    with op.batch_alter_table("poll_ballots", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_poll_ballots_user_id"))
        batch_op.drop_index(batch_op.f("ix_poll_ballots_poll_id"))
    op.drop_table("poll_ballots")
