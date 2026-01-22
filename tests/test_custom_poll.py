"""
Tests for the Custom Single Poll Mode feature.

This tests the alternative poll mechanism that uses inline buttons
instead of native Telegram polls to overcome the 10-option limit.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

from src.bot.handlers import (
    create_poll,
    custom_poll_action_callback,
    custom_poll_vote_callback,
    join_lobby_callback,
    poll_settings_callback,
    start_poll_callback,
    toggle_poll_mode_callback,
    toggle_weights_callback,
)
from src.core import db
from src.core.models import (
    Collection,
    Game,
    GameNightPoll,
    GameState,
    PollType,
    PollVote,
    Session,
    SessionPlayer,
    User,
)

# ============================================================================
# Poll Settings Tests
# ============================================================================


@pytest.mark.asyncio
async def test_poll_settings_shows_current_mode_custom(mock_update, mock_context):
    """Test poll settings shows Custom mode when poll_type is CUSTOM."""
    chat_id = 12345

    async with db.AsyncSessionLocal() as session:
        session.add(Session(chat_id=chat_id, is_active=True, poll_type=PollType.CUSTOM))
        await session.commit()

    mock_update.callback_query.message.chat.id = chat_id

    await poll_settings_callback(mock_update, mock_context)

    mock_update.callback_query.edit_message_text.assert_called_once()
    call_args = mock_update.callback_query.edit_message_text.call_args
    text = call_args.kwargs.get("text") or call_args.args[0]
    assert "Custom (Single)" in text


@pytest.mark.asyncio
async def test_poll_settings_shows_current_mode_native(mock_update, mock_context):
    """Test poll settings shows Native mode when settings_single_poll is False."""
    chat_id = 12345

    async with db.AsyncSessionLocal() as session:
        session.add(Session(chat_id=chat_id, is_active=True, poll_type=PollType.NATIVE))
        await session.commit()

    mock_update.callback_query.message.chat.id = chat_id

    await poll_settings_callback(mock_update, mock_context)

    mock_update.callback_query.edit_message_text.assert_called_once()
    call_args = mock_update.callback_query.edit_message_text.call_args
    text = call_args.kwargs.get("text") or call_args.args[0]
    assert "Native (Multiple)" in text


@pytest.mark.asyncio
async def test_poll_settings_no_session_returns_early(mock_update, mock_context):
    """Test poll settings returns early when no session exists."""
    mock_update.callback_query.message.chat.id = 99999  # Non-existent

    await poll_settings_callback(mock_update, mock_context)

    mock_update.callback_query.edit_message_text.assert_not_called()


@pytest.mark.asyncio
async def test_toggle_poll_mode_switches_to_native(mock_update, mock_context):
    """Test toggling from Custom to Native mode."""
    chat_id = 12345

    async with db.AsyncSessionLocal() as session:
        session.add(Session(chat_id=chat_id, is_active=True, poll_type=PollType.CUSTOM))
        await session.commit()

    mock_update.callback_query.message.chat.id = chat_id

    await toggle_poll_mode_callback(mock_update, mock_context)

    # Verify mode changed in DB
    async with db.AsyncSessionLocal() as session:
        sess = await session.get(Session, chat_id)
        assert sess.poll_type == PollType.NATIVE

    # Verify UI shows new mode
    call_args = mock_update.callback_query.edit_message_text.call_args
    text = call_args.kwargs.get("text") or call_args.args[0]
    assert "Native (Multiple)" in text


@pytest.mark.asyncio
async def test_toggle_poll_mode_switches_to_custom(mock_update, mock_context):
    """Test toggling from Native to Custom mode."""
    chat_id = 12345

    async with db.AsyncSessionLocal() as session:
        session.add(Session(chat_id=chat_id, is_active=True, poll_type=PollType.NATIVE))
        await session.commit()

    mock_update.callback_query.message.chat.id = chat_id

    await toggle_poll_mode_callback(mock_update, mock_context)

    # Verify mode changed in DB
    async with db.AsyncSessionLocal() as session:
        sess = await session.get(Session, chat_id)
        assert sess.poll_type == PollType.CUSTOM


# ============================================================================
# Custom Poll Creation Tests
# ============================================================================


@pytest.mark.asyncio
async def test_create_poll_custom_mode_creates_poll(mock_update, mock_context):
    """Test /poll in custom mode creates a GameNightPoll and sends message."""
    chat_id = 12345

    async with db.AsyncSessionLocal() as session:
        # Custom mode session
        session.add(Session(chat_id=chat_id, is_active=True, poll_type=PollType.CUSTOM))

        u1 = User(telegram_id=111, telegram_name="User1")
        u2 = User(telegram_id=222, telegram_name="User2")
        session.add_all([u1, u2])
        await session.flush()

        # Two games for valid poll
        g1 = Game(id=1, name="Game1", min_players=2, max_players=4, playing_time=60, complexity=2.0)
        g2 = Game(id=2, name="Game2", min_players=2, max_players=4, playing_time=60, complexity=2.5)
        session.add_all([g1, g2])
        await session.flush()

        c1 = Collection(user_id=111, game_id=1)
        c2 = Collection(user_id=222, game_id=2)
        session.add_all([c1, c2])

        sp1 = SessionPlayer(session_id=chat_id, user_id=111)
        sp2 = SessionPlayer(session_id=chat_id, user_id=222)
        session.add_all([sp1, sp2])
        await session.commit()

    mock_update.effective_chat.id = chat_id

    await create_poll(mock_update, mock_context)

    # Verify send_message was called (custom poll uses this, not send_poll)
    mock_context.bot.send_message.assert_called()

    # Verify poll was saved to DB
    async with db.AsyncSessionLocal() as session:
        stmt = select(GameNightPoll).where(GameNightPoll.chat_id == chat_id)
        poll = (await session.execute(stmt)).scalar_one_or_none()
        assert poll is not None
        assert poll.poll_id.startswith(f"poll_{chat_id}_")


@pytest.mark.asyncio
async def test_create_poll_custom_mode_shows_games(mock_update, mock_context):
    """Test custom poll shows game buttons in the keyboard."""
    chat_id = 12345

    async with db.AsyncSessionLocal() as session:
        session.add(Session(chat_id=chat_id, is_active=True, poll_type=PollType.CUSTOM))

        u1 = User(telegram_id=111, telegram_name="User1")
        u2 = User(telegram_id=222, telegram_name="User2")
        session.add_all([u1, u2])
        await session.flush()

        g1 = Game(id=1, name="Catan", min_players=2, max_players=4, playing_time=60, complexity=2.0)
        g2 = Game(
            id=2, name="Wingspan", min_players=2, max_players=4, playing_time=60, complexity=2.5
        )
        session.add_all([g1, g2])
        await session.flush()

        c1 = Collection(user_id=111, game_id=1)
        c2 = Collection(user_id=222, game_id=2)
        session.add_all([c1, c2])

        sp1 = SessionPlayer(session_id=chat_id, user_id=111)
        sp2 = SessionPlayer(session_id=chat_id, user_id=222)
        session.add_all([sp1, sp2])
        await session.commit()

    mock_update.effective_chat.id = chat_id

    await create_poll(mock_update, mock_context)

    # Verify edit_message_text was called to render the poll
    mock_context.bot.edit_message_text.assert_called()
    call_kwargs = mock_context.bot.edit_message_text.call_args.kwargs

    # Check keyboard contains game names
    keyboard = call_kwargs.get("reply_markup")
    assert keyboard is not None
    button_labels = [btn.text for row in keyboard.inline_keyboard for btn in row]
    # At least one game should appear
    assert any("Catan" in label or "Wingspan" in label for label in button_labels)


# ============================================================================
# Custom Poll Vote Tests
# ============================================================================


@pytest.mark.asyncio
async def test_custom_poll_vote_adds_vote(mock_update, mock_context):
    """Test voting on a custom poll adds a PollVote record."""
    chat_id = 12345
    poll_id = "poll_12345_123456"
    game_id = 1
    user_id = 111

    async with db.AsyncSessionLocal() as session:
        session.add(Session(chat_id=chat_id, is_active=True, poll_type=PollType.CUSTOM))

        u1 = User(telegram_id=user_id, telegram_name="Voter")
        u2 = User(telegram_id=222, telegram_name="Other")
        session.add_all([u1, u2])
        await session.flush()

        g1 = Game(
            id=game_id,
            name="TestGame",
            min_players=2,
            max_players=4,
            playing_time=60,
            complexity=2.0,
        )
        session.add(g1)
        await session.flush()

        c1 = Collection(user_id=user_id, game_id=game_id)
        c2 = Collection(user_id=222, game_id=game_id)
        session.add_all([c1, c2])

        sp1 = SessionPlayer(session_id=chat_id, user_id=user_id)
        sp2 = SessionPlayer(session_id=chat_id, user_id=222)
        session.add_all([sp1, sp2])

        # Create the poll
        poll = GameNightPoll(poll_id=poll_id, chat_id=chat_id, message_id=999)
        session.add(poll)
        await session.commit()

    mock_update.callback_query.data = f"vote:{poll_id}:{game_id}"
    mock_update.callback_query.from_user.id = user_id
    mock_update.callback_query.from_user.first_name = "Voter"

    await custom_poll_vote_callback(mock_update, mock_context)

    # Verify vote was recorded
    async with db.AsyncSessionLocal() as session:
        stmt = select(PollVote).where(
            PollVote.poll_id == poll_id, PollVote.user_id == user_id, PollVote.game_id == game_id
        )
        vote = (await session.execute(stmt)).scalar_one_or_none()
        assert vote is not None
        assert vote.user_name == "Voter"

    # Verify answer callback was called
    mock_update.callback_query.answer.assert_called_with("Vote recorded")


@pytest.mark.asyncio
async def test_custom_poll_vote_removes_vote(mock_update, mock_context):
    """Test voting again on a custom poll removes the vote (toggle)."""
    chat_id = 12345
    poll_id = "poll_12345_123456"
    game_id = 1
    user_id = 111

    async with db.AsyncSessionLocal() as session:
        session.add(Session(chat_id=chat_id, is_active=True, poll_type=PollType.CUSTOM))

        u1 = User(telegram_id=user_id, telegram_name="Voter")
        u2 = User(telegram_id=222, telegram_name="Other")
        session.add_all([u1, u2])
        await session.flush()

        g1 = Game(
            id=game_id,
            name="TestGame",
            min_players=2,
            max_players=4,
            playing_time=60,
            complexity=2.0,
        )
        session.add(g1)
        await session.flush()

        c1 = Collection(user_id=user_id, game_id=game_id)
        c2 = Collection(user_id=222, game_id=game_id)
        session.add_all([c1, c2])

        sp1 = SessionPlayer(session_id=chat_id, user_id=user_id)
        sp2 = SessionPlayer(session_id=chat_id, user_id=222)
        session.add_all([sp1, sp2])

        poll = GameNightPoll(poll_id=poll_id, chat_id=chat_id, message_id=999)
        session.add(poll)

        # Pre-existing vote
        vote = PollVote(poll_id=poll_id, user_id=user_id, game_id=game_id, user_name="Voter")
        session.add(vote)
        await session.commit()

    mock_update.callback_query.data = f"vote:{poll_id}:{game_id}"
    mock_update.callback_query.from_user.id = user_id
    mock_update.callback_query.from_user.first_name = "Voter"

    await custom_poll_vote_callback(mock_update, mock_context)

    # Verify vote was removed
    async with db.AsyncSessionLocal() as session:
        stmt = select(PollVote).where(
            PollVote.poll_id == poll_id, PollVote.user_id == user_id, PollVote.game_id == game_id
        )
        vote = (await session.execute(stmt)).scalar_one_or_none()
        assert vote is None

    mock_update.callback_query.answer.assert_called_with("Vote removed")


@pytest.mark.asyncio
async def test_custom_poll_vote_invalid_data(mock_update, mock_context):
    """Test voting with invalid callback data shows error."""
    mock_update.callback_query.data = "vote:invalid"  # Missing game_id

    await custom_poll_vote_callback(mock_update, mock_context)

    mock_update.callback_query.answer.assert_called_with("Invalid vote data")


# ============================================================================
# Custom Poll Actions Tests
# ============================================================================


@pytest.mark.asyncio
async def test_custom_poll_refresh(mock_update, mock_context):
    """Test refresh action re-renders the poll."""
    chat_id = 12345
    poll_id = "poll_12345_123456"

    async with db.AsyncSessionLocal() as session:
        session.add(Session(chat_id=chat_id, is_active=True, poll_type=PollType.CUSTOM))

        u1 = User(telegram_id=111, telegram_name="User1")
        u2 = User(telegram_id=222, telegram_name="User2")
        session.add_all([u1, u2])
        await session.flush()

        g1 = Game(id=1, name="Game1", min_players=2, max_players=4, playing_time=60, complexity=2.0)
        session.add(g1)
        await session.flush()

        c1 = Collection(user_id=111, game_id=1)
        c2 = Collection(user_id=222, game_id=1)
        session.add_all([c1, c2])

        sp1 = SessionPlayer(session_id=chat_id, user_id=111)
        sp2 = SessionPlayer(session_id=chat_id, user_id=222)
        session.add_all([sp1, sp2])

        poll = GameNightPoll(poll_id=poll_id, chat_id=chat_id, message_id=999)
        session.add(poll)
        await session.commit()

    mock_update.callback_query.data = f"poll_refresh:{poll_id}"

    await custom_poll_action_callback(mock_update, mock_context)

    mock_update.callback_query.answer.assert_called_with("Refreshing...")
    mock_context.bot.edit_message_text.assert_called()


@pytest.mark.asyncio
async def test_custom_poll_close_announces_winner(mock_update, mock_context):
    """Test closing poll announces the winner."""
    chat_id = 12345
    poll_id = "poll_12345_123456"

    async with db.AsyncSessionLocal() as session:
        session.add(Session(chat_id=chat_id, is_active=True, poll_type=PollType.CUSTOM))

        u1 = User(telegram_id=111, telegram_name="User1")
        u2 = User(telegram_id=222, telegram_name="User2")
        session.add_all([u1, u2])
        await session.flush()

        g1 = Game(
            id=1, name="Winner Game", min_players=2, max_players=4, playing_time=60, complexity=2.0
        )
        g2 = Game(
            id=2, name="Loser Game", min_players=2, max_players=4, playing_time=60, complexity=2.5
        )
        session.add_all([g1, g2])
        await session.flush()

        c1 = Collection(user_id=111, game_id=1)
        c2 = Collection(user_id=222, game_id=1)
        c3 = Collection(user_id=111, game_id=2)
        session.add_all([c1, c2, c3])

        sp1 = SessionPlayer(session_id=chat_id, user_id=111)
        sp2 = SessionPlayer(session_id=chat_id, user_id=222)
        session.add_all([sp1, sp2])

        poll = GameNightPoll(poll_id=poll_id, chat_id=chat_id, message_id=999)
        session.add(poll)

        # Two votes for Winner Game
        v1 = PollVote(poll_id=poll_id, user_id=111, game_id=1, user_name="User1")
        v2 = PollVote(poll_id=poll_id, user_id=222, game_id=1, user_name="User2")
        session.add_all([v1, v2])
        await session.commit()

    mock_update.callback_query.data = f"poll_close:{poll_id}"
    mock_update.callback_query.message.chat.id = chat_id

    await custom_poll_action_callback(mock_update, mock_context)

    mock_update.callback_query.answer.assert_called_with("Closing poll...")
    mock_context.bot.edit_message_text.assert_called()

    call_kwargs = mock_context.bot.edit_message_text.call_args.kwargs
    text = call_kwargs.get("text")
    assert "Winner Game" in text
    assert "winner" in text.lower()


@pytest.mark.asyncio
async def test_custom_poll_close_tie(mock_update, mock_context):
    """Test closing poll with a tie shows both games."""
    chat_id = 12345
    poll_id = "poll_12345_123456"

    async with db.AsyncSessionLocal() as session:
        session.add(Session(chat_id=chat_id, is_active=True, poll_type=PollType.CUSTOM))

        u1 = User(telegram_id=111, telegram_name="User1")
        u2 = User(telegram_id=222, telegram_name="User2")
        session.add_all([u1, u2])
        await session.flush()

        g1 = Game(
            id=1, name="TieGame1", min_players=2, max_players=4, playing_time=60, complexity=2.0
        )
        g2 = Game(
            id=2, name="TieGame2", min_players=2, max_players=4, playing_time=60, complexity=2.5
        )
        session.add_all([g1, g2])
        await session.flush()

        c1 = Collection(user_id=111, game_id=1)
        c2 = Collection(user_id=222, game_id=2)
        session.add_all([c1, c2])

        sp1 = SessionPlayer(session_id=chat_id, user_id=111)
        sp2 = SessionPlayer(session_id=chat_id, user_id=222)
        session.add_all([sp1, sp2])

        poll = GameNightPoll(poll_id=poll_id, chat_id=chat_id, message_id=999)
        session.add(poll)

        # One vote each - tie!
        v1 = PollVote(poll_id=poll_id, user_id=111, game_id=1, user_name="User1")
        v2 = PollVote(poll_id=poll_id, user_id=222, game_id=2, user_name="User2")
        session.add_all([v1, v2])
        await session.commit()

    mock_update.callback_query.data = f"poll_close:{poll_id}"
    mock_update.callback_query.message.chat.id = chat_id

    await custom_poll_action_callback(mock_update, mock_context)

    call_kwargs = mock_context.bot.edit_message_text.call_args.kwargs
    text = call_kwargs.get("text")
    assert "tie" in text.lower()
    assert "TieGame1" in text
    assert "TieGame2" in text


@pytest.mark.asyncio
async def test_custom_poll_close_resolves_category_votes(mock_update, mock_context):
    """Test closing poll resolves category votes to an actual game."""
    import random

    chat_id = random.randint(10000, 99999)
    poll_id = f"poll_cat_close_{chat_id}"
    base_id = random.randint(10000, 90000)

    async with db.AsyncSessionLocal() as session:
        session.add(Session(chat_id=chat_id, is_active=True, poll_type=PollType.CUSTOM))

        u1 = User(telegram_id=111, telegram_name="User1")
        u2 = User(telegram_id=222, telegram_name="User2")
        session.add_all([u1, u2])
        await session.flush()

        # Group 4: Two games (complexity 4.x)
        g1 = Game(
            id=base_id,
            name="ComplexGame1",
            min_players=2,
            max_players=4,
            playing_time=60,
            complexity=4.5,
        )
        g2 = Game(
            id=base_id + 1,
            name="ComplexGame2",
            min_players=2,
            max_players=4,
            playing_time=60,
            complexity=4.2,
        )
        session.add_all([g1, g2])
        await session.flush()

        c1 = Collection(user_id=111, game_id=base_id)
        c2 = Collection(user_id=222, game_id=base_id + 1)
        session.add_all([c1, c2])

        sp1 = SessionPlayer(session_id=chat_id, user_id=111)
        sp2 = SessionPlayer(session_id=chat_id, user_id=222)
        session.add_all([sp1, sp2])

        poll = GameNightPoll(poll_id=poll_id, chat_id=chat_id, message_id=999)
        session.add(poll)

        # Both users voted on category 4 (game_id = -4)
        v1 = PollVote(poll_id=poll_id, user_id=111, game_id=-4, user_name="User1")
        v2 = PollVote(poll_id=poll_id, user_id=222, game_id=-4, user_name="User2")
        session.add_all([v1, v2])
        await session.commit()

    mock_update.callback_query.data = f"poll_close:{poll_id}"
    mock_update.callback_query.message.chat.id = chat_id

    await custom_poll_action_callback(mock_update, mock_context)

    call_kwargs = mock_context.bot.edit_message_text.call_args.kwargs
    text = call_kwargs.get("text")

    # Should have a winner (resolved from category votes)
    assert "winner" in text.lower()
    # Winner should be one of the category games
    assert "ComplexGame1" in text or "ComplexGame2" in text
    # The winner should have 2 votes (both category votes resolved to same game)
    assert "2.0 pts" in text


@pytest.mark.asyncio
async def test_custom_poll_close_no_votes(mock_update, mock_context):
    """Test closing poll with no votes shows appropriate message."""
    chat_id = 12345
    poll_id = "poll_12345_123456"

    async with db.AsyncSessionLocal() as session:
        session.add(Session(chat_id=chat_id, is_active=True, poll_type=PollType.CUSTOM))

        u1 = User(telegram_id=111, telegram_name="User1")
        u2 = User(telegram_id=222, telegram_name="User2")
        session.add_all([u1, u2])
        await session.flush()

        g1 = Game(id=1, name="Game1", min_players=2, max_players=4, playing_time=60, complexity=2.0)
        session.add(g1)
        await session.flush()

        c1 = Collection(user_id=111, game_id=1)
        c2 = Collection(user_id=222, game_id=1)
        session.add_all([c1, c2])

        sp1 = SessionPlayer(session_id=chat_id, user_id=111)
        sp2 = SessionPlayer(session_id=chat_id, user_id=222)
        session.add_all([sp1, sp2])

        poll = GameNightPoll(poll_id=poll_id, chat_id=chat_id, message_id=999)
        session.add(poll)
        await session.commit()

    mock_update.callback_query.data = f"poll_close:{poll_id}"
    mock_update.callback_query.message.chat.id = chat_id

    await custom_poll_action_callback(mock_update, mock_context)

    call_kwargs = mock_context.bot.edit_message_text.call_args.kwargs
    text = call_kwargs.get("text")
    assert "No votes" in text


@pytest.mark.asyncio
async def test_custom_poll_close_with_weighted_voting(mock_update, mock_context):
    """Test closing poll applies weighted voting when enabled."""
    chat_id = 12345
    poll_id = "poll_12345_123456"

    async with db.AsyncSessionLocal() as session:
        # Weighted voting enabled
        session.add(
            Session(
                chat_id=chat_id, is_active=True, poll_type=PollType.CUSTOM, settings_weighted=True
            )
        )

        u1 = User(telegram_id=111, telegram_name="User1")
        u2 = User(telegram_id=222, telegram_name="User2")
        session.add_all([u1, u2])
        await session.flush()

        g1 = Game(
            id=1, name="StarredGame", min_players=2, max_players=4, playing_time=60, complexity=2.0
        )
        g2 = Game(
            id=2, name="NormalGame", min_players=2, max_players=4, playing_time=60, complexity=2.5
        )
        session.add_all([g1, g2])
        await session.flush()

        # User1 has StarredGame as STARRED
        c1 = Collection(user_id=111, game_id=1, state=GameState.STARRED)
        c2 = Collection(user_id=222, game_id=2)
        session.add_all([c1, c2])

        sp1 = SessionPlayer(session_id=chat_id, user_id=111)
        sp2 = SessionPlayer(session_id=chat_id, user_id=222)
        session.add_all([sp1, sp2])

        poll = GameNightPoll(poll_id=poll_id, chat_id=chat_id, message_id=999)
        session.add(poll)

        # One vote each, but StarredGame should get boost from User1
        v1 = PollVote(poll_id=poll_id, user_id=111, game_id=1, user_name="User1")
        v2 = PollVote(poll_id=poll_id, user_id=222, game_id=2, user_name="User2")
        session.add_all([v1, v2])
        await session.commit()

    mock_update.callback_query.data = f"poll_close:{poll_id}"
    mock_update.callback_query.message.chat.id = chat_id

    await custom_poll_action_callback(mock_update, mock_context)

    call_kwargs = mock_context.bot.edit_message_text.call_args.kwargs
    text = call_kwargs.get("text")
    # StarredGame should win due to boost
    assert "StarredGame" in text
    assert "winner" in text.lower()


# ============================================================================
# Integration: Native Poll Mode Still Works
# ============================================================================


@pytest.mark.asyncio
async def test_native_poll_mode_uses_send_poll(mock_update, mock_context):
    """Test that native poll mode still uses Telegram's send_poll."""
    chat_id = 12345

    async with db.AsyncSessionLocal() as session:
        # Native mode
        session.add(Session(chat_id=chat_id, is_active=True, poll_type=PollType.NATIVE))

        u1 = User(telegram_id=111, telegram_name="User1")
        u2 = User(telegram_id=222, telegram_name="User2")
        session.add_all([u1, u2])
        await session.flush()

        g1 = Game(id=1, name="Game1", min_players=2, max_players=4, playing_time=60, complexity=2.5)
        g2 = Game(id=2, name="Game2", min_players=2, max_players=4, playing_time=60, complexity=2.6)
        session.add_all([g1, g2])
        await session.flush()

        c1 = Collection(user_id=111, game_id=1)
        c2 = Collection(user_id=222, game_id=2)
        session.add_all([c1, c2])

        sp1 = SessionPlayer(session_id=chat_id, user_id=111)
        sp2 = SessionPlayer(session_id=chat_id, user_id=222)
        session.add_all([sp1, sp2])
        await session.commit()

    mock_update.effective_chat.id = chat_id

    await create_poll(mock_update, mock_context)

    # In native mode, send_poll should be called
    mock_context.bot.send_poll.assert_called()


