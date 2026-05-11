"""Regression tests for issue #45 — expansion-driven player count sync.

Covers the full lifecycle the original feature missed:
- expansion bumps Collection.effective_max_players on first sync
- removing the expansion from BGG clears the bump on next sync
- multiple expansions for the same base game take the max
- removing the highest-bumping expansion falls back to the runner-up
- manual /manage overrides survive sync regardless of expansion state
- a re-fetch that returns None for new_max_players doesn't clobber a
  previously known good value
- a BGG fetch failure (None return) leaves existing expansion state alone
"""

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from src.bot.handlers import set_bgg
from src.core import db
from src.core.models import (
    Collection,
    Expansion,
    Game,
    User,
    UserExpansion,
)


def _catan() -> Game:
    """Catan-shaped base game: 3-4 players."""
    return Game(
        id=13,
        name="Catan",
        min_players=3,
        max_players=4,
        playing_time=60,
        complexity=2.32,
    )


def _expansion_info(*, exp_id: int, name: str, base_id: int, new_max: int | None) -> dict:
    """Mirror BGGClient.get_expansion_info return shape."""
    return {
        "id": exp_id,
        "name": name,
        "base_game_id": base_id,
        "new_max_players": new_max,
        "complexity": None,
    }


async def _seed_user_with_catan(user_id: int = 111) -> None:
    """User has Catan in their collection. No expansions yet."""
    async with db.AsyncSessionLocal() as session:
        session.add(User(telegram_id=user_id, telegram_name="TestUser", bgg_username="testuser"))
        session.add(_catan())
        await session.flush()
        session.add(Collection(user_id=user_id, game_id=13))
        await session.commit()


async def _get_collection(user_id: int, game_id: int) -> Collection | None:
    async with db.AsyncSessionLocal() as session:
        stmt = select(Collection).where(
            Collection.user_id == user_id, Collection.game_id == game_id
        )
        return (await session.execute(stmt)).scalar_one_or_none()


@pytest.mark.asyncio
async def test_expansion_bumps_effective_max_players(mock_update, mock_context):
    """First sync with a 5-6 Player Extension bumps Catan's effective max to 6."""
    await _seed_user_with_catan()
    mock_context.args = ["testuser"]

    with patch("src.bot.handlers.BGGClient") as MockBGG:
        bgg = MockBGG.return_value
        bgg.fetch_collection = AsyncMock(return_value=[_catan()])
        bgg.fetch_expansions = AsyncMock(return_value=[{"id": 926, "name": "Catan: 5-6 Player"}])
        bgg.get_expansion_info = AsyncMock(
            return_value=_expansion_info(exp_id=926, name="5-6P", base_id=13, new_max=6)
        )
        await set_bgg(mock_update, mock_context)

    col = await _get_collection(111, 13)
    assert col is not None
    assert col.effective_max_players == 6
    assert col.is_manual_player_override is False


@pytest.mark.asyncio
async def test_expansion_removed_clears_effective_max_players(mock_update, mock_context):
    """Issue #45: when an expansion is sold/removed from BGG, the bump must clear."""
    await _seed_user_with_catan()
    mock_context.args = ["testuser"]

    # First sync — user owns the 5-6 player expansion.
    with patch("src.bot.handlers.BGGClient") as MockBGG:
        bgg = MockBGG.return_value
        bgg.fetch_collection = AsyncMock(return_value=[_catan()])
        bgg.fetch_expansions = AsyncMock(return_value=[{"id": 926, "name": "5-6P"}])
        bgg.get_expansion_info = AsyncMock(
            return_value=_expansion_info(exp_id=926, name="5-6P", base_id=13, new_max=6)
        )
        await set_bgg(mock_update, mock_context)

    col = await _get_collection(111, 13)
    assert col is not None and col.effective_max_players == 6, "precondition: bump applied"

    # Second sync — BGG now returns no expansions (user sold it).
    with patch("src.bot.handlers.BGGClient") as MockBGG:
        bgg = MockBGG.return_value
        bgg.fetch_collection = AsyncMock(return_value=[_catan()])
        bgg.fetch_expansions = AsyncMock(return_value=[])
        bgg.get_expansion_info = AsyncMock(return_value=None)
        await set_bgg(mock_update, mock_context)

    col = await _get_collection(111, 13)
    assert col is not None
    assert col.effective_max_players is None, "bump must clear when expansion no longer owned"

    # UserExpansion row must also be pruned.
    async with db.AsyncSessionLocal() as session:
        ue = (
            (await session.execute(select(UserExpansion).where(UserExpansion.user_id == 111)))
            .scalars()
            .all()
        )
        assert ue == []


