import os
import sqlite3

DB_PATH = "game_night.db"


def migrate_db():
    if not os.path.exists(DB_PATH):
        print(f"Database {DB_PATH} not found. Nothing to migrate.")
        return

    print("Checking database schema...")
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Check users table
    cursor.execute("PRAGMA table_info(users)")
    columns = [row[1] for row in cursor.fetchall()]
    print(f"Current 'users' columns: {columns}")

    if "is_guest" not in columns:
        print("Adding 'is_guest' column to 'users'...")
        try:
            cursor.execute("ALTER TABLE users ADD COLUMN is_guest BOOLEAN DEFAULT 0")
        except sqlite3.OperationalError as e:
            print(f"Error adding is_guest: {e}")

    if "added_by_user_id" not in columns:
        print("Adding 'added_by_user_id' column to 'users'...")
        try:
            cursor.execute("ALTER TABLE users ADD COLUMN added_by_user_id INTEGER")
        except sqlite3.OperationalError as e:
            print(f"Error adding added_by_user_id: {e}")

    # Check collection table for 'is_new' (just in case)
    cursor.execute("PRAGMA table_info(collection)")
    col_columns = [row[1] for row in cursor.fetchall()]
    print(f"Current 'collection' columns: {col_columns}")

    if "is_new" not in col_columns:
        print("Adding 'is_new' column to 'collection'...")
        try:
            cursor.execute("ALTER TABLE collection ADD COLUMN is_new BOOLEAN DEFAULT 0")
        except sqlite3.OperationalError as e:
            print(f"Error adding is_new: {e}")

    conn.commit()
    conn.close()
    print("Migration check complete.")


if __name__ == "__main__":
    migrate_db()