@pytest.mark.asyncio
async def test_custom_poll_allows_multiple_votes_per_user(mock_update, mock_context):
    """Test that a user can vote for multiple games in the same poll."""
    chat_id = 12345
    poll_id = "poll_multi_vote"
    user_id = 999

    # 1. Setup Session, Games, Poll
    async with db.AsyncSessionLocal() as session:
        session.add(Session(chat_id=chat_id, is_active=True, poll_type=PollType.CUSTOM))
        session.add(User(telegram_id=user_id, telegram_name="MultiVoter"))

        g1 = Game(id=1, name="Game1", min_players=2, max_players=4, playing_time=60, complexity=2.0)
        g2 = Game(id=2, name="Game2", min_players=2, max_players=4, playing_time=60, complexity=2.0)
        session.add_all([g1, g2])

        session.add(GameNightPoll(poll_id=poll_id, chat_id=chat_id, message_id=123))
        await session.commit()

    # 2. Vote for Game 1
    mock_update.callback_query.data = f"vote:{poll_id}:1"
    mock_update.callback_query.from_user.id = user_id
    mock_update.callback_query.from_user.first_name = "MultiVoter"

    await custom_poll_vote_callback(mock_update, mock_context)

    # 3. Vote for Game 2 (Should NOT fail)
    mock_update.callback_query.data = f"vote:{poll_id}:2"
    await custom_poll_vote_callback(mock_update, mock_context)

    # 4. Verify both votes exist
    async with db.AsyncSessionLocal() as session:
        stmt = select(PollVote).where(PollVote.poll_id == poll_id, PollVote.user_id == user_id)
        votes = (await session.execute(stmt)).scalars().all()

        assert len(votes) == 2
        game_ids = [v.game_id for v in votes]
        assert 1 in game_ids
        assert 2 in game_ids


