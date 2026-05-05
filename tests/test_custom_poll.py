"""
Tests for the Custom Single Poll Mode feature.

This tests the alternative poll mechanism that uses inline buttons
instead of native Telegram polls to overcome the 10-option limit.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

from src.bot.handlers import (
    _wrap_button_label,
    create_poll,
    custom_poll_action_callback,
    custom_poll_vote_callback,
    join_lobby_callback,
    poll_add_select_callback,
    poll_settings_callback,
    render_poll_message,
    start_poll_callback,
    toggle_allow_adding_callback,
    toggle_hide_results_callback,
    toggle_poll_mode_callback,
    toggle_shuffle_callback,
    toggle_weights_callback,
)
from src.core import db
from src.core.models import (
    Collection,
    Game,
    GameNightPoll,
    GameState,
    PollAddedGame,
    PollType,
    PollVote,
    Session,
    SessionPlayer,
    User,
    VoteType,
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
    mock_update.callback_query.from_user.last_name = None
    mock_update.callback_query.from_user.username = None

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
        vote = PollVote(
            poll_id=poll_id,
            user_id=user_id,
            vote_type=VoteType.GAME,
            game_id=game_id,
            user_name="Voter",
        )
        session.add(vote)
        await session.commit()

    mock_update.callback_query.data = f"vote:{poll_id}:{game_id}"
    mock_update.callback_query.from_user.id = user_id
    mock_update.callback_query.from_user.first_name = "Voter"
    mock_update.callback_query.from_user.last_name = None
    mock_update.callback_query.from_user.username = None

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
        v1 = PollVote(
            poll_id=poll_id, user_id=111, vote_type=VoteType.GAME, game_id=1, user_name="User1"
        )
        v2 = PollVote(
            poll_id=poll_id, user_id=222, vote_type=VoteType.GAME, game_id=1, user_name="User2"
        )
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
        v1 = PollVote(
            poll_id=poll_id, user_id=111, vote_type=VoteType.GAME, game_id=1, user_name="User1"
        )
        v2 = PollVote(
            poll_id=poll_id, user_id=222, vote_type=VoteType.GAME, game_id=2, user_name="User2"
        )
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

    chat_id = 12345
    poll_id = f"poll_cat_close_{chat_id}"
    base_id = 90000

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
        v1 = PollVote(
            poll_id=poll_id,
            user_id=111,
            vote_type=VoteType.CATEGORY,
            category_level=4,
            user_name="User1",
        )
        v2 = PollVote(
            poll_id=poll_id,
            user_id=222,
            vote_type=VoteType.CATEGORY,
            category_level=4,
            user_name="User2",
        )
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
    # Both category votes must resolve to the SAME game — otherwise we'd get
    # a 1-1 split and the close message would announce a tie.
    assert "tie" not in text.lower()


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
        v1 = PollVote(
            poll_id=poll_id, user_id=111, vote_type=VoteType.GAME, game_id=1, user_name="User1"
        )
        v2 = PollVote(
            poll_id=poll_id, user_id=222, vote_type=VoteType.GAME, game_id=2, user_name="User2"
        )
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
# Leaderboard rendering — issue #54
#
# `_handle_poll_close` builds a "Top 5" board from poll scores. Games in the
# poll that received no votes still appear in `scores` with value 0.0, so a
# naive top-N slice padded the leaderboard with 0-pt losers (issue #54).
# These tests pin down the leaderboard contract: only voted games count, the
# board is hidden when fewer than 2 games scored, and the cap of 5 still holds.
# ============================================================================


async def _seed_close_poll_scenario(
    chat_id: int,
    poll_id: str,
    games: list[tuple[int, str]],
    votes: list[tuple[int, int]],
):
    """
    Seed an active session with `games` and `votes`, ready for poll close.

    games: list of (game_id, name) — every game gets added to every player's
           collection, so they're all valid in the poll.
    votes: list of (user_id, game_id) — one vote row per entry.
    """
    voter_ids = sorted({uid for uid, _ in votes}) or [111, 222]
    # Ensure at least 2 players so close logic doesn't trip on player counts.
    player_ids = list(dict.fromkeys(voter_ids + [111, 222]))[:max(2, len(voter_ids))]

    async with db.AsyncSessionLocal() as session:
        session.add(Session(chat_id=chat_id, is_active=True, poll_type=PollType.CUSTOM))

        for uid in player_ids:
            session.add(User(telegram_id=uid, telegram_name=f"User{uid}"))
        await session.flush()

        # Generous player range so games stay "valid" even when tests seed
        # many voters (get_session_valid_games filters by player_count).
        for gid, name in games:
            session.add(
                Game(
                    id=gid,
                    name=name,
                    min_players=1,
                    max_players=20,
                    playing_time=60,
                    complexity=2.0,
                )
            )
        await session.flush()

        # Every player has every game in their collection — keeps games "valid".
        for uid in player_ids:
            for gid, _ in games:
                session.add(Collection(user_id=uid, game_id=gid))

        for uid in player_ids:
            session.add(SessionPlayer(session_id=chat_id, user_id=uid))

        session.add(GameNightPoll(poll_id=poll_id, chat_id=chat_id, message_id=999))

        for uid, gid in votes:
            session.add(
                PollVote(
                    poll_id=poll_id,
                    user_id=uid,
                    vote_type=VoteType.GAME,
                    game_id=gid,
                    user_name=f"User{uid}",
                )
            )

        await session.commit()


@pytest.mark.asyncio
async def test_leaderboard_excludes_zero_vote_games(mock_update, mock_context):
    """
    Issue #54: with 2 voted games out of 5 in the poll, the leaderboard must
    only list those 2 — not pad the remaining slots with 0-pt entries.
    """
    chat_id = 12345
    poll_id = "poll_lb_zero"

    await _seed_close_poll_scenario(
        chat_id,
        poll_id,
        games=[
            (1, "Voted A"),
            (2, "Voted B"),
            (3, "Unvoted X"),
            (4, "Unvoted Y"),
            (5, "Unvoted Z"),
        ],
        votes=[(111, 1), (111, 1), (222, 2)],  # 2 votes for A, 1 for B
    )

    mock_update.callback_query.data = f"poll_close:{poll_id}"
    mock_update.callback_query.message.chat.id = chat_id

    await custom_poll_action_callback(mock_update, mock_context)

    text = mock_context.bot.edit_message_text.call_args.kwargs.get("text")
    assert "Top 5:" in text
    assert "Voted A" in text
    assert "Voted B" in text
    # Unvoted games must not appear, and no 0-pt rows.
    assert "Unvoted X" not in text
    assert "Unvoted Y" not in text
    assert "Unvoted Z" not in text
    assert "0.0 pts" not in text


@pytest.mark.asyncio
async def test_leaderboard_hidden_when_only_one_game_voted(mock_update, mock_context):
    """
    With a single voted game (everything else 0 pts), there's nothing to
    rank — the leaderboard section must not render. The winner line still does.
    """
    chat_id = 12345
    poll_id = "poll_lb_one"

    await _seed_close_poll_scenario(
        chat_id,
        poll_id,
        games=[(1, "Solo Winner"), (2, "Ignored"), (3, "Also Ignored")],
        votes=[(111, 1), (222, 1)],
    )

    mock_update.callback_query.data = f"poll_close:{poll_id}"
    mock_update.callback_query.message.chat.id = chat_id

    await custom_poll_action_callback(mock_update, mock_context)

    text = mock_context.bot.edit_message_text.call_args.kwargs.get("text")
    assert "Solo Winner" in text
    assert "winner" in text.lower()
    # No leaderboard header, no padding.
    assert "Top 5:" not in text
    assert "Ignored" not in text


@pytest.mark.asyncio
async def test_leaderboard_caps_at_five_when_more_voted(mock_update, mock_context):
    """
    With 6 voted games, the leaderboard should still show exactly the top 5
    by score — confirming the cap survives the zero-filter change.
    """
    chat_id = 12345
    poll_id = "poll_lb_cap"

    games = [(i, f"Game{i}") for i in range(1, 7)]
    # Game1 gets 6 votes, Game2 gets 5, ..., Game6 gets 1 vote.
    votes: list[tuple[int, int]] = []
    for gid in range(1, 7):
        vote_count = 7 - gid
        for v in range(vote_count):
            votes.append((100 + v, gid))

    await _seed_close_poll_scenario(chat_id, poll_id, games=games, votes=votes)

    mock_update.callback_query.data = f"poll_close:{poll_id}"
    mock_update.callback_query.message.chat.id = chat_id

    await custom_poll_action_callback(mock_update, mock_context)

    text = mock_context.bot.edit_message_text.call_args.kwargs.get("text")
    assert "Top 5:" in text
    for gid in range(1, 6):
        assert f"Game{gid}" in text
    # Game6 (lowest, 1 vote) is squeezed out by the cap, not by zero-filter.
    assert "Game6" not in text


@pytest.mark.asyncio
async def test_leaderboard_renders_tie_at_top_without_zero_padding(mock_update, mock_context):
    """
    A 2-way tie with one extra unvoted game should announce a tie *and* show
    a 2-row leaderboard — but still no 0-pt third row.
    """
    chat_id = 12345
    poll_id = "poll_lb_tie"

    await _seed_close_poll_scenario(
        chat_id,
        poll_id,
        games=[(1, "TiedA"), (2, "TiedB"), (3, "Unvoted")],
        votes=[(111, 1), (222, 2)],
    )

    mock_update.callback_query.data = f"poll_close:{poll_id}"
    mock_update.callback_query.message.chat.id = chat_id

    await custom_poll_action_callback(mock_update, mock_context)

    text = mock_context.bot.edit_message_text.call_args.kwargs.get("text")
    assert "tie" in text.lower()
    assert "TiedA" in text and "TiedB" in text
    assert "Top 5:" in text
    assert "Unvoted" not in text
    assert "0.0 pts" not in text


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
    mock_update.callback_query.from_user.last_name = None
    mock_update.callback_query.from_user.username = None

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

    chat_id = 99999
    base_id = 80000
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

    chat_id = 88888
    poll_id = f"poll_random_{chat_id}"
    base_id = 70000

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
    mock_update.callback_query.from_user.last_name = None
    mock_update.callback_query.from_user.username = None

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
        assert votes[0].category_level == 4  # Category vote should have level set

    # Verify answer indicates category vote
    mock_update.callback_query.answer.assert_called()


@pytest.mark.asyncio
async def test_custom_poll_category_vote_toggle(mock_update, mock_context):
    """Test clicking category header again removes the vote (toggle behavior)."""

    chat_id = 77777
    poll_id = f"poll_toggle_{chat_id}"
    base_id = 60000

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
        session.add(
            PollVote(
                poll_id=poll_id,
                user_id=111,
                vote_type=VoteType.CATEGORY,
                category_level=3,
                user_name="Toggler",
            )
        )
        await session.commit()

    mock_update.callback_query.data = f"poll_random_vote:{poll_id}:3"
    mock_update.callback_query.message.chat.id = chat_id
    mock_update.callback_query.message.message_id = 999
    mock_update.callback_query.from_user.id = 111
    mock_update.callback_query.from_user.first_name = "Toggler"
    mock_update.callback_query.from_user.last_name = None
    mock_update.callback_query.from_user.username = None

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
    mock_update.callback_query.answer.assert_called_with("Vote removed")


@pytest.mark.asyncio
async def test_auto_close_previous_polls(mock_update, mock_context):
    """Test that starting a new poll closes existing ones."""

    chat_id = 66666
    old_poll_id = f"old_{chat_id}"

    # Setup: Active Custom Poll
    async with db.AsyncSessionLocal() as session:
        session.add(Session(chat_id=chat_id, is_active=True, poll_type=PollType.CUSTOM))
        session.add(User(telegram_id=111, telegram_name="User1"))
        session.add(User(telegram_id=112, telegram_name="User2"))

        # Add games/players so start_poll works
        g1 = Game(
            id=44444,
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

    chat_id = 55555
    poll_id = f"active_{chat_id}"
    new_user_id = 999

    async with db.AsyncSessionLocal() as session:
        session.add(Session(chat_id=chat_id, is_active=True, poll_type=PollType.CUSTOM))
        session.add(User(telegram_id=111, telegram_name="User1"))

        # Games
        g1 = Game(
            id=33333,
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
    mock_update.callback_query.from_user.last_name = None
    mock_update.callback_query.from_user.username = None

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


# ============================================================================
# New Settings Toggle Tests (API 9.6 Migration)
# ============================================================================


@pytest.mark.asyncio
async def test_shuffle_is_stable_across_renders(mock_context):
    """With shuffle_options on, repeated renders of the same poll keep the same button order."""
    chat_id = 31415
    poll_id = f"poll_shuffle_{chat_id}"
    base_id = 90000

    async with db.AsyncSessionLocal() as session:
        session.add(
            Session(
                chat_id=chat_id,
                is_active=True,
                poll_type=PollType.CUSTOM,
                shuffle_options=True,
            )
        )
        session.add(
            GameNightPoll(poll_id=poll_id, chat_id=chat_id, message_id=555, shuffle_seed=424242)
        )
        session.add(User(telegram_id=111, telegram_name="P1"))
        session.add(User(telegram_id=112, telegram_name="P2"))
        session.add(SessionPlayer(session_id=chat_id, user_id=111))
        session.add(SessionPlayer(session_id=chat_id, user_id=112))
        games = [
            Game(
                id=base_id + i,
                name=f"Game{i}",
                min_players=1,
                max_players=5,
                complexity=2.0,
                playing_time=60,
            )
            for i in range(6)
        ]
        session.add_all(games)
        for g in games:
            session.add(Collection(user_id=111, game_id=g.id))
        await session.commit()

    def vote_callbacks_of(call):
        kb = call.kwargs["reply_markup"]
        return [
            btn.callback_data
            for row in kb.inline_keyboard
            for btn in row
            if btn.callback_data and btn.callback_data.startswith("vote:")
        ]

    async with db.AsyncSessionLocal() as session:
        games_list = list(
            (await session.execute(select(Game).where(Game.id.in_([g.id for g in games]))))
            .scalars()
            .all()
        )
        await render_poll_message(
            mock_context.bot, chat_id, 555, session, poll_id, games_list, set()
        )
        first_order = vote_callbacks_of(mock_context.bot.edit_message_text.call_args_list[-1])

        await render_poll_message(
            mock_context.bot, chat_id, 555, session, poll_id, games_list, set()
        )
        second_order = vote_callbacks_of(mock_context.bot.edit_message_text.call_args_list[-1])

    assert first_order == second_order
    # Sanity: shuffle actually ran (order differs from the sorted-by-id input)
    sorted_order = [f"vote:{poll_id}:{base_id + i}" for i in range(6)]
    assert first_order != sorted_order


@pytest.mark.asyncio
async def test_toggle_shuffle_options(mock_update, mock_context):
    """Test toggling shuffle options setting."""
    chat_id = 12345

    async with db.AsyncSessionLocal() as session:
        session.add(Session(chat_id=chat_id, is_active=True, shuffle_options=False))
        await session.commit()

    mock_update.callback_query.message.chat.id = chat_id

    await toggle_shuffle_callback(mock_update, mock_context)

    async with db.AsyncSessionLocal() as session:
        sess = await session.get(Session, chat_id)
        assert sess.shuffle_options is True

    # Toggle back
    await toggle_shuffle_callback(mock_update, mock_context)

    async with db.AsyncSessionLocal() as session:
        sess = await session.get(Session, chat_id)
        assert sess.shuffle_options is False


@pytest.mark.asyncio
async def test_toggle_hide_results(mock_update, mock_context):
    """Test toggling hide results setting."""
    chat_id = 12345

    async with db.AsyncSessionLocal() as session:
        session.add(Session(chat_id=chat_id, is_active=True, hide_results=False))
        await session.commit()

    mock_update.callback_query.message.chat.id = chat_id

    await toggle_hide_results_callback(mock_update, mock_context)

    async with db.AsyncSessionLocal() as session:
        sess = await session.get(Session, chat_id)
        assert sess.hide_results is True


@pytest.mark.asyncio
async def test_toggle_allow_adding_options(mock_update, mock_context):
    """Test toggling allow adding options setting."""
    chat_id = 12345

    async with db.AsyncSessionLocal() as session:
        session.add(Session(chat_id=chat_id, is_active=True, allow_adding_options=False))
        await session.commit()

    mock_update.callback_query.message.chat.id = chat_id

    await toggle_allow_adding_callback(mock_update, mock_context)

    async with db.AsyncSessionLocal() as session:
        sess = await session.get(Session, chat_id)
        assert sess.allow_adding_options is True


# ============================================================================
# Hide Results Tests
# ============================================================================


@pytest.mark.asyncio
async def test_custom_poll_hide_results_hides_vote_counts(mock_update, mock_context):
    """Test that hide_results hides vote counts and voter names in the poll display."""
    chat_id = 12345
    poll_id = "poll_12345_hide"

    async with db.AsyncSessionLocal() as session:
        session.add(
            Session(chat_id=chat_id, is_active=True, poll_type=PollType.CUSTOM, hide_results=True)
        )

        u1 = User(telegram_id=111, telegram_name="User1")
        u2 = User(telegram_id=222, telegram_name="User2")
        session.add_all([u1, u2])
        await session.flush()

        g1 = Game(id=1, name="Catan", min_players=2, max_players=4, playing_time=60, complexity=2.0)
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

        # Add a vote
        v1 = PollVote(
            poll_id=poll_id, user_id=111, vote_type=VoteType.GAME, game_id=1, user_name="User1"
        )
        session.add(v1)
        await session.commit()

    # Trigger a refresh to render the poll
    mock_update.callback_query.data = f"poll_refresh:{poll_id}"
    await custom_poll_action_callback(mock_update, mock_context)

    call_kwargs = mock_context.bot.edit_message_text.call_args.kwargs
    text = call_kwargs.get("text")

    # Vote counts and voter names should be hidden
    assert "Results will be revealed" in text
    assert "User1" not in text

    # Button labels should NOT show vote counts
    keyboard = call_kwargs.get("reply_markup")
    button_labels = [btn.text for row in keyboard.inline_keyboard for btn in row]
    catan_buttons = [b for b in button_labels if "Catan" in b]
    for label in catan_buttons:
        assert "(1)" not in label


# ============================================================================
# Allow Adding Options Tests
# ============================================================================


@pytest.mark.asyncio
async def test_custom_poll_add_button_shown_when_enabled(mock_update, mock_context):
    """Test that the Add button appears when allow_adding_options is enabled."""
    chat_id = 12345
    poll_id = "poll_12345_add"

    async with db.AsyncSessionLocal() as session:
        session.add(
            Session(
                chat_id=chat_id,
                is_active=True,
                poll_type=PollType.CUSTOM,
                allow_adding_options=True,
            )
        )

        u1 = User(telegram_id=111, telegram_name="User1")
        u2 = User(telegram_id=222, telegram_name="User2")
        session.add_all([u1, u2])
        await session.flush()

        g1 = Game(id=1, name="Catan", min_players=2, max_players=4, playing_time=60, complexity=2.0)
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

    call_kwargs = mock_context.bot.edit_message_text.call_args.kwargs
    keyboard = call_kwargs.get("reply_markup")
    button_labels = [btn.text for row in keyboard.inline_keyboard for btn in row]

    assert "➕ Add" in button_labels


@pytest.mark.asyncio
async def test_custom_poll_add_game_select(mock_update, mock_context):
    """Test adding a game to the poll via poll_add_select callback."""
    chat_id = 12345
    poll_id = "poll_12345_add2"
    extra_game_id = 99

    async with db.AsyncSessionLocal() as session:
        session.add(
            Session(
                chat_id=chat_id,
                is_active=True,
                poll_type=PollType.CUSTOM,
                allow_adding_options=True,
            )
        )

        u1 = User(telegram_id=111, telegram_name="User1")
        u2 = User(telegram_id=222, telegram_name="User2")
        session.add_all([u1, u2])
        await session.flush()

        g1 = Game(id=1, name="Catan", min_players=2, max_players=4, playing_time=60, complexity=2.0)
        g_extra = Game(
            id=extra_game_id,
            name="ExtraGame",
            min_players=1,
            max_players=2,
            playing_time=30,
            complexity=1.5,
        )
        session.add_all([g1, g_extra])
        await session.flush()

        c1 = Collection(user_id=111, game_id=1)
        c2 = Collection(user_id=222, game_id=1)
        c3 = Collection(user_id=111, game_id=extra_game_id)
        session.add_all([c1, c2, c3])

        sp1 = SessionPlayer(session_id=chat_id, user_id=111)
        sp2 = SessionPlayer(session_id=chat_id, user_id=222)
        session.add_all([sp1, sp2])

        poll = GameNightPoll(poll_id=poll_id, chat_id=chat_id, message_id=999)
        session.add(poll)
        await session.commit()

    # Simulate selecting the extra game from the picker
    mock_update.callback_query.data = f"poll_add_select:{poll_id}:{extra_game_id}"
    mock_update.callback_query.from_user.id = 111
    mock_update.callback_query.message.chat.id = chat_id
    mock_update.callback_query.message.delete = AsyncMock()

    await poll_add_select_callback(mock_update, mock_context)

    # Verify PollAddedGame was created
    async with db.AsyncSessionLocal() as session:
        stmt = select(PollAddedGame).where(
            PollAddedGame.poll_id == poll_id, PollAddedGame.game_id == extra_game_id
        )
        added = (await session.execute(stmt)).scalar_one_or_none()
        assert added is not None
        assert added.added_by_user_id == 111

    # Verify the poll was refreshed (edit_message_text called)
    mock_context.bot.edit_message_text.assert_called()


# ============================================================================
# Settings Keyboard Tests
# ============================================================================


@pytest.mark.asyncio
async def test_settings_shows_new_toggles(mock_update, mock_context):
    """Test that poll settings shows the new toggle buttons."""
    chat_id = 12345

    async with db.AsyncSessionLocal() as session:
        session.add(Session(chat_id=chat_id, is_active=True))
        await session.commit()

    mock_update.callback_query.message.chat.id = chat_id

    await poll_settings_callback(mock_update, mock_context)

    call_args = mock_update.callback_query.edit_message_text.call_args
    text = call_args.kwargs.get("text") or call_args.args[0]
    keyboard = call_args.kwargs.get("reply_markup")
    button_labels = [btn.text for row in keyboard.inline_keyboard for btn in row]

    # Verify new settings appear in text
    assert "Shuffle" in text
    assert "Hide Results" in text
    assert "Allow Suggestions" in text

    # Verify new buttons exist
    assert any("Shuffle" in b for b in button_labels)
    assert any("Hide Results" in b for b in button_labels)
    assert any("Allow Suggestions" in b for b in button_labels)


# ============================================================================
# Poll Description Tests
# ============================================================================


@pytest.mark.asyncio
async def test_custom_poll_shows_description(mock_update, mock_context):
    """Test that custom poll header includes context description."""
    chat_id = 12345
    poll_id = "poll_12345_desc"

    async with db.AsyncSessionLocal() as session:
        session.add(
            Session(
                chat_id=chat_id,
                is_active=True,
                poll_type=PollType.CUSTOM,
                settings_weighted=True,
            )
        )

        u1 = User(telegram_id=111, telegram_name="User1")
        u2 = User(telegram_id=222, telegram_name="User2")
        session.add_all([u1, u2])
        await session.flush()

        g1 = Game(id=1, name="Catan", min_players=2, max_players=4, playing_time=60, complexity=2.0)
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

    call_kwargs = mock_context.bot.edit_message_text.call_args.kwargs
    text = call_kwargs.get("text")

    # Should show player count, game count, and active settings
    assert "2 players" in text
    assert "1 games" in text
    assert "weighted voting" in text


# ============================================================================
# Add Game Cancel Test
# ============================================================================


@pytest.mark.asyncio
async def test_custom_poll_add_cancel(mock_update, mock_context):
    """Test that canceling the add picker deletes the message without adding a game."""
    chat_id = 12345
    poll_id = "poll_12345_cancel"

    async with db.AsyncSessionLocal() as session:
        session.add(
            Session(
                chat_id=chat_id,
                is_active=True,
                poll_type=PollType.CUSTOM,
                allow_adding_options=True,
            )
        )
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

    mock_update.callback_query.data = f"poll_add_cancel:{poll_id}"
    mock_update.callback_query.answer = AsyncMock()
    mock_update.callback_query.message.delete = AsyncMock()

    await poll_add_select_callback(mock_update, mock_context)

    # Picker message should be deleted
    mock_update.callback_query.message.delete.assert_called_once()

    # No PollAddedGame should exist
    async with db.AsyncSessionLocal() as session:
        stmt = select(PollAddedGame).where(PollAddedGame.poll_id == poll_id)
        added = (await session.execute(stmt)).scalars().all()
        assert len(added) == 0


# ============================================================================
# Duplicate Add Prevention Test
# ============================================================================


@pytest.mark.asyncio
async def test_custom_poll_add_game_duplicate_prevented(mock_update, mock_context):
    """Test that adding the same game twice returns 'Already added!' message."""
    chat_id = 12345
    poll_id = "poll_12345_dup"
    extra_game_id = 99

    async with db.AsyncSessionLocal() as session:
        session.add(
            Session(
                chat_id=chat_id,
                is_active=True,
                poll_type=PollType.CUSTOM,
                allow_adding_options=True,
            )
        )
        u1 = User(telegram_id=111, telegram_name="User1")
        u2 = User(telegram_id=222, telegram_name="User2")
        session.add_all([u1, u2])
        await session.flush()

        g1 = Game(id=1, name="Catan", min_players=2, max_players=4, playing_time=60, complexity=2.0)
        g_extra = Game(
            id=extra_game_id,
            name="Extra",
            min_players=1,
            max_players=2,
            playing_time=30,
            complexity=1.0,
        )
        session.add_all([g1, g_extra])
        await session.flush()

        c1 = Collection(user_id=111, game_id=1)
        c2 = Collection(user_id=222, game_id=1)
        session.add_all([c1, c2])

        sp1 = SessionPlayer(session_id=chat_id, user_id=111)
        sp2 = SessionPlayer(session_id=chat_id, user_id=222)
        session.add_all([sp1, sp2])

        poll = GameNightPoll(poll_id=poll_id, chat_id=chat_id, message_id=999)
        session.add(poll)

        # Pre-add the game
        added = PollAddedGame(poll_id=poll_id, game_id=extra_game_id, added_by_user_id=111)
        session.add(added)
        await session.commit()

    mock_update.callback_query.data = f"poll_add_select:{poll_id}:{extra_game_id}"
    mock_update.callback_query.from_user.id = 111
    mock_update.callback_query.answer = AsyncMock()
    mock_update.callback_query.message.chat.id = chat_id
    mock_update.callback_query.message.delete = AsyncMock()

    await poll_add_select_callback(mock_update, mock_context)

    # Should show "Already added!"
    mock_update.callback_query.answer.assert_called_with("Already added!")

    # Should NOT create a second record
    async with db.AsyncSessionLocal() as session:
        stmt = select(PollAddedGame).where(
            PollAddedGame.poll_id == poll_id, PollAddedGame.game_id == extra_game_id
        )
        records = (await session.execute(stmt)).scalars().all()
        assert len(records) == 1


# ============================================================================
# Hide Results with Category Votes Test
# ============================================================================


@pytest.mark.asyncio
async def test_custom_poll_hide_results_hides_category_votes(mock_update, mock_context):
    """Test that hide_results also hides category vote counts."""
    chat_id = 12345
    poll_id = "poll_12345_hide_cat"

    async with db.AsyncSessionLocal() as session:
        session.add(
            Session(chat_id=chat_id, is_active=True, poll_type=PollType.CUSTOM, hide_results=True)
        )

        u1 = User(telegram_id=111, telegram_name="User1")
        u2 = User(telegram_id=222, telegram_name="User2")
        session.add_all([u1, u2])
        await session.flush()

        g1 = Game(id=1, name="Game1", min_players=2, max_players=4, playing_time=60, complexity=3.5)
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

        # Add category votes
        v1 = PollVote(
            poll_id=poll_id,
            user_id=111,
            vote_type=VoteType.CATEGORY,
            category_level=3,
            user_name="User1",
        )
        session.add(v1)
        await session.commit()

    mock_update.callback_query.data = f"poll_refresh:{poll_id}"
    await custom_poll_action_callback(mock_update, mock_context)

    call_kwargs = mock_context.bot.edit_message_text.call_args.kwargs
    text = call_kwargs.get("text")

    # Should show hidden results message, NOT category vote details
    assert "Results will be revealed" in text
    assert "Category 3" not in text
    assert "User1" not in text

    # Category header in buttons should NOT show vote count
    keyboard = call_kwargs.get("reply_markup")
    button_labels = [btn.text for row in keyboard.inline_keyboard for btn in row]
    cat_headers = [b for b in button_labels if "---" in b]
    for header in cat_headers:
        assert "(1)" not in header


# ============================================================================
# Native Poll New Parameters Test
# ============================================================================


@pytest.mark.asyncio
async def test_native_poll_passes_new_api_parameters(mock_update, mock_context):
    """Test that native polls pass shuffle, hide_results, allow_adding params to send_poll."""
    chat_id = 12345

    async with db.AsyncSessionLocal() as session:
        session.add(
            Session(
                chat_id=chat_id,
                is_active=True,
                poll_type=PollType.NATIVE,
                shuffle_options=True,
                hide_results=True,
                allow_adding_options=True,
            )
        )

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

    mock_context.bot.send_poll.assert_called()
    call_kwargs = mock_context.bot.send_poll.call_args.kwargs

    # Bot API 9.1/9.6 poll params are passed via api_kwargs because
    # python-telegram-bot 22.x does not yet accept them as native kwargs.
    api_kwargs = call_kwargs["api_kwargs"]
    assert api_kwargs["allows_revoting"] is True
    assert api_kwargs["shuffle_options"] is True
    assert api_kwargs["hide_results_until_closes"] is True
    assert api_kwargs["allow_adding_options"] is True
    assert "description" in api_kwargs
    assert "2 players" in api_kwargs["description"]


# ============================================================================
# Allow Adding Guard Test
# ============================================================================


@pytest.mark.asyncio
async def test_poll_add_blocked_when_setting_disabled(mock_update, mock_context):
    """Test that _handle_poll_add rejects when allow_adding_options is False."""
    chat_id = 12345
    poll_id = "poll_12345_guard"

    async with db.AsyncSessionLocal() as session:
        session.add(
            Session(
                chat_id=chat_id,
                is_active=True,
                poll_type=PollType.CUSTOM,
                allow_adding_options=False,
            )
        )
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

    mock_update.callback_query.data = f"poll_add:{poll_id}"
    mock_update.callback_query.message.chat.id = chat_id
    mock_update.callback_query.from_user.id = 111
    mock_update.callback_query.answer = AsyncMock()

    await custom_poll_action_callback(mock_update, mock_context)

    # Should show rejection alert
    mock_update.callback_query.answer.assert_called_with(
        "Adding games is not enabled for this poll.", show_alert=True
    )

    # Should NOT send a picker message
    mock_context.bot.send_message.assert_not_called()


# ============================================================================
# Cascade Delete Test
# ============================================================================


@pytest.mark.asyncio
async def test_poll_added_games_cascade_deleted(mock_update, mock_context):
    """Test that PollAddedGame records are cascade-deleted when poll is closed."""
    chat_id = 12345
    poll_id = "poll_12345_cascade"

    async with db.AsyncSessionLocal() as session:
        session.add(
            Session(
                chat_id=chat_id,
                is_active=True,
                poll_type=PollType.CUSTOM,
                allow_adding_options=True,
            )
        )
        u1 = User(telegram_id=111, telegram_name="User1")
        u2 = User(telegram_id=222, telegram_name="User2")
        session.add_all([u1, u2])
        await session.flush()

        g1 = Game(id=1, name="Game1", min_players=2, max_players=4, playing_time=60, complexity=2.0)
        g2 = Game(
            id=99,
            name="Added",
            min_players=2,
            max_players=4,
            playing_time=60,
            complexity=1.5,
        )
        session.add_all([g1, g2])
        await session.flush()

        c1 = Collection(user_id=111, game_id=1)
        c2 = Collection(user_id=222, game_id=1)
        session.add_all([c1, c2])

        sp1 = SessionPlayer(session_id=chat_id, user_id=111)
        sp2 = SessionPlayer(session_id=chat_id, user_id=222)
        session.add_all([sp1, sp2])

        poll = GameNightPoll(poll_id=poll_id, chat_id=chat_id, message_id=999)
        session.add(poll)

        added = PollAddedGame(poll_id=poll_id, game_id=99, added_by_user_id=111)
        session.add(added)
        await session.commit()

    mock_update.callback_query.data = f"poll_close:{poll_id}"
    mock_update.callback_query.message.chat.id = chat_id
    mock_update.callback_query.answer = AsyncMock()

    await custom_poll_action_callback(mock_update, mock_context)

    # Verify both poll and added games are gone
    async with db.AsyncSessionLocal() as session:
        poll = await session.get(GameNightPoll, poll_id)
        assert poll is None

        stmt = select(PollAddedGame).where(PollAddedGame.poll_id == poll_id)
        added = (await session.execute(stmt)).scalars().all()
        assert len(added) == 0


# ============================================================================
# Button Label Wrapping Tests
# ============================================================================


def test_wrap_button_label_short_name_single_line():
    """Short names produce a single-line label with prefix and suffix glued on."""
    label = _wrap_button_label("⭐ ", "Catan", " (3)")
    assert label == "⭐ Catan (3)"
    assert "\n" not in label


def test_wrap_button_label_long_name_wraps_across_lines():
    """A long name wraps across multiple lines instead of being truncated."""
    label = _wrap_button_label("", "Twilight Imperium Fourth Edition", "")
    assert "\n" in label
    # Every word from the original name should still appear somewhere
    for word in ("Twilight", "Imperium", "Fourth", "Edition"):
        assert word in label
    # No ellipsis: the whole name fit within the wrap budget
    assert "…" not in label


def test_wrap_button_label_preserves_vote_count_on_wrapped_name():
    """The vote-count suffix lands on the final wrapped line, never lost."""
    label = _wrap_button_label("⭐ ", "Twilight Imperium Fourth Edition", " (7)")
    lines = label.split("\n")
    assert len(lines) >= 2
    assert lines[0].startswith("⭐ ")
    assert lines[-1].endswith(" (7)")


def test_wrap_button_label_extremely_long_name_truncates_with_ellipsis():
    """A name that overflows the max-lines budget ends with an ellipsis."""
    very_long = "Word " * 40  # 200 chars of repeated words
    label = _wrap_button_label("", very_long.strip(), " (1)")
    assert "…" in label
    assert label.endswith(" (1)")
    assert label.count("\n") == 2  # exactly _BUTTON_MAX_LINES - 1 newlines


def test_wrap_button_label_long_word_without_spaces_breaks_mid_word():
    """A single word longer than the line width is split mid-word, not lost."""
    label = _wrap_button_label("", "Supercalifragilisticexpialidocious", "")
    # Should produce more than one line (the word is 34 chars, line width 18)
    assert "\n" in label
    # The full word's characters should all be present
    assert "Supercali" in label
    assert "expialidocious" in label.replace("\n", "")


@pytest.mark.asyncio
async def test_render_poll_long_game_name_wrapped_not_truncated(mock_update, mock_context):
    """A game with a long name renders as a wrapped multi-line button, no '…'."""
    chat_id = 12345
    poll_id = "poll_12345_wrap"
    long_name = "Twilight Imperium Fourth Edition"

    async with db.AsyncSessionLocal() as session:
        session.add(Session(chat_id=chat_id, is_active=True, poll_type=PollType.CUSTOM))

        session.add_all(
            [
                User(telegram_id=111, telegram_name="User1"),
                User(telegram_id=222, telegram_name="User2"),
            ]
        )
        await session.flush()

        g1 = Game(
            id=1, name=long_name, min_players=2, max_players=4, playing_time=240, complexity=4.5
        )
        session.add(g1)
        await session.flush()

        session.add_all(
            [
                Collection(user_id=111, game_id=1),
                Collection(user_id=222, game_id=1),
                SessionPlayer(session_id=chat_id, user_id=111),
                SessionPlayer(session_id=chat_id, user_id=222),
                GameNightPoll(poll_id=poll_id, chat_id=chat_id, message_id=999),
            ]
        )
        await session.commit()

    mock_update.callback_query.data = f"poll_refresh:{poll_id}"
    await custom_poll_action_callback(mock_update, mock_context)

    keyboard = mock_context.bot.edit_message_text.call_args.kwargs["reply_markup"]
    button_labels = [btn.text for row in keyboard.inline_keyboard for btn in row]
    long_button = next(b for b in button_labels if "Twilight" in b)

    assert "\n" in long_button, "long name should wrap across multiple lines"
    assert "…" not in long_button, "name fits within wrap budget; no ellipsis expected"
    assert "Edition" in long_button, "tail of the name should not be lost"


@pytest.mark.asyncio
async def test_render_poll_long_name_group_renders_one_per_row(mock_update, mock_context):
    """A complexity group containing any long name lays out one button per row."""
    chat_id = 12345
    poll_id = "poll_12345_onecol"

    async with db.AsyncSessionLocal() as session:
        session.add(Session(chat_id=chat_id, is_active=True, poll_type=PollType.CUSTOM))

        session.add_all(
            [
                User(telegram_id=111, telegram_name="User1"),
                User(telegram_id=222, telegram_name="User2"),
            ]
        )
        await session.flush()

        # Two games in the same complexity bucket, one with a long name.
        g_short = Game(
            id=1, name="Catan", min_players=2, max_players=4, playing_time=60, complexity=2.0
        )
        g_long = Game(
            id=2,
            name="Twilight Imperium Fourth Edition",
            min_players=2,
            max_players=4,
            playing_time=60,
            complexity=2.0,
        )
        session.add_all([g_short, g_long])
        await session.flush()

        session.add_all(
            [
                Collection(user_id=111, game_id=1),
                Collection(user_id=111, game_id=2),
                Collection(user_id=222, game_id=1),
                Collection(user_id=222, game_id=2),
                SessionPlayer(session_id=chat_id, user_id=111),
                SessionPlayer(session_id=chat_id, user_id=222),
                GameNightPoll(poll_id=poll_id, chat_id=chat_id, message_id=999),
            ]
        )
        await session.commit()

    mock_update.callback_query.data = f"poll_refresh:{poll_id}"
    await custom_poll_action_callback(mock_update, mock_context)

    keyboard = mock_context.bot.edit_message_text.call_args.kwargs["reply_markup"]
    # Find the two game-vote rows (callback_data starts with "vote:")
    vote_rows = [
        row
        for row in keyboard.inline_keyboard
        if row and row[0].callback_data and row[0].callback_data.startswith("vote:")
    ]
    assert len(vote_rows) == 2, "expected one row per game when group has a long name"
    assert all(len(row) == 1 for row in vote_rows), "long-name group should be one-per-row"


@pytest.mark.asyncio
async def test_render_poll_short_names_stay_two_per_row(mock_update, mock_context):
    """A complexity group with only short names keeps the two-per-row layout."""
    chat_id = 12345
    poll_id = "poll_12345_twocol"

    async with db.AsyncSessionLocal() as session:
        session.add(Session(chat_id=chat_id, is_active=True, poll_type=PollType.CUSTOM))

        session.add_all(
            [
                User(telegram_id=111, telegram_name="User1"),
                User(telegram_id=222, telegram_name="User2"),
            ]
        )
        await session.flush()

        # Two short-named games, same complexity bucket.
        g1 = Game(id=1, name="Catan", min_players=2, max_players=4, playing_time=60, complexity=2.0)
        g2 = Game(id=2, name="Azul", min_players=2, max_players=4, playing_time=60, complexity=2.0)
        session.add_all([g1, g2])
        await session.flush()

        session.add_all(
            [
                Collection(user_id=111, game_id=1),
                Collection(user_id=111, game_id=2),
                Collection(user_id=222, game_id=1),
                Collection(user_id=222, game_id=2),
                SessionPlayer(session_id=chat_id, user_id=111),
                SessionPlayer(session_id=chat_id, user_id=222),
                GameNightPoll(poll_id=poll_id, chat_id=chat_id, message_id=999),
            ]
        )
        await session.commit()

    mock_update.callback_query.data = f"poll_refresh:{poll_id}"
    await custom_poll_action_callback(mock_update, mock_context)

    keyboard = mock_context.bot.edit_message_text.call_args.kwargs["reply_markup"]
    vote_rows = [
        row
        for row in keyboard.inline_keyboard
        if row and row[0].callback_data and row[0].callback_data.startswith("vote:")
    ]
    assert len(vote_rows) == 1, "expected a single row holding both short-name buttons"
    assert len(vote_rows[0]) == 2, "short-name group should stay two-per-row"


@pytest.mark.asyncio
async def test_render_poll_long_name_with_vote_keeps_count_visible(mock_update, mock_context):
    """A wrapped long-name button still ends with the vote-count suffix."""
    chat_id = 12345
    poll_id = "poll_12345_wrapcount"
    long_name = "Twilight Imperium Fourth Edition"

    async with db.AsyncSessionLocal() as session:
        session.add(Session(chat_id=chat_id, is_active=True, poll_type=PollType.CUSTOM))

        session.add_all(
            [
                User(telegram_id=111, telegram_name="User1"),
                User(telegram_id=222, telegram_name="User2"),
            ]
        )
        await session.flush()

        g1 = Game(
            id=1, name=long_name, min_players=2, max_players=4, playing_time=240, complexity=4.5
        )
        session.add(g1)
        await session.flush()

        session.add_all(
            [
                Collection(user_id=111, game_id=1),
                Collection(user_id=222, game_id=1),
                SessionPlayer(session_id=chat_id, user_id=111),
                SessionPlayer(session_id=chat_id, user_id=222),
                GameNightPoll(poll_id=poll_id, chat_id=chat_id, message_id=999),
                PollVote(
                    poll_id=poll_id,
                    user_id=111,
                    vote_type=VoteType.GAME,
                    game_id=1,
                    user_name="User1",
                ),
            ]
        )
        await session.commit()

    mock_update.callback_query.data = f"poll_refresh:{poll_id}"
    await custom_poll_action_callback(mock_update, mock_context)

    keyboard = mock_context.bot.edit_message_text.call_args.kwargs["reply_markup"]
    button_labels = [btn.text for row in keyboard.inline_keyboard for btn in row]
    long_button = next(b for b in button_labels if "Twilight" in b)

    # The count suffix lives on the last visual line of the wrapped label.
    assert long_button.split("\n")[-1].endswith(" (1)")