@pytest.mark.asyncio
async def test_multiple_expansions_take_max(mock_update, mock_context):
    """Two expansions for the same base game — effective_max takes the maximum."""
    await _seed_user_with_catan()
    mock_context.args = ["testuser"]

    info_by_id = {
        926: _expansion_info(exp_id=926, name="5-6P", base_id=13, new_max=6),
        927: _expansion_info(exp_id=927, name="7-8P Homebrew", base_id=13, new_max=8),
    }

    with patch("src.bot.handlers.BGGClient") as MockBGG:
        bgg = MockBGG.return_value
        bgg.fetch_collection = AsyncMock(return_value=[_catan()])
        bgg.fetch_expansions = AsyncMock(
            return_value=[{"id": 926, "name": "5-6P"}, {"id": 927, "name": "7-8P"}]
        )
        bgg.get_expansion_info = AsyncMock(side_effect=lambda eid: info_by_id[eid])
        await set_bgg(mock_update, mock_context)

    col = await _get_collection(111, 13)
    assert col is not None
    assert col.effective_max_players == 8


@pytest.mark.asyncio
async def test_removing_top_expansion_falls_back_to_runner_up(mock_update, mock_context):
    """Owning two expansions then losing the higher one — effective_max drops to the
    second-highest, not stays at the historical peak."""
    await _seed_user_with_catan()
    mock_context.args = ["testuser"]

    info_by_id = {
        926: _expansion_info(exp_id=926, name="5-6P", base_id=13, new_max=6),
        927: _expansion_info(exp_id=927, name="7-8P", base_id=13, new_max=8),
    }

    # Sync 1: own both.
    with patch("src.bot.handlers.BGGClient") as MockBGG:
        bgg = MockBGG.return_value
        bgg.fetch_collection = AsyncMock(return_value=[_catan()])
        bgg.fetch_expansions = AsyncMock(
            return_value=[{"id": 926, "name": "5-6P"}, {"id": 927, "name": "7-8P"}]
        )
        bgg.get_expansion_info = AsyncMock(side_effect=lambda eid: info_by_id[eid])
        await set_bgg(mock_update, mock_context)

    col = await _get_collection(111, 13)
    assert col is not None and col.effective_max_players == 8, "precondition: higher bump wins"

    # Sync 2: sold the 7-8P one, only 5-6P remains.
    with patch("src.bot.handlers.BGGClient") as MockBGG:
        bgg = MockBGG.return_value
        bgg.fetch_collection = AsyncMock(return_value=[_catan()])
        bgg.fetch_expansions = AsyncMock(return_value=[{"id": 926, "name": "5-6P"}])
        bgg.get_expansion_info = AsyncMock(side_effect=lambda eid: info_by_id[eid])
        await set_bgg(mock_update, mock_context)

    col = await _get_collection(111, 13)
    assert col is not None
    assert col.effective_max_players == 6, "must drop to runner-up, not stay at 8"


@pytest.mark.asyncio
async def test_manual_override_preserved_through_sync(mock_update, mock_context):
    """A manual /manage override must never be touched by expansion sync, regardless
    of whether expansion data would push it up or clear it down."""
    user_id = 111
    async with db.AsyncSessionLocal() as session:
        session.add(User(telegram_id=user_id, telegram_name="TestUser", bgg_username="testuser"))
        session.add(_catan())
        await session.flush()
        session.add(
            Collection(
                user_id=user_id,
                game_id=13,
                effective_max_players=7,
                is_manual_player_override=True,
            )
        )
        await session.commit()

    mock_context.args = ["testuser"]

    # Sync with an 8-player expansion — manual override (7) must stay.
    with patch("src.bot.handlers.BGGClient") as MockBGG:
        bgg = MockBGG.return_value
        bgg.fetch_collection = AsyncMock(return_value=[_catan()])
        bgg.fetch_expansions = AsyncMock(return_value=[{"id": 927, "name": "7-8P"}])
        bgg.get_expansion_info = AsyncMock(
            return_value=_expansion_info(exp_id=927, name="7-8P", base_id=13, new_max=8)
        )
        await set_bgg(mock_update, mock_context)

    col = await _get_collection(111, 13)
    assert col is not None
    assert col.effective_max_players == 7
    assert col.is_manual_player_override is True

    # Sync without expansions — manual override must still survive.
    with patch("src.bot.handlers.BGGClient") as MockBGG:
        bgg = MockBGG.return_value
        bgg.fetch_collection = AsyncMock(return_value=[_catan()])
        bgg.fetch_expansions = AsyncMock(return_value=[])
        await set_bgg(mock_update, mock_context)

    col = await _get_collection(111, 13)
    assert col is not None
    assert col.effective_max_players == 7
    assert col.is_manual_player_override is True


