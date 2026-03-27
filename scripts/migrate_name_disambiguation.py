"""
Migration: add name disambiguation columns.

Changes:
- users table: add telegram_last_name, telegram_username
- poll_votes table: add user_last_name, user_tg_username

All new columns are nullable so existing rows are unaffected.
Run from the project root: uv run python scripts/migrate_name_disambiguation.py
"""
import asyncio
import logging
import sys
from pathlib import Path

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
        def get_columns(connection, table):
            return {c["name"] for c in inspect(connection).get_columns(table)}

        # --- users table ---
        user_cols = await conn.run_sync(lambda c: get_columns(c, "users"))

        for col, ddl in [
            ("telegram_last_name", "ALTER TABLE users ADD COLUMN telegram_last_name VARCHAR"),
            ("telegram_username",  "ALTER TABLE users ADD COLUMN telegram_username VARCHAR"),
        ]:
            if col not in user_cols:
                await conn.execute(text(ddl))
                logger.info(f"✅ Added users.{col}")
            else:
                logger.info(f"✓ users.{col} already exists")

        # --- poll_votes table ---
        vote_cols = await conn.run_sync(lambda c: get_columns(c, "poll_votes"))

        for col, ddl in [
            ("user_last_name",   "ALTER TABLE poll_votes ADD COLUMN user_last_name VARCHAR"),
            ("user_tg_username", "ALTER TABLE poll_votes ADD COLUMN user_tg_username VARCHAR"),
        ]:
            if col not in vote_cols:
                await conn.execute(text(ddl))
                logger.info(f"✅ Added poll_votes.{col}")
            else:
                logger.info(f"✓ poll_votes.{col} already exists")

    await engine.dispose()
    logger.info("✅ Migration complete!")


if __name__ == "__main__":
    asyncio.run(migrate())