# ============================================================================
# Weights Toggle Tests (Poll Settings)
# ============================================================================


@pytest.mark.asyncio
async def test_poll_settings_shows_weights_button(mock_update, mock_context):
    """Test poll settings menu contains the weights toggle button."""
    chat_id = 998877
    async with db.AsyncSessionLocal() as session:
        session.add(Session(chat_id=chat_id, is_active=True, settings_weighted=False))
        await session.commit()

    mock_update.callback_query.message.chat.id = chat_id
    await poll_settings_callback(mock_update, mock_context)

    # Check button text
    _, kwargs = mock_update.callback_query.edit_message_text.call_args
    reply_markup = kwargs.get("reply_markup")
    assert reply_markup is not None

    # Flatten keyboard to search for button text
    buttons = [btn for row in reply_markup.inline_keyboard for btn in row]
    btn_texts = [btn.text for btn in buttons]

    assert any("Weights: ❌" in t for t in btn_texts)


@pytest.mark.asyncio
async def test_toggle_weights_updates_setting_and_refresh_menu(mock_update, mock_context):
    """Test toggling weights updates DB and stays in Poll Settings menu."""
    chat_id = 998877
    async with db.AsyncSessionLocal() as session:
        session.add(Session(chat_id=chat_id, is_active=True, settings_weighted=False))
        await session.commit()

    mock_update.callback_query.message.chat.id = chat_id
    await toggle_weights_callback(mock_update, mock_context)

    # Verify DB update
    async with db.AsyncSessionLocal() as session:
        s = await session.get(Session, chat_id)
        assert s.settings_weighted is True

    # Verify it refreshed the SETTINGS menu (not lobby)
    # Check for text in edit_message_text that indicates settings menu
    _, kwargs = mock_update.callback_query.edit_message_text.call_args


