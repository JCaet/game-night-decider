"""Refresh community_unplayable_counts for all games with the field unpopulated.

Hits BGG /thing once per game and writes the parsed CSV (or empty string if the
poll exists but no count met the blocklist threshold) back to the row. Safe to
re-run; only games where the field is NULL are touched.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import inspect, select

from src.core import db
from src.core.bgg import BGGClient
from src.core.models import Game

REQUIRED_COLUMN = "community_unplayable_counts"


async def _ensure_schema_up_to_date() -> None:
    async with db.engine.connect() as conn:
        columns = await conn.run_sync(lambda sync_conn: inspect(sync_conn).get_columns("games"))
    if not any(c["name"] == REQUIRED_COLUMN for c in columns):
        sys.exit(
            f"Schema is out of date: games.{REQUIRED_COLUMN} is missing. "
            f"Run `uv run python -m alembic upgrade head` first."
        )


async def refresh_community_player_counts() -> None:
    await _ensure_schema_up_to_date()

    bgg = BGGClient()

    async with db.AsyncSessionLocal() as session:
        result = await session.execute(
            select(Game).where(Game.community_unplayable_counts.is_(None), Game.id > 0)
        )
        games = result.scalars().all()

        print(f"Found {len(games)} games with no community_unplayable_counts")

        updated = 0
        blocked_total = 0
        for i, game in enumerate(games):
            print(f"[{i + 1}/{len(games)}] {game.name} (ID: {game.id})")
            try:
                details = await bgg.get_game_details(game.id)
            except Exception as e:
                print(f"  -> Error: {e}")
                await asyncio.sleep(0.5)
                continue

            if details is None:
                print("  -> No details returned")
                await asyncio.sleep(0.5)
                continue

            value = details.community_unplayable_counts
            # Persist empty string to mark "parsed, nothing blocked" so we don't retry.
            game.community_unplayable_counts = value if value is not None else ""
            updated += 1
            if value:
                blocked_total += 1
                print(f"  -> Blocked counts: {value}")
            else:
                print("  -> No counts blocked")

            await asyncio.sleep(0.5)

        await session.commit()
        print("\n=== DONE ===")
        print(f"Updated {updated}/{len(games)} games ({blocked_total} with at least one block)")


if __name__ == "__main__":
    asyncio.run(refresh_community_player_counts())
