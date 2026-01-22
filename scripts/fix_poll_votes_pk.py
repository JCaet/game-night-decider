import asyncio
import os
import sys

# Add src to path
sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import text

from src.core.db import engine
from src.core.models import Base


async def migrate():
    print("Migrating poll_votes table to include game_id in primary key...")

    async with engine.begin() as conn:
        # Drop existing table
        print("Dropping poll_votes table...")
        await conn.execute(text("DROP TABLE IF EXISTS poll_votes"))

        # Recreate table using updated model definition
        print("Recreating poll_votes table...")
        await conn.run_sync(Base.metadata.create_all)

    print("Migration complete!")

if __name__ == "__main__":
    asyncio.run(migrate())