@pytest.mark.asyncio
async def test_resync_does_not_clobber_known_max_with_none(mock_update, mock_context):
    """If a re-sync's get_expansion_info returns new_max_players=None (BGG sometimes
    omits the value transiently), don't overwrite the previously stored max."""
    await _seed_user_with_catan()
    mock_context.args = ["testuser"]

    # Sync 1: full info — Expansion.new_max_players=6 stored.
    with patch("src.bot.handlers.BGGClient") as MockBGG:
        bgg = MockBGG.return_value
        bgg.fetch_collection = AsyncMock(return_value=[_catan()])
        bgg.fetch_expansions = AsyncMock(return_value=[{"id": 926, "name": "5-6P"}])
        bgg.get_expansion_info = AsyncMock(
            return_value=_expansion_info(exp_id=926, name="5-6P", base_id=13, new_max=6)
        )
        await set_bgg(mock_update, mock_context)

    async with db.AsyncSessionLocal() as session:
        exp = await session.get(Expansion, 926)
        assert exp is not None and exp.new_max_players == 6, "precondition"

    # Sync 2: BGG returns None for new_max_players (parser couldn't read maxplayers).
    with patch("src.bot.handlers.BGGClient") as MockBGG:
        bgg = MockBGG.return_value
        bgg.fetch_collection = AsyncMock(return_value=[_catan()])
        bgg.fetch_expansions = AsyncMock(return_value=[{"id": 926, "name": "5-6P"}])
        bgg.get_expansion_info = AsyncMock(
            return_value=_expansion_info(exp_id=926, name="5-6P", base_id=13, new_max=None)
        )
        await set_bgg(mock_update, mock_context)

    async with db.AsyncSessionLocal() as session:
        exp = await session.get(Expansion, 926)
        assert exp is not None
        assert exp.new_max_players == 6, "must not clobber stored 6 with None"

    # Effective max should also still be 6 (driven by the preserved stored value).
    col = await _get_collection(111, 13)
    assert col is not None and col.effective_max_players == 6


@pytest.mark.asyncio
async def test_bgg_error_does_not_wipe_expansion_state(mock_update, mock_context):
    """fetch_expansions returns None on BGG outage — existing expansion-driven bumps
    and UserExpansion links must survive untouched until BGG comes back."""
    await _seed_user_with_catan()
    mock_context.args = ["testuser"]

    # Sync 1: bump applied.
    with patch("src.bot.handlers.BGGClient") as MockBGG:
        bgg = MockBGG.return_value
        bgg.fetch_collection = AsyncMock(return_value=[_catan()])
        bgg.fetch_expansions = AsyncMock(return_value=[{"id": 926, "name": "5-6P"}])
        bgg.get_expansion_info = AsyncMock(
            return_value=_expansion_info(exp_id=926, name="5-6P", base_id=13, new_max=6)
        )
        await set_bgg(mock_update, mock_context)

    col = await _get_collection(111, 13)
    assert col is not None and col.effective_max_players == 6, "precondition"

    # Sync 2: BGG expansion endpoint is down.
    with patch("src.bot.handlers.BGGClient") as MockBGG:
        bgg = MockBGG.return_value
        bgg.fetch_collection = AsyncMock(return_value=[_catan()])
        bgg.fetch_expansions = AsyncMock(return_value=None)
        await set_bgg(mock_update, mock_context)

    col = await _get_collection(111, 13)
    assert col is not None
    assert col.effective_max_players == 6, "outage must not wipe expansion-driven bump"

    async with db.AsyncSessionLocal() as session:
        ue = (
            (await session.execute(select(UserExpansion).where(UserExpansion.user_id == 111)))
            .scalars()
            .all()
        )
        assert len(ue) == 1, "UserExpansion row must survive a transient BGG outage"