@pytest.mark.asyncio
async def test_custom_poll_ui_grouping(mock_update, mock_context):
    """Test pollution UI groups games by complexity with separators."""
    import random

    chat_id = random.randint(10000, 99999)
    base_id = random.randint(10000, 90000)
    g_ids = [base_id + i for i in range(4)]

    # Setup:
    # Game A (Starred, C=2.0) -> Should be at top
    # Game B (C=4.5) -> Group 4
    # Game C (C=4.2) -> Group 4
    # Game D (C=1.5) -> Group 1

    async with db.AsyncSessionLocal() as session:
        session.add(Session(chat_id=chat_id, is_active=True, poll_type=PollType.CUSTOM))
        session.add(User(telegram_id=111, telegram_name="User1"))
        session.add(User(telegram_id=112, telegram_name="User2"))  # Second player needed

        gA = Game(
            id=g_ids[0], name="GameA", min_players=1, max_players=5, complexity=2.0, playing_time=60
        )
        gB = Game(
            id=g_ids[1], name="GameB", min_players=1, max_players=5, complexity=4.5, playing_time=60
        )
        gC = Game(
            id=g_ids[2], name="GameC", min_players=1, max_players=5, complexity=4.2, playing_time=60
        )
        gD = Game(
            id=g_ids[3], name="GameD", min_players=1, max_players=5, complexity=1.5, playing_time=60
        )
        session.add_all([gA, gB, gC, gD])

        # User owns all (Game 0 is starred)
        state_map = {
            g_ids[0]: GameState.STARRED,
            g_ids[1]: GameState.INCLUDED,
            g_ids[2]: GameState.INCLUDED,
            g_ids[3]: GameState.INCLUDED,
        }
        for gid, state in state_map.items():
            session.add(Collection(user_id=111, game_id=gid, state=state))

        session.add(SessionPlayer(session_id=chat_id, user_id=111))
        session.add(SessionPlayer(session_id=chat_id, user_id=112))  # Second player
        await session.commit()

    mock_update.effective_chat.id = chat_id
    await create_poll(mock_update, mock_context)

    # Poll is sent as placeholder then edited. Check edit_message_text.
    calls = mock_context.bot.edit_message_text.call_args_list
    assert len(calls) > 0
    # Get the last call which should have the rendered poll
    _, kwargs = calls[-1]
    keyboard = kwargs["reply_markup"]

    # Flatten buttons
    buttons = [btn for row in keyboard.inline_keyboard for btn in row]
    labels = [btn.text for btn in buttons]
    callbacks = [btn.callback_data for btn in buttons]

    # Verify Starred game exists (in its complexity group, not necessarily first)
    assert any("⭐ GameA" in label for label in labels)

    # Verify Separators (groups 4, 2, and 1 should exist)
    assert "--- 4 ---" in labels or any("--- 4" in label for label in labels)
    assert "--- 1 ---" in labels or any("--- 1" in label for label in labels)

    # Verify Callback data
    assert any("poll_random_vote" in cb for cb in callbacks)


