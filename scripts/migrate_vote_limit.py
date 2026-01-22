
import os
import sys

sys.path.append(os.getcwd())

import asyncio

from sqlalchemy import text

from src.core.db import engine


async def migrate():
    async with engine.begin() as conn:
        print("Migrating Session table...")
        try:
            # Add vote_limit column with default -1 (VoteLimit.AUTO)
            await conn.execute(
                text("ALTER TABLE sessions ADD COLUMN vote_limit INTEGER DEFAULT -1")
            )
            print("Added vote_limit column.")
        except Exception as e:
            if "duplicate column" in str(e).lower() or "already exists" in str(e).lower():
                print(f"Column already exists, skipping: {e}")
            else:
                print(f"Error adding vote_limit: {e}")


if __name__ == "__main__":
    asyncio.run(migrate())
