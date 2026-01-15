"""Migration script to convert boolean columns to single state integer.

This script:
1. Adds the new 'state' column
2. Migrates existing data based on is_priority and is_excluded
3. Drops the old columns

Run with: uv run python scripts/migrate_game_state.py
"""

import sqlite3
import sys
from pathlib import Path


def migrate_database(db_path: str = "game_night.db") -> None:
    """Migrate the collection table to use single state column."""
    if not Path(db_path).exists():
        print(f"Database {db_path} not found. Nothing to migrate.")
        return

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    try:
        # Check if migration is needed
        cursor.execute("PRAGMA table_info(collection)")
        columns = {row[1] for row in cursor.fetchall()}

        if "state" in columns:
            print("Migration already complete (state column exists).")
            return

        if "is_excluded" not in columns:
            print("Old columns not found. Database may be in unexpected state.")
            return

        print("Starting migration...")

        # Step 1: Add state column
        print("  Adding 'state' column...")
        cursor.execute("ALTER TABLE collection ADD COLUMN state INTEGER DEFAULT 0")

        # Step 2: Migrate data
        # Priority (starred) games get state=1
        # Excluded games get state=2
        # Note: is_priority takes precedence (a priority game can't be excluded)
        print("  Migrating data...")
        cursor.execute("UPDATE collection SET state = 1 WHERE is_priority = 1")
        cursor.execute("UPDATE collection SET state = 2 WHERE is_excluded = 1 AND is_priority = 0")

        # Step 3: Drop old columns (SQLite requires table recreation)
        print("  Recreating table without old columns...")

        # Create new table
        cursor.execute("""
            CREATE TABLE collection_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                game_id INTEGER NOT NULL,
                state INTEGER DEFAULT 0,
                FOREIGN KEY (user_id) REFERENCES users(telegram_id),
                FOREIGN KEY (game_id) REFERENCES games(id),
                UNIQUE (user_id, game_id)
            )
        """)

        # Copy data
        cursor.execute("""
            INSERT INTO collection_new (id, user_id, game_id, state)
            SELECT id, user_id, game_id, state FROM collection
        """)

        # Drop old table
        cursor.execute("DROP TABLE collection")

        # Rename new table
        cursor.execute("ALTER TABLE collection_new RENAME TO collection")

        # Recreate index
        cursor.execute("CREATE INDEX IF NOT EXISTS ix_collection_user_id ON collection(user_id)")

        conn.commit()
        print("Migration complete!")

        # Verify
        cursor.execute("SELECT COUNT(*) FROM collection WHERE state = 0")
        included = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM collection WHERE state = 1")
        starred = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM collection WHERE state = 2")
        excluded = cursor.fetchone()[0]

        print(f"\nFinal counts: {included} included, {starred} starred, {excluded} excluded")

    except Exception as e:
        conn.rollback()
        print(f"Migration failed: {e}")
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    db = sys.argv[1] if len(sys.argv) > 1 else "game_night.db"
    migrate_database(db)
