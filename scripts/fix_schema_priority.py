import asyncio
import os

import aiosqlite

DB_PATH = "game_night.db"


async def fix_schema():
    if not os.path.exists(DB_PATH):
        print(f"Database {DB_PATH} not found.")
        return

    print(f"Connecting to {DB_PATH}...")
    async with aiosqlite.connect(DB_PATH) as db:
        # Check if column exists
        async with db.execute("PRAGMA table_info(collection)") as cursor:
            columns = await cursor.fetchall()
            column_names = [col[1] for col in columns]

        if "is_priority" in column_names:
            print("Column 'is_priority' already exists. No action needed.")
        else:
            print("Column 'is_priority' missing. Adding it...")
            try:
                await db.execute("ALTER TABLE collection ADD COLUMN is_priority BOOLEAN DEFAULT 0")
                await db.commit()
                print("Successfully added 'is_priority' column.")
            except Exception as e:
                print(f"Error adding column: {e}")


if __name__ == "__main__":
    asyncio.run(fix_schema())