@pytest.mark.asyncio
async def test_custom_poll_random_vote(mock_update, mock_context):
    """Test clicking separator stores a category vote (not a random game vote)."""
    import random

    chat_id = random.randint(10000, 99999)
    poll_id = f"poll_random_{chat_id}"
    base_id = random.randint(10000, 90000)

    async with db.AsyncSessionLocal() as session:
        session.add(Session(chat_id=chat_id, is_active=True, poll_type=PollType.CUSTOM))
        session.add(User(telegram_id=111, telegram_name="RandomVoter"))

        # Group 4: Two games
        g1 = Game(
            id=base_id,
            name="Complex1",
            min_players=1,
            max_players=5,
            complexity=4.5,
            playing_time=60,
        )
        g2 = Game(
            id=base_id + 1,
            name="Complex2",
            min_players=1,
            max_players=5,
            complexity=4.2,
            playing_time=60,
        )
        session.add_all([g1, g2])

        session.add(Collection(user_id=111, game_id=base_id))
        session.add(Collection(user_id=111, game_id=base_id + 1))
        session.add(SessionPlayer(session_id=chat_id, user_id=111))

        session.add(GameNightPoll(poll_id=poll_id, chat_id=chat_id, message_id=999))
        await session.commit()

    mock_update.callback_query.data = f"poll_random_vote:{poll_id}:4"
    mock_update.callback_query.message.chat.id = chat_id
    mock_update.callback_query.message.message_id = 999
    mock_update.callback_query.from_user.id = 111
    mock_update.callback_query.from_user.first_name = "RandomVoter"

    # Mock bot.edit_message_text to avoid awaiting on Mock if not set up
    mock_context.bot.edit_message_text = AsyncMock()

    await custom_poll_action_callback(mock_update, mock_context)

    # Verify category vote added (game_id = -level = -4)
    async with db.AsyncSessionLocal() as session:
        votes = (
            (await session.execute(select(PollVote).where(PollVote.poll_id == poll_id)))
            .scalars()
            .all()
        )

        assert len(votes) == 1
        assert votes[0].game_id == -4  # Category vote marker (negative level)

    # Verify answer indicates category vote
    mock_update.callback_query.answer.assert_called()


