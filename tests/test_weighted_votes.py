from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot.handlers import receive_poll_answer
from src.core import db
from src.core.models import (
    Collection,
    Game,
    GameNightPoll,
    GameState,
    Session,
    SessionPlayer,
    User,
)


@pytest.mark.asyncio
async def test_weighted_voting_logic(mock_update, mock_context):
    """Test that starred games get boosted score when weighted voting is ON."""
    chat_id = 98765
    poll_id = "poll_weighted_1"
    message_id = 888

    # Setup Data
    async with db.AsyncSessionLocal() as session:
        # Create Users
        u1 = User(telegram_id=111, telegram_name="User1")
        u2 = User(telegram_id=222, telegram_name="User2")
        session.add_all([u1, u2])

        # Create Games
        g1 = Game(
            id=1, name="Normal Game", min_players=2, max_players=4, playing_time=60, complexity=2.0
        )
        g2 = Game(
            id=2,
            name="Starred Game",
            min_players=2,
            max_players=4,
            playing_time=60,
            complexity=2.0,
        )
        session.add_all([g1, g2])

        # Collections - Only User1 starred Game2
        c1 = Collection(user_id=111, game_id=1)
        c2 = Collection(user_id=111, game_id=2, state=GameState.STARRED)  # User1 starred Game2
        c3 = Collection(user_id=222, game_id=1)
        c4 = Collection(user_id=222, game_id=2)  # User2 did NOT star Game2
        session.add_all([c1, c2, c3, c4])

        # Session with weighted ON
        s = Session(chat_id=chat_id, is_active=True, settings_weighted=True)
        session.add(s)

        # Players
        sp1 = SessionPlayer(session_id=chat_id, user_id=111)
        sp2 = SessionPlayer(session_id=chat_id, user_id=222)
        session.add_all([sp1, sp2])

        await session.commit()

    # Setup Poll in DB
    async with db.AsyncSessionLocal() as session:
        poll = GameNightPoll(poll_id=poll_id, chat_id=chat_id, message_id=message_id)
        session.add(poll)
        await session.commit()

    # Scenario:
    # User1 votes "Normal Game" -> 1 vote
    # User2 votes "Starred Game" -> 1 vote + 0.5 (1 user starred) = 1.5 => WINS

    mock_update.poll_answer.poll_id = poll_id
    mock_update.poll_answer.user.id = 111
    mock_update.poll_answer.user.first_name = "User1"
    mock_update.poll_answer.option_ids = [0]  # Normal Game

    mock_context.bot.stop_poll = AsyncMock()

    await receive_poll_answer(mock_update, mock_context)

    # User 2 Votes for Starred Game
    mock_update.poll_answer.user.id = 222
    mock_update.poll_answer.user.first_name = "User2"
    mock_update.poll_answer.option_ids = [1]  # Starred Game

    mock_poll_data = MagicMock()
    opt_normal = MagicMock()
    opt_normal.text = "Normal Game"
    opt_normal.voter_count = 1

    opt_starred = MagicMock()
    opt_starred.text = "⭐ Starred Game"
    opt_starred.voter_count = 1

    mock_poll_data.options = [opt_normal, opt_starred]
    mock_context.bot.stop_poll.return_value = mock_poll_data

    await receive_poll_answer(mock_update, mock_context)

    # Verify Winner
    mock_context.bot.send_message.assert_called()
    args = mock_context.bot.send_message.call_args[1]
    text = args["text"]

    assert "Starred Game" in text
    assert "Normal Game" not in text
    assert "winner" in text.lower()
    assert "Weighted votes active" in text


@pytest.mark.asyncio
async def test_per_user_star_boost(mock_update, mock_context):
    """Test that star boost accumulates per user who starred the game."""
    chat_id = 77777
    poll_id = "poll_multi_star"
    message_id = 777

    # Setup Data: 3 Players, 2 Games
    # Game1: Normal Game (no stars)
    # Game2: Starred by ALL 3 players -> should get +1.5 boost (0.5 * 3)
    async with db.AsyncSessionLocal() as session:
        u1 = User(telegram_id=301, telegram_name="Player1")
        u2 = User(telegram_id=302, telegram_name="Player2")
        u3 = User(telegram_id=303, telegram_name="Player3")
        session.add_all([u1, u2, u3])

        g1 = Game(
            id=101,
            name="Normal Game",
            min_players=2,
            max_players=4,
            playing_time=60,
            complexity=2.0,
        )
        g2 = Game(
            id=102,
            name="Triple Star",
            min_players=2,
            max_players=4,
            playing_time=60,
            complexity=2.0,
        )
        session.add_all([g1, g2])

        # All 3 players star Game2
        c1 = Collection(user_id=301, game_id=101)
        c2 = Collection(user_id=301, game_id=102, state=GameState.STARRED)
        c3 = Collection(user_id=302, game_id=101)
        c4 = Collection(user_id=302, game_id=102, state=GameState.STARRED)
        c5 = Collection(user_id=303, game_id=101)
        c6 = Collection(user_id=303, game_id=102, state=GameState.STARRED)
        session.add_all([c1, c2, c3, c4, c5, c6])

        s = Session(chat_id=chat_id, is_active=True, settings_weighted=True)
        session.add(s)

        sp1 = SessionPlayer(session_id=chat_id, user_id=301)
        sp2 = SessionPlayer(session_id=chat_id, user_id=302)
        sp3 = SessionPlayer(session_id=chat_id, user_id=303)
        session.add_all([sp1, sp2, sp3])

        poll = GameNightPoll(poll_id=poll_id, chat_id=chat_id, message_id=message_id)
        session.add(poll)

        await session.commit()

    # Voting: 2 votes for Normal, 1 vote for Triple Star
    # Normal: 2 votes = 2.0
    # Triple Star: 1 vote + 1.5 boost (3 users starred) = 2.5 => WINS

    mock_update.poll_answer.poll_id = poll_id
    mock_context.bot.stop_poll = AsyncMock()

    # Player 1 votes Normal
    mock_update.poll_answer.user.id = 301
    mock_update.poll_answer.user.first_name = "Player1"
    mock_update.poll_answer.option_ids = [0]
    await receive_poll_answer(mock_update, mock_context)

    # Player 2 votes Normal
    mock_update.poll_answer.user.id = 302
    mock_update.poll_answer.user.first_name = "Player2"
    mock_update.poll_answer.option_ids = [0]
    await receive_poll_answer(mock_update, mock_context)

    # Player 3 votes Triple Star
    mock_update.poll_answer.user.id = 303
    mock_update.poll_answer.user.first_name = "Player3"
    mock_update.poll_answer.option_ids = [1]

    mock_poll_data = MagicMock()
    opt_normal = MagicMock()
    opt_normal.text = "Normal Game"
    opt_normal.voter_count = 2  # 2 votes

    opt_starred = MagicMock()
    opt_starred.text = "⭐ Triple Star"
    opt_starred.voter_count = 1  # 1 vote

    mock_poll_data.options = [opt_normal, opt_starred]
    mock_context.bot.stop_poll.return_value = mock_poll_data

    await receive_poll_answer(mock_update, mock_context)

    # Verify Winner: Triple Star should win with 1 + 1.5 = 2.5 vs 2.0
    mock_context.bot.send_message.assert_called()
    args = mock_context.bot.send_message.call_args[1]
    text = args["text"]

    assert "Triple Star" in text
    assert "Normal Game" not in text
    assert "winner" in text.lower()
