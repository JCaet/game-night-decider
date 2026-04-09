"""
Migration script for Telegram Bot API 9.1/9.6 poll features.

Changes:
- Session table: Add shuffle_options, hide_results, allow_adding_options columns
- New table: poll_added_games (created automatically by create_all)

Run: uv run python scripts/migrate_poll_settings.py
"""

import asyncio
import logging
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from src.core.db import DATABASE_URL

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def migrate():
    engine = create_async_engine(DATABASE_URL, echo=False)

    async with engine.begin() as conn:
        # Add new session columns
        session_columns = [
            ("shuffle_options", "BOOLEAN DEFAULT 0"),
            ("hide_results", "BOOLEAN DEFAULT 0"),
            ("allow_adding_options", "BOOLEAN DEFAULT 0"),
        ]

        for col_name, col_def in session_columns:
            try:
                await conn.execute(
                    text(f"ALTER TABLE sessions ADD COLUMN {col_name} {col_def}")
                )
                logger.info(f"Added sessions.{col_name}")
            except Exception as e:
                if "duplicate" in str(e).lower() or "already exists" in str(e).lower():
                    logger.info(f"sessions.{col_name} already exists, skipping")
                else:
                    logger.error(f"Error adding sessions.{col_name}: {e}")

        # Create poll_added_games table if it doesn't exist
        try:
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS poll_added_games (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    poll_id VARCHAR NOT NULL,
                    game_id BIGINT NOT NULL,
                    added_by_user_id BIGINT NOT NULL,
                    FOREIGN KEY (poll_id) REFERENCES game_night_polls (poll_id),
                    FOREIGN KEY (game_id) REFERENCES games (id),
                    UNIQUE (poll_id, game_id)
                )
            """))
            logger.info("Created poll_added_games table (or already existed)")
        except Exception as e:
            logger.error(f"Error creating poll_added_games table: {e}")

        # Create index on poll_id for poll_added_games
        try:
            await conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_poll_added_games_poll_id "
                "ON poll_added_games (poll_id)"
            ))
            logger.info("Created index on poll_added_games.poll_id")
        except Exception:
            pass

    await engine.dispose()
    logger.info("Migration complete!")


if __name__ == "__main__":
    asyncio.run(migrate())