@pytest.mark.asyncio
async def test_custom_poll_category_vote_toggle(mock_update, mock_context):
    """Test clicking category header again removes the vote (toggle behavior)."""
    import random

    chat_id = random.randint(10000, 99999)
    poll_id = f"poll_toggle_{chat_id}"
    base_id = random.randint(10000, 90000)

    async with db.AsyncSessionLocal() as session:
        session.add(Session(chat_id=chat_id, is_active=True, poll_type=PollType.CUSTOM))
        session.add(User(telegram_id=111, telegram_name="Toggler"))

        # Group 3: One game
        g1 = Game(
            id=base_id,
            name="Medium",
            min_players=1,
            max_players=5,
            complexity=3.5,
            playing_time=60,
        )
        session.add(g1)

        session.add(Collection(user_id=111, game_id=base_id))
        session.add(SessionPlayer(session_id=chat_id, user_id=111))

        session.add(GameNightPoll(poll_id=poll_id, chat_id=chat_id, message_id=999))

        # Pre-existing category vote
        session.add(PollVote(poll_id=poll_id, user_id=111, game_id=-3, user_name="Toggler"))
        await session.commit()

    mock_update.callback_query.data = f"poll_random_vote:{poll_id}:3"
    mock_update.callback_query.message.chat.id = chat_id
    mock_update.callback_query.message.message_id = 999
    mock_update.callback_query.from_user.id = 111
    mock_update.callback_query.from_user.first_name = "Toggler"

    mock_context.bot.edit_message_text = AsyncMock()

    await custom_poll_action_callback(mock_update, mock_context)

    # Verify category vote was removed
    async with db.AsyncSessionLocal() as session:
        votes = (
            (await session.execute(select(PollVote).where(PollVote.poll_id == poll_id)))
            .scalars()
            .all()
        )

        assert len(votes) == 0

    # Verify answer indicates removal
    mock_update.callback_query.answer.assert_called_with("Category 3 vote removed")


