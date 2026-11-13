"""Guests are removed when their game night ends, and only then.

These run with foreign keys enforced (see conftest), as Postgres does: a
cleanup that removes a user still referenced elsewhere fails the whole
transaction, which is what took the bot down after 2.1.
"""

import pytest
from sqlalchemy import select

from src.bot.handlers import cancel_night, start_night
from src.core import db
from src.core.models import Collection, Game, Session, SessionPlayer, User

CHAT_A = 12345  # the chat the mock update comes from
CHAT_B = 67890  # another group with its own game night


async def _seat_guest(chat_id: int, guest_id: int, name: str, *, with_game: bool = False) -> None:
    async with db.AsyncSessionLocal() as session:
        if await session.get(Session, chat_id) is None:
            session.add(Session(chat_id=chat_id, is_active=True))
            await session.flush()
        session.add(User(telegram_id=guest_id, telegram_name=name, is_guest=True))
        await session.flush()
        session.add(SessionPlayer(session_id=chat_id, user_id=guest_id))
        if with_game:
            if await session.get(Game, -1) is None:
                session.add(
                    Game(
                        id=-1,
                        name="Guest Game",
                        min_players=1,
                        max_players=6,
                        playing_time=60,
                        complexity=2.0,
                    )
                )
                await session.flush()
            session.add(Collection(user_id=guest_id, game_id=-1))
        await session.commit()


async def _user_ids() -> set[int]:
    async with db.AsyncSessionLocal() as session:
        return set((await session.execute(select(User.telegram_id))).scalars())


@pytest.mark.asyncio
async def test_cancel_keeps_guests_seated_in_another_chat(mock_update, mock_context):
    """Ending one chat's night must not touch a guest playing in another chat."""
    await _seat_guest(CHAT_A, -1001, "Ana")
    await _seat_guest(CHAT_B, -2002, "Bruno")

    await cancel_night(mock_update, mock_context)

    assert await _user_ids() == {-2002}
    async with db.AsyncSessionLocal() as session:
        seated = (await session.execute(select(SessionPlayer.user_id))).scalars().all()
    assert seated == [-2002]


@pytest.mark.asyncio
async def test_cancel_removes_guest_who_has_games(mock_update, mock_context):
    """A guest's games go with them; the 2.1 guest flow always asks for some."""
    await _seat_guest(CHAT_A, -1001, "Ana", with_game=True)

    await cancel_night(mock_update, mock_context)

    assert await _user_ids() == set()
    async with db.AsyncSessionLocal() as session:
        assert (await session.execute(select(Collection))).first() is None
        # The game itself is shared and stays.
        assert await session.get(Game, -1) is not None


@pytest.mark.asyncio
async def test_new_night_keeps_other_chats_guests_and_clears_own(mock_update, mock_context):
    """Starting fresh in one chat clears its guests, games and all, and no one else's."""
    async with db.AsyncSessionLocal() as session:
        # An inactive night with no players goes straight to a fresh start.
        session.add(Session(chat_id=CHAT_A, is_active=False))
        session.add(User(telegram_id=-1001, telegram_name="Leftover", is_guest=True))
        await session.commit()
    await _seat_guest(CHAT_B, -2002, "Bruno", with_game=True)

    await start_night(mock_update, mock_context)

    assert await _user_ids() == {-2002}
    async with db.AsyncSessionLocal() as session:
        sess = await session.get(Session, CHAT_A)
        assert sess.is_active is True
        owners = (await session.execute(select(Collection.user_id))).scalars().all()
    assert owners == [-2002]
