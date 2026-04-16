import logging
import os
from collections.abc import AsyncGenerator
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.core.models import Base

logger = logging.getLogger(__name__)


def resolve_database_url(url: str | None = None) -> str:
    raw = url or os.getenv("DATABASE_URL", "sqlite+aiosqlite:///game_night.db")
    if raw.startswith("postgres://"):
        raw = raw.replace("postgres://", "postgresql+asyncpg://", 1)
    elif raw.startswith("postgresql://") and "asyncpg" not in raw and "+" not in raw.split("://")[0]:
        raw = raw.replace("postgresql://", "postgresql+asyncpg://", 1)
    # Rewrite libpq-only query params that asyncpg doesn't understand, so
    # Neon-style URLs copied from their dashboard work unchanged:
    #   - sslmode=X -> ssl=X (same values accepted)
    #   - channel_binding=X: dropped; no asyncpg equivalent, TLS still enforced
    #     by ssl=require
    if "+asyncpg" in raw:
        parts = urlsplit(raw)
        if parts.query:
            params = [
                ("ssl", v) if k == "sslmode" else (k, v)
                for k, v in parse_qsl(parts.query, keep_blank_values=True)
                if k != "channel_binding"
            ]
            raw = urlunsplit(parts._replace(query=urlencode(params)))
    return raw


DATABASE_URL = resolve_database_url()

engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def run_migrations() -> None:
    from alembic.config import Config

    from alembic import command

    cfg = Config("alembic.ini")
    command.upgrade(cfg, "head")
    logger.info("Database migrated (URL: %s)", DATABASE_URL)


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database initialized with URL: %s", DATABASE_URL)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        yield session