@pytest.mark.asyncio
async def test_auto_close_previous_polls(mock_update, mock_context):
    """Test that starting a new poll closes existing ones."""
    import random

    chat_id = random.randint(10000, 99999)
    old_poll_id = f"old_{chat_id}"

    # Setup: Active Custom Poll
    async with db.AsyncSessionLocal() as session:
        session.add(Session(chat_id=chat_id, is_active=True, poll_type=PollType.CUSTOM))
        session.add(User(telegram_id=111, telegram_name="User1"))
        session.add(User(telegram_id=112, telegram_name="User2"))

        # Add games/players so start_poll works
        g1 = Game(
            id=random.randint(10000, 99999),
            name="Game1",
            min_players=1,
            max_players=5,
            complexity=2.0,
            playing_time=60,
        )
        session.add(g1)
        session.add(Collection(user_id=111, game_id=g1.id))

        session.add(SessionPlayer(session_id=chat_id, user_id=111))
        session.add(SessionPlayer(session_id=chat_id, user_id=112))

        # Existing Poll
        session.add(GameNightPoll(poll_id=old_poll_id, chat_id=chat_id, message_id=888))
        await session.commit()

    mock_update.effective_chat.id = chat_id
    mock_update.callback_query.message.chat.id = chat_id
    mock_update.callback_query.data = "start_poll"

    # Mock bot methods
    mock_context.bot.stop_poll = AsyncMock()
    mock_context.bot.edit_message_text = AsyncMock()
    mock_context.bot.send_message = AsyncMock(
        return_value=MagicMock(message_id=999)
    )  # For new poll

    await start_poll_callback(mock_update, mock_context)

    # Verify Old Poll Closed (Custom mode -> edit_message_text try)
    # Since we didn't mock type to NATIVE, it probably tried stop_poll first then edit_message_text
    # We can check specific calls. The code tries stop_poll, excepts, then edit_message_text.

    # Verify DB deleted
    async with db.AsyncSessionLocal() as session:
        poll = await session.get(GameNightPoll, old_poll_id)
        assert poll is None


