
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
            # Check if column exists pragma
            await conn.execute(text("ALTER TABLE sessions ADD COLUMN message_id INTEGER"))
            print("Added message_id column.")
        except Exception as e:
            if "duplicate column" in str(e) or "no such table" in str(e):
                print(f"Skipping add column (exists or table missing): {e}")
            else:
                print(f"Error adding message_id: {e}")

if __name__ == "__main__":
    asyncio.run(migrate())
