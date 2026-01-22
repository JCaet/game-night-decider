"""
Tests for the Vote Limit feature.

Tests the configurable vote limit per player in polls.
"""

import pytest
from sqlalchemy import select

from src.bot.handlers import (
    calculate_auto_vote_limit,
    custom_poll_vote_callback,
    cycle_vote_limit_callback,
    get_vote_limit_display,
    poll_settings_callback,
)
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
    VoteLimit,
)

# ============================================================================
# Auto Limit Calculation Tests
# ============================================================================


def test_calculate_auto_vote_limit_small_game_counts():
    """Test auto limit formula returns minimum of 3 for small counts."""
    assert calculate_auto_vote_limit(1) == 3
    assert calculate_auto_vote_limit(2) == 3
    assert calculate_auto_vote_limit(4) == 3
    assert calculate_auto_vote_limit(8) == 3


def test_calculate_auto_vote_limit_medium_game_counts():
    """Test auto limit formula scales correctly for medium counts."""
    # log2(9) = ~3.17 -> ceil = 4
    assert calculate_auto_vote_limit(9) == 4
    # log2(16) = 4
    assert calculate_auto_vote_limit(16) == 4
    # log2(17) = ~4.09 -> ceil = 5
    assert calculate_auto_vote_limit(17) == 5


def test_calculate_auto_vote_limit_large_game_counts():
    """Test auto limit formula for larger game counts."""
    # log2(32) = 5
    assert calculate_auto_vote_limit(32) == 5
    # log2(33) = ~5.04 -> ceil = 6
    assert calculate_auto_vote_limit(33) == 6
    # log2(64) = 6
    assert calculate_auto_vote_limit(64) == 6


def test_calculate_auto_vote_limit_zero_or_negative():
    """Test auto limit returns 3 for edge cases."""
    assert calculate_auto_vote_limit(0) == 3
    assert calculate_auto_vote_limit(-1) == 3


# ============================================================================
# Vote Limit Display Tests
# ============================================================================


def test_get_vote_limit_display_auto():
    """Test display text for AUTO mode."""
    display = get_vote_limit_display(VoteLimit.AUTO, game_count=16)
    assert "Auto" in display
    assert "(4)" in display  # log2(16) = 4


def test_get_vote_limit_display_auto_no_game_count():
    """Test display text for AUTO without game count."""
    display = get_vote_limit_display(VoteLimit.AUTO, game_count=0)
    assert "Auto" in display
    assert "?" in display


def test_get_vote_limit_display_unlimited():
    """Test display text for UNLIMITED mode."""
    display = get_vote_limit_display(VoteLimit.UNLIMITED)
    assert display == "Unlimited"


def test_get_vote_limit_display_static():
    """Test display text for static values."""
    assert get_vote_limit_display(3) == "3"
    assert get_vote_limit_display(5) == "5"
    assert get_vote_limit_display(7) == "7"
    assert get_vote_limit_display(10) == "10"


# ============================================================================
# Vote Limit Cycle Tests
# ============================================================================


@pytest.mark.asyncio
async def test_cycle_vote_limit_changes_setting(mock_update, mock_context):
    """Test cycling vote limit updates the session."""
    chat_id = 12345

    async with db.AsyncSessionLocal() as session:
        session.add(
            Session(
                chat_id=chat_id,
                is_active=True,
                poll_type=PollType.CUSTOM,
                vote_limit=VoteLimit.AUTO,
            )
        )
        await session.commit()

    mock_update.callback_query.message.chat.id = chat_id

    await cycle_vote_limit_callback(mock_update, mock_context)

    # Verify limit changed to next option (AUTO -> 3)
    async with db.AsyncSessionLocal() as session:
        sess = await session.get(Session, chat_id)
        assert sess.vote_limit == 3


