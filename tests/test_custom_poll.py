"""
Tests for the game-night poll: one interactive message for all valid games.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

import src.bot.handlers as handlers
from src.bot.handlers import (
    _render_poll_job,
    _wrap_button_label,
    create_poll,
    custom_poll_action_callback,
    join_lobby_callback,
    poll_add_select_callback,
    poll_settings_callback,
    start_poll_callback,
    toggle_allow_adding_callback,
    toggle_hide_results_callback,
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
async def test_poll_settings_no_session_returns_early(mock_update, mock_context):
    """Test poll settings returns early when no session exists."""
    mock_update.callback_query.message.chat.id = 99999  # Non-existent

    await poll_settings_callback(mock_update, mock_context)

    mock_update.callback_query.edit_message_text.assert_not_called()


# ============================================================================
# Custom Poll Creation Tests
# ============================================================================


@pytest.mark.asyncio
async def test_create_poll_custom_mode_creates_poll(mock_update, mock_context):
    """Test /poll in custom mode creates a GameNightPoll and sends message."""
    chat_id = 12345

    async with db.AsyncSessionLocal() as session:
        # Custom mode session
        session.add(Session(chat_id=chat_id, is_active=True))

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

    # The poll starts life as a plain message that is then edited.
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
        session.add(Session(chat_id=chat_id, is_active=True))

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

    # Games are voted on from each player's ballot; the message links to it
    keyboard = call_kwargs.get("reply_markup")
    assert keyboard is not None
    vote_button = keyboard.inline_keyboard[0][0]
    assert vote_button.url.startswith("https://t.me/test_bot?start=b_poll_")


# ============================================================================
# Custom Poll Actions Tests
# ============================================================================


@pytest.mark.asyncio
async def test_custom_poll_refresh(mock_update, mock_context):
    """Test refresh action re-renders the poll."""
    chat_id = 12345
    poll_id = "poll_12345_123456"

    async with db.AsyncSessionLocal() as session:
        session.add(Session(chat_id=chat_id, is_active=True))

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
        session.add(Session(chat_id=chat_id, is_active=True))

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
        session.add(Session(chat_id=chat_id, is_active=True))

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
        session.add(Session(chat_id=chat_id, is_active=True))

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
        session.add(Session(chat_id=chat_id, is_active=True))

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
        session.add(Session(chat_id=chat_id, is_active=True, settings_weighted=True))

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
    player_ids = list(dict.fromkeys(voter_ids + [111, 222]))[: max(2, len(voter_ids))]

    async with db.AsyncSessionLocal() as session:
        session.add(Session(chat_id=chat_id, is_active=True))

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
async def test_auto_close_previous_polls(mock_update, mock_context):
    """Test that starting a new poll closes existing ones."""

    chat_id = 66666
    old_poll_id = f"old_{chat_id}"

    # Setup: Active Custom Poll
    async with db.AsyncSessionLocal() as session:
        session.add(Session(chat_id=chat_id, is_active=True))
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
    mock_context.bot.edit_message_text = AsyncMock()
    mock_context.bot.send_message = AsyncMock(
        return_value=MagicMock(message_id=999)
    )  # For new poll

    await start_poll_callback(mock_update, mock_context)

    # The old poll's row is gone
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
        session.add(Session(chat_id=chat_id, is_active=True))
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
    assert "Poll Active" in refresh_call.kwargs["text"]


# ============================================================================
# New Settings Toggle Tests (API 9.6 Migration)
# ============================================================================


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
        session.add(Session(chat_id=chat_id, is_active=True, hide_results=True))

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
        session.add(Session(chat_id=chat_id, is_active=True, hide_results=True))

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


# ============================================================================
# Debounced Render (Vote Coalescing) Tests
# ============================================================================


class _FakeJob:
    def __init__(self, name, data):
        self.name = name
        self.data = data
        self.removed = False

    def schedule_removal(self):
        self.removed = True


class _FakeJobQueue:
    """Minimal stand-in for telegram.ext.JobQueue."""

    def __init__(self):
        self.jobs: list[_FakeJob] = []

    def get_jobs_by_name(self, name):
        return tuple(j for j in self.jobs if j.name == name and not j.removed)

    def run_once(self, callback, when, name, data):  # noqa: ARG002 - signature parity
        self.jobs.append(_FakeJob(name, data))

    def pending(self, name):
        return [j for j in self.jobs if j.name == name and not j.removed]


async def _seed_basic_poll(chat_id, poll_id, game_id, user_id, message_id=999):
    async with db.AsyncSessionLocal() as session:
        session.add(Session(chat_id=chat_id, is_active=True))
        session.add_all(
            [
                User(telegram_id=user_id, telegram_name="Voter"),
                User(telegram_id=user_id + 1, telegram_name="Other"),
            ]
        )
        await session.flush()
        session.add(
            Game(
                id=game_id,
                name="TestGame",
                min_players=2,
                max_players=4,
                playing_time=60,
                complexity=2.0,
            )
        )
        await session.flush()
        session.add_all(
            [
                Collection(user_id=user_id, game_id=game_id),
                Collection(user_id=user_id + 1, game_id=game_id),
                SessionPlayer(session_id=chat_id, user_id=user_id),
                SessionPlayer(session_id=chat_id, user_id=user_id + 1),
                GameNightPoll(poll_id=poll_id, chat_id=chat_id, message_id=message_id),
            ]
        )
        await session.commit()


@pytest.mark.asyncio
async def test_render_job_edits_message(mock_context):
    """The debounced job renders the live poll state to the message."""
    chat_id, poll_id, game_id, user_id = 12345, "poll_12345_3", 1, 111
    await _seed_basic_poll(chat_id, poll_id, game_id, user_id)

    mock_context.job = MagicMock()
    mock_context.job.data = {"chat_id": chat_id, "message_id": 999, "poll_id": poll_id}

    await _render_poll_job(mock_context)

    mock_context.bot.edit_message_text.assert_called_once()


@pytest.mark.asyncio
async def test_render_job_skips_closed_poll(mock_context):
    """If the poll was closed (record gone) during the debounce window, the job
    is a no-op so it can't clobber the results message."""
    chat_id, poll_id = 12345, "poll_does_not_exist"

    mock_context.job = MagicMock()
    mock_context.job.data = {"chat_id": chat_id, "message_id": 999, "poll_id": poll_id}

    await _render_poll_job(mock_context)

    mock_context.bot.edit_message_text.assert_not_called()


def test_clamp_message_text_leaves_short_text_alone():
    lines = ["📊 **Poll Active**", "**3** - Brass Birmingham", "   └ _Joao, Maria_"]
    assert handlers._clamp_message_text(lines) == "\n".join(lines)


def test_clamp_message_text_truncates_a_long_tally():
    """A big tally must not exceed Telegram's text limit: the whole edit —
    keyboard included — is rejected with Message_too_long, freezing the poll."""
    lines = [f"**{i}** - Some Rather Long Game Name Here" for i in range(400)]
    text = handlers._clamp_message_text(lines)

    assert len(text) <= handlers._MESSAGE_TEXT_LIMIT
    assert "truncated" in text
    # Keeps the top of the tally, drops the tail.
    assert text.startswith("**0** - ")
    # Cuts only on line boundaries, so Markdown emphasis is never split.
    assert all(line.count("**") % 2 == 0 for line in text.split("\n") if "**" in line)
