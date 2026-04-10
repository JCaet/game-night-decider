import contextlib
import logging
import os
from collections.abc import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.core.models import Base

logger = logging.getLogger(__name__)

# Default to local SQLite if no DATABASE_URL is present
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///game_night.db")

# If using Postgres on Cloud Run (which might start with 'postgres://'),
# SQLAlchemy requires 'postgresql+asyncpg://'
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)
elif (
    DATABASE_URL.startswith("postgresql://")
    and "asyncpg" not in DATABASE_URL
    and "+" not in DATABASE_URL.split("://")[0]
):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def _run_migrations(conn) -> None:  # type: ignore[no-untyped-def]
    """Add missing columns for schema evolution (runs inside engine.begin())."""
    migrations: list[str] = [
        # collection
        "ALTER TABLE collection ADD COLUMN is_manual_player_override BOOLEAN DEFAULT 0",
        # users
        "ALTER TABLE users ADD COLUMN telegram_last_name VARCHAR",
        "ALTER TABLE users ADD COLUMN telegram_username VARCHAR",
        # sessions
        "ALTER TABLE sessions ADD COLUMN poll_type INTEGER DEFAULT 0",
        "ALTER TABLE sessions ADD COLUMN message_id INTEGER",
        "ALTER TABLE sessions ADD COLUMN hide_voters BOOLEAN DEFAULT 0",
        "ALTER TABLE sessions ADD COLUMN vote_limit INTEGER DEFAULT -1",
        "ALTER TABLE sessions ADD COLUMN shuffle_options BOOLEAN DEFAULT 0",
        "ALTER TABLE sessions ADD COLUMN hide_results BOOLEAN DEFAULT 0",
        "ALTER TABLE sessions ADD COLUMN allow_adding_options BOOLEAN DEFAULT 0",
        # poll_votes
        "ALTER TABLE poll_votes ADD COLUMN vote_type VARCHAR DEFAULT 'game'",
        "ALTER TABLE poll_votes ADD COLUMN game_id BIGINT",
        "ALTER TABLE poll_votes ADD COLUMN category_level INTEGER",
        "ALTER TABLE poll_votes ADD COLUMN user_last_name VARCHAR",
        "ALTER TABLE poll_votes ADD COLUMN user_tg_username VARCHAR",
        "ALTER TABLE poll_votes ADD COLUMN version INTEGER DEFAULT 1 NOT NULL",
    ]
    for stmt in migrations:
        with contextlib.suppress(Exception):
            conn.execute(text(stmt))


async def init_db() -> None:
    """Initialize the database (create tables)."""
    async with engine.begin() as conn:
        # In production with migrations (Alembic), we wouldn't do this.
        # But for this simple bot, creating tables if missing is fine.
        logger.info(f"Creating tables: {Base.metadata.tables.keys()}")
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_run_migrations)
    logger.info(f"Database initialized with URL: {DATABASE_URL}")


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Dependency for getting DB session."""
    async with AsyncSessionLocal() as session:
        yield session