@pytest.mark.asyncio
async def test_cycle_vote_limit_wraps_around(mock_update, mock_context):
    """Test vote limit cycles back to AUTO after UNLIMITED."""
    chat_id = 12345

    async with db.AsyncSessionLocal() as session:
        session.add(
            Session(
                chat_id=chat_id,
                is_active=True,
                poll_type=PollType.CUSTOM,
                vote_limit=VoteLimit.UNLIMITED,  # Last option
            )
        )
        await session.commit()

    mock_update.callback_query.message.chat.id = chat_id

    await cycle_vote_limit_callback(mock_update, mock_context)

    # Should wrap to AUTO
    async with db.AsyncSessionLocal() as session:
        sess = await session.get(Session, chat_id)
        assert sess.vote_limit == VoteLimit.AUTO


# ============================================================================
# Vote Limit Enforcement Tests
# ============================================================================


@pytest.mark.asyncio
async def test_vote_limit_enforced_when_exceeded(mock_update, mock_context):
    """Test that voting is blocked when user reaches vote limit."""
    chat_id = 12345
    poll_id = "poll_limit_test"
    user_id = 111

    async with db.AsyncSessionLocal() as session:
        session.add(
            Session(
                chat_id=chat_id,
                is_active=True,
                poll_type=PollType.CUSTOM,
                vote_limit=3,  # Static limit of 3
            )
        )

        u1 = User(telegram_id=user_id, telegram_name="Voter")
        u2 = User(telegram_id=222, telegram_name="Other")
        session.add_all([u1, u2])
        await session.flush()

        # Create 3 games
        games = [
            Game(
                id=i, name=f"Game{i}", min_players=2, max_players=4, playing_time=60, complexity=2.0
            )
            for i in range(1, 5)
        ]
        session.add_all(games)
        await session.flush()

        # Add to collections
        for g in games:
            session.add(Collection(user_id=user_id, game_id=g.id))
            session.add(Collection(user_id=222, game_id=g.id))

        session.add(SessionPlayer(session_id=chat_id, user_id=user_id))
        session.add(SessionPlayer(session_id=chat_id, user_id=222))

        # Create poll
        session.add(GameNightPoll(poll_id=poll_id, chat_id=chat_id, message_id=999))

        # User has already voted 3 times (at limit)
        for i in range(1, 4):
            session.add(PollVote(poll_id=poll_id, user_id=user_id, game_id=i, user_name="Voter"))

        await session.commit()

    # Try to vote for game 4 (should be blocked)
    mock_update.callback_query.data = f"vote:{poll_id}:4"
    mock_update.callback_query.from_user.id = user_id
    mock_update.callback_query.from_user.first_name = "Voter"

    await custom_poll_vote_callback(mock_update, mock_context)

    # Verify error alert was shown
    mock_update.callback_query.answer.assert_called()
    call_args = mock_update.callback_query.answer.call_args
    assert "limit reached" in call_args.kwargs.get("", "") or "limit reached" in str(call_args)


@pytest.mark.asyncio
async def test_vote_limit_unlimited_allows_all(mock_update, mock_context):
    """Test that UNLIMITED mode allows voting for all games."""
    chat_id = 12345
    poll_id = "poll_unlimited_test"
    user_id = 111

    async with db.AsyncSessionLocal() as session:
        session.add(
            Session(
                chat_id=chat_id,
                is_active=True,
                poll_type=PollType.CUSTOM,
                vote_limit=VoteLimit.UNLIMITED,
            )
        )

        u1 = User(telegram_id=user_id, telegram_name="Voter")
        session.add(u1)
        await session.flush()

        # Create 5 games
        games = [
            Game(
                id=i, name=f"Game{i}", min_players=1, max_players=4, playing_time=60, complexity=2.0
            )
            for i in range(1, 6)
        ]
        session.add_all(games)
        await session.flush()

        for g in games:
            session.add(Collection(user_id=user_id, game_id=g.id))

        session.add(SessionPlayer(session_id=chat_id, user_id=user_id))
        session.add(GameNightPoll(poll_id=poll_id, chat_id=chat_id, message_id=999))

        # Vote for first 4 games
        for i in range(1, 5):
            session.add(PollVote(poll_id=poll_id, user_id=user_id, game_id=i, user_name="Voter"))

        await session.commit()

    # Vote for 5th game (should succeed with UNLIMITED)
    mock_update.callback_query.data = f"vote:{poll_id}:5"
    mock_update.callback_query.from_user.id = user_id
    mock_update.callback_query.from_user.first_name = "Voter"

    await custom_poll_vote_callback(mock_update, mock_context)

    # Verify vote was recorded
    async with db.AsyncSessionLocal() as session:
        votes = (
            (
                await session.execute(
                    select(PollVote).where(PollVote.poll_id == poll_id, PollVote.user_id == user_id)
                )
            )
            .scalars()
            .all()
        )

        assert len(votes) == 5


