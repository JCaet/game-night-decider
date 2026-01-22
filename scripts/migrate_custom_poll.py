"""
Migration script to update to the refactored poll schema.

Changes:
- Session table: Replace settings_single_poll (Boolean) with poll_type (Integer, default 0)
- PollVote table: game_id is now nullable instead of having a default of 0

This script drops old poll data since the schema change is significant.
Run this script to update an existing database.
"""
import asyncio
import logging
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DATABASE_URL = "sqlite+aiosqlite:///game_night.db"


async def migrate():
    engine = create_async_engine(DATABASE_URL, echo=True)

    async with engine.begin() as conn:
        # Check current sessions table schema
        def check_session_columns(connection):
            inspector = inspect(connection)
            columns = {c["name"]: c for c in inspector.get_columns("sessions")}
            return columns

        columns = await conn.run_sync(check_session_columns)

        # Handle sessions table migration
        if "settings_single_poll" in columns and "poll_type" not in columns:
            logger.info("Migrating sessions table: settings_single_poll -> poll_type...")
            # Add new column
            await conn.execute(text("ALTER TABLE sessions ADD COLUMN poll_type INTEGER DEFAULT 0"))
            # Migrate data: True (custom) -> 0, False (native) -> 1
            await conn.execute(text("""
                UPDATE sessions SET poll_type = CASE
                    WHEN settings_single_poll = 1 THEN 0
                    ELSE 1
                END
            """))
            logger.info("✅ Migrated sessions.settings_single_poll to poll_type")
        elif "poll_type" in columns:
            logger.info("✓ poll_type column already exists in sessions")
        else:
            # Fresh database, add poll_type
            logger.info("Adding poll_type column to sessions table...")
            await conn.execute(text("ALTER TABLE sessions ADD COLUMN poll_type INTEGER DEFAULT 0"))
            logger.info("✅ Added poll_type column")

        # Handle poll_votes table migration - just drop and recreate
        logger.info("Recreating poll_votes table with nullable game_id...")

        # Drop old table and create new
        await conn.execute(text("DROP TABLE IF EXISTS poll_votes"))
        await conn.execute(text("""
            CREATE TABLE poll_votes (
                poll_id VARCHAR NOT NULL,
                user_id BIGINT NOT NULL,
                game_id BIGINT,
                user_name VARCHAR,
                PRIMARY KEY (poll_id, user_id),
                FOREIGN KEY (poll_id) REFERENCES game_night_polls (poll_id)
            )
        """))
        logger.info("✅ Recreated poll_votes table with nullable game_id")

        # Clear old polls since schema changed
        await conn.execute(text("DELETE FROM game_night_polls"))
        logger.info("✅ Cleared old poll data")

    await engine.dispose()
    logger.info("✅ Migration complete!")


if __name__ == "__main__":
    asyncio.run(migrate())