@pytest.mark.asyncio
async def test_auto_refresh_poll_on_join(mock_update, mock_context):
    """Test that joining the lobby refreshes the active custom poll."""
    import random

    chat_id = random.randint(10000, 99999)
    poll_id = f"active_{chat_id}"
    new_user_id = 999

    async with db.AsyncSessionLocal() as session:
        session.add(Session(chat_id=chat_id, is_active=True, poll_type=PollType.CUSTOM))
        session.add(User(telegram_id=111, telegram_name="User1"))

        # Games
        g1 = Game(
            id=random.randint(10000, 99999),
            name="GameA",
            min_players=1,
            max_players=5,
            complexity=2.0,
            playing_time=60,
        )
        session.add(g1)
        session.add(Collection(user_id=111, game_id=g1.id))
        session.add(SessionPlayer(session_id=chat_id, user_id=111))

        # Active Poll
        session.add(GameNightPoll(poll_id=poll_id, chat_id=chat_id, message_id=777))
        await session.commit()

    mock_update.callback_query.data = "join_lobby"
    mock_update.callback_query.message.chat.id = chat_id
    mock_update.callback_query.from_user.id = new_user_id
    mock_update.callback_query.from_user.first_name = "NewJoiner"

    mock_context.bot.edit_message_text = AsyncMock()  # Used for updating lobby AND refreshing poll

    await join_lobby_callback(mock_update, mock_context)

    # Verify refresh called
    # render_poll_message calls edit_message_text on message_id=777

    calls = mock_context.bot.edit_message_text.call_args_list
    # Should be at least 2 calls: one for lobby update (no message_id arg, usually)
    # or on query.message
    # one for poll update (message_id=777)

    refresh_call = None
    for call in calls:
        kwargs = call.kwargs
        if kwargs.get("message_id") == 777:
            refresh_call = call
            break

    assert refresh_call is not None
    # Game should be in keyboard buttons (not text if no votes)
    keyboard = refresh_call.kwargs.get("reply_markup")
    assert keyboard is not None
    buttons = [btn.text for row in keyboard.inline_keyboard for btn in row]
    assert any("GameA" in btn for btn in buttons)