@pytest.mark.asyncio
async def test_vote_removal_allows_new_vote(mock_update, mock_context):
    """Test that removing a vote allows adding a new one at limit."""
    chat_id = 12345
    poll_id = "poll_remove_test"
    user_id = 111

    async with db.AsyncSessionLocal() as session:
        session.add(
            Session(
                chat_id=chat_id,
                is_active=True,
                poll_type=PollType.CUSTOM,
                vote_limit=2,  # Low limit
            )
        )

        u1 = User(telegram_id=user_id, telegram_name="Voter")
        session.add(u1)
        await session.flush()

        g1 = Game(id=1, name="Game1", min_players=1, max_players=4, playing_time=60, complexity=2.0)
        g2 = Game(id=2, name="Game2", min_players=1, max_players=4, playing_time=60, complexity=2.0)
        g3 = Game(id=3, name="Game3", min_players=1, max_players=4, playing_time=60, complexity=2.0)
        session.add_all([g1, g2, g3])
        await session.flush()

        for g in [g1, g2, g3]:
            session.add(Collection(user_id=user_id, game_id=g.id))

        session.add(SessionPlayer(session_id=chat_id, user_id=user_id))
        session.add(GameNightPoll(poll_id=poll_id, chat_id=chat_id, message_id=999))

        # At limit: 2 votes
        session.add(PollVote(poll_id=poll_id, user_id=user_id, game_id=1, user_name="Voter"))
        session.add(PollVote(poll_id=poll_id, user_id=user_id, game_id=2, user_name="Voter"))

        await session.commit()

    # Remove vote for game 1
    mock_update.callback_query.data = f"vote:{poll_id}:1"
    mock_update.callback_query.from_user.id = user_id
    mock_update.callback_query.from_user.first_name = "Voter"

    await custom_poll_vote_callback(mock_update, mock_context)

    # Verify removal
    mock_update.callback_query.answer.assert_called_with("Vote removed")

    # Now vote for game 3 should succeed
    mock_update.callback_query.data = f"vote:{poll_id}:3"

    await custom_poll_vote_callback(mock_update, mock_context)

    # Verify new vote recorded
    async with db.AsyncSessionLocal() as session:
        votes = (
            (
                await session.execute(
                    select(PollVote).where(PollVote.poll_id == poll_id, PollVote.user_id == user_id)
                )
            )
            .scalars()
            .all()
        )

        game_ids = [v.game_id for v in votes]
        assert 1 not in game_ids  # Removed
        assert 2 in game_ids
        assert 3 in game_ids


# ============================================================================
# Poll Settings UI Tests
# ============================================================================


@pytest.mark.asyncio
async def test_poll_settings_shows_vote_limit_button(mock_update, mock_context):
    """Test that poll settings menu includes vote limit button."""
    chat_id = 12345

    async with db.AsyncSessionLocal() as session:
        session.add(Session(chat_id=chat_id, is_active=True, vote_limit=VoteLimit.AUTO))
        await session.commit()

    mock_update.callback_query.message.chat.id = chat_id

    await poll_settings_callback(mock_update, mock_context)

    # Check button exists
    call_kwargs = mock_update.callback_query.edit_message_text.call_args.kwargs
    reply_markup = call_kwargs.get("reply_markup")
    assert reply_markup is not None

    buttons = [btn for row in reply_markup.inline_keyboard for btn in row]
    btn_texts = [btn.text for btn in buttons]

    # Should have a vote limit button
    assert any("Vote Limit" in t for t in btn_texts)
