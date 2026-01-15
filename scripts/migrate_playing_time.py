"""Migration script to add min/max playing time columns to games table.

Run with: uv run python scripts/migrate_playing_time.py
"""

import sqlite3
import sys
from pathlib import Path


def migrate_database(db_path: str = "game_night.db") -> None:
    """Add min_playing_time and max_playing_time columns to games table."""
    if not Path(db_path).exists():
        print(f"Database {db_path} not found. Nothing to migrate.")
        return

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    try:
        # Check if migration is needed
        cursor.execute("PRAGMA table_info(games)")
        columns = {row[1] for row in cursor.fetchall()}

        if "min_playing_time" in columns:
            print("Migration already complete (min_playing_time column exists).")
            return

        print("Starting migration...")

        # Add new columns
        print("  Adding 'min_playing_time' column...")
        cursor.execute("ALTER TABLE games ADD COLUMN min_playing_time INTEGER")

        print("  Adding 'max_playing_time' column...")
        cursor.execute("ALTER TABLE games ADD COLUMN max_playing_time INTEGER")

        conn.commit()
        print("Migration complete!")

        # Verify
        cursor.execute("SELECT COUNT(*) FROM games")
        total = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM games WHERE min_playing_time IS NOT NULL")
        with_min = cursor.fetchone()[0]

        print(f"\nTotal games: {total}, with min_playing_time: {with_min}")
        print("Note: Existing games will have NULL values until BGG data is re-synced.")

    except Exception as e:
        conn.rollback()
        print(f"Migration failed: {e}")
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    db = sys.argv[1] if len(sys.argv) > 1 else "game_night.db"
    migrate_database(db)
