"""Migration script to add expansion support.

This script:
1. Adds effective_max_players and effective_complexity columns to collection
2. Creates the expansions table
3. Creates the user_expansions table

Run with: uv run python scripts/migrate_expansions.py
"""

import sqlite3
import sys
from pathlib import Path


def migrate_database(db_path: str = "game_night.db") -> None:
    """Add expansion support to the database."""
    if not Path(db_path).exists():
        print(f"Database {db_path} not found. Nothing to migrate.")
        return

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    try:
        # Check if migration is needed
        cursor.execute("PRAGMA table_info(collection)")
        columns = {row[1] for row in cursor.fetchall()}

        if "effective_max_players" in columns:
            print("Migration already complete (effective_max_players column exists).")
        else:
            print("Adding effective columns to collection table...")
            cursor.execute(
                "ALTER TABLE collection ADD COLUMN effective_max_players INTEGER"
            )
            cursor.execute(
                "ALTER TABLE collection ADD COLUMN effective_complexity REAL"
            )
            print("  Added effective_max_players and effective_complexity columns.")

        # Check if expansions table exists
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='expansions'"
        )
        if cursor.fetchone():
            print("Expansions table already exists.")
        else:
            print("Creating expansions table...")
            cursor.execute("""
                CREATE TABLE expansions (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    base_game_id INTEGER NOT NULL,
                    new_max_players INTEGER,
                    complexity_delta REAL,
                    FOREIGN KEY (base_game_id) REFERENCES games(id)
                )
            """)
            cursor.execute("CREATE INDEX ix_expansions_name ON expansions(name)")
            cursor.execute(
                "CREATE INDEX ix_expansions_base_game ON expansions(base_game_id)"
            )
            print("  Created expansions table with indexes.")

        # Check if user_expansions table exists
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='user_expansions'"
        )
        if cursor.fetchone():
            print("User_expansions table already exists.")
        else:
            print("Creating user_expansions table...")
            cursor.execute("""
                CREATE TABLE user_expansions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    expansion_id INTEGER NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(telegram_id),
                    FOREIGN KEY (expansion_id) REFERENCES expansions(id),
                    UNIQUE (user_id, expansion_id)
                )
            """)
            print("  Created user_expansions table.")

        conn.commit()
        print("\nMigration complete!")

        # Verify tables
        cursor.execute("PRAGMA table_info(collection)")
        cols = [row[1] for row in cursor.fetchall()]
        print(f"\nCollection columns: {cols}")

        cursor.execute("SELECT COUNT(*) FROM expansions")
        exp_count = cursor.fetchone()[0]
        print(f"Expansions table: {exp_count} records")

        cursor.execute("SELECT COUNT(*) FROM user_expansions")
        ue_count = cursor.fetchone()[0]
        print(f"User_expansions table: {ue_count} records")

    except Exception as e:
        conn.rollback()
        print(f"Migration failed: {e}")
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    db = sys.argv[1] if len(sys.argv) > 1 else "game_night.db"
    migrate_database(db)
