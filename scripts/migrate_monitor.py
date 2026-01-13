import asyncio

from src.core.db import engine
from src.core.models import Base


async def migrate():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print("Database tables created.")


if __name__ == "__main__":
    asyncio.run(migrate())
