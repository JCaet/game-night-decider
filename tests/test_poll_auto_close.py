from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import func, select

from src.bot.handlers import create_poll, receive_poll_answer
from src.core import db
from src.core.models import (
    Collection,
    Game,
    GameNightPoll,
    PollType,
    PollVote,
    Session,
    SessionPlayer,
    User,
)


@pytest.mark.asyncio
async def test_poll_auto_close(mock_update, mock_context):
    """Test that poll auto-closes when all players have voted."""
    chat_id = 12345
    poll_id = "poll_123"
    message_id = 999

    # Setup: 2 Players, 2 Games (need 2+ for poll creation)
    async with db.AsyncSessionLocal() as session:
        # Create Users
        u1 = User(telegram_id=111, telegram_name="User1")
        u2 = User(telegram_id=222, telegram_name="User2")
        session.add_all([u1, u2])

        # Create Games (need at least 2 for a poll - Telegram requirement)
        g1 = Game(id=1, name="Catan", min_players=2, max_players=4, playing_time=60, complexity=2.0)
        g2 = Game(
            id=2,
            name="Ticket to Ride",
            min_players=2,
            max_players=5,
            playing_time=60,
            complexity=2.0,
        )
        session.add_all([g1, g2])

        # Create Collections (both users own both games)
        c1 = Collection(user_id=111, game_id=1)
        c2 = Collection(user_id=222, game_id=1)
        c3 = Collection(user_id=111, game_id=2)
        c4 = Collection(user_id=222, game_id=2)
        session.add_all([c1, c2, c3, c4])

        # Create Session (use native poll mode)
        s = Session(chat_id=chat_id, is_active=True, poll_type=PollType.NATIVE)
        session.add(s)

        # Add Players to Session
        sp1 = SessionPlayer(session_id=chat_id, user_id=111)
        sp2 = SessionPlayer(session_id=chat_id, user_id=222)
        session.add_all([sp1, sp2])

        await session.commit()

    # Mock Update for create_poll
    mock_update.effective_chat.id = chat_id

    # Mock send_poll return value - poll_id must be a string to match model
    mock_message = MagicMock()
    mock_message.poll = MagicMock()
    mock_message.poll.id = poll_id
    mock_message.message_id = message_id
    mock_context.bot.send_poll = AsyncMock(return_value=mock_message)

    # 1. Create Poll
    await create_poll(mock_update, mock_context)

    # Verify Poll saved to DB
    async with db.AsyncSessionLocal() as session:
        poll = await session.get(GameNightPoll, poll_id)
        assert poll is not None, f"Expected poll with id '{poll_id}' to be saved"
        assert poll.chat_id == chat_id
        assert poll.message_id == message_id

    # 2. Player 1 Votes
    # Reset mocks for next handler
    mock_update.poll_answer = MagicMock()
    mock_update.poll_answer.poll_id = poll_id
    mock_update.poll_answer.user.id = 111
    mock_update.poll_answer.user.first_name = "User1"
    mock_update.poll_answer.option_ids = [0]  # Voted for option 0

    # Prepare stop_poll mock (should NOT be called yet)
    mock_context.bot.stop_poll = AsyncMock()

    await receive_poll_answer(mock_update, mock_context)

    # Verify Vote saved
    async with db.AsyncSessionLocal() as session:
        voters = await session.scalar(
            select(func.count(PollVote.user_id)).where(PollVote.poll_id == poll_id)
        )
        assert voters == 1

    # Verify stop_poll NOT called
    mock_context.bot.stop_poll.assert_not_called()

    # 3. Player 2 Votes
    mock_update.poll_answer.user.id = 222
    mock_update.poll_answer.user.first_name = "User2"
    mock_update.poll_answer.option_ids = [0]

    # Mock stop_poll return value (needed for winner calculation)
    mock_poll_data = MagicMock()
    option0 = MagicMock()
    option0.text = "Catan"
    option0.voter_count = 2
    mock_poll_data.options = [option0]
    mock_context.bot.stop_poll.return_value = mock_poll_data

    await receive_poll_answer(mock_update, mock_context)

    # Verify Vote saved
    async with db.AsyncSessionLocal() as session:
        voters = await session.scalar(
            select(func.count(PollVote.user_id)).where(PollVote.poll_id == poll_id)
        )
        assert voters == 2

    # Verify stop_poll CALLED
    mock_context.bot.stop_poll.assert_called_once_with(chat_id=chat_id, message_id=message_id)

    # Verify Winner Announcement
    mock_context.bot.send_message.assert_called()
    args = mock_context.bot.send_message.call_args[1]
    assert "Winner" in args["text"] or "winner" in args["text"]
    assert "Catan" in args["text"]
