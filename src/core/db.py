import logging
import os
from collections.abc import AsyncGenerator

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


async def init_db() -> None:
    """Initialize the database (create tables)."""
    async with engine.begin() as conn:
        # In production with migrations (Alembic), we wouldn't do this.
        # But for this simple bot, creating tables if missing is fine.
        logger.info(f"Creating tables: {Base.metadata.tables.keys()}")
        await conn.run_sync(Base.metadata.create_all)
    logger.info(f"Database initialized with URL: {DATABASE_URL}")


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Dependency for getting DB session."""
    async with AsyncSessionLocal() as session:
        yield session
