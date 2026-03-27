"""
Tests for vote pruning when player count changes.

When a player joins or leaves mid-poll, the list of valid games may change
(filtered by player count). Votes for removed games become "stale" and must
be pruned so they don't block the vote limit.
"""

import pytest
from sqlalchemy import select

from src.bot.handlers import join_lobby_callback, leave_lobby_callback
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
    VoteType,
)

# ============================================================================
# Helper to set up common test data
# ============================================================================


async def _setup_poll_scenario(
    *,
    chat_id=12345,
    poll_id="poll_prune_test",
    games=None,
    players=None,
    votes=None,
    vote_limit=3,
):
    """
    Create a session with players, games, a custom poll, and votes.

    Args:
        games: list of dicts with id, name, min_players, max_players, complexity
        players: list of user_id ints
        votes: list of dicts with user_id, game_id
    """
    if games is None:
        games = []
    if players is None:
        players = []
    if votes is None:
        votes = []

    async with db.AsyncSessionLocal() as session:
        session.add(
            Session(
                chat_id=chat_id,
                is_active=True,
                poll_type=PollType.CUSTOM,
                vote_limit=vote_limit,
                message_id=888,
            )
        )

        for uid in players:
            # Create user if referenced
            session.add(User(telegram_id=uid, telegram_name=f"User{uid}"))

        await session.flush()

        for uid in players:
            session.add(SessionPlayer(session_id=chat_id, user_id=uid))

        for g in games:
            session.add(
                Game(
                    id=g["id"],
                    name=g["name"],
                    min_players=g.get("min_players", 2),
                    max_players=g.get("max_players", 6),
                    playing_time=60,
                    complexity=g.get("complexity", 2.0),
                )
            )

        await session.flush()

        # Add all games to first player's collection
        for g in games:
            for uid in players:
                session.add(Collection(user_id=uid, game_id=g["id"]))

        session.add(GameNightPoll(poll_id=poll_id, chat_id=chat_id, message_id=999))

        for v in votes:
            session.add(
                PollVote(
                    poll_id=poll_id,
                    user_id=v["user_id"],
                    vote_type=v.get("vote_type", VoteType.GAME),
                    game_id=v.get("game_id"),
                    category_level=v.get("category_level"),
                    user_name=f"User{v['user_id']}",
                )
            )

        await session.commit()


# ============================================================================
# Test: Join prunes stale votes
# ============================================================================


@pytest.mark.asyncio
async def test_join_prunes_stale_votes(mock_update, mock_context):
    """Player has votes on 4-max games. 5th player joins -> those games removed -> votes pruned."""
    chat_id = 12345
    poll_id = "poll_prune_test"

    # Games: two support 2-4 players, two support 2-6 players
    await _setup_poll_scenario(
        chat_id=chat_id,
        poll_id=poll_id,
        games=[
            {"id": 1, "name": "SmallGame1", "min_players": 2, "max_players": 4},
            {"id": 2, "name": "SmallGame2", "min_players": 2, "max_players": 4},
            {"id": 3, "name": "BigGame1", "min_players": 2, "max_players": 6},
            {"id": 4, "name": "BigGame2", "min_players": 2, "max_players": 6},
        ],
        players=[111, 222, 333, 444],  # 4 players initially
        votes=[
            {"user_id": 111, "game_id": 1},  # Vote on 4-max game
            {"user_id": 111, "game_id": 2},  # Vote on 4-max game
            {"user_id": 111, "game_id": 3},  # Vote on 6-max game
        ],
        vote_limit=3,
    )

    # 5th player joins -> SmallGame1 and SmallGame2 (max 4) should be removed
    # Create the 5th user
    async with db.AsyncSessionLocal() as session:
        session.add(User(telegram_id=555, telegram_name="User555"))
        await session.commit()

    # Configure mock for join
    mock_update.callback_query.from_user.id = 555
    mock_update.callback_query.from_user.first_name = "User555"
    mock_update.callback_query.message.chat.id = chat_id
    mock_update.callback_query.message.message_id = 888

    await join_lobby_callback(mock_update, mock_context)

    # Verify: votes for game 1 and 2 should be pruned, vote for game 3 survives
    async with db.AsyncSessionLocal() as session:
        remaining_votes = (
            (await session.execute(select(PollVote).where(PollVote.poll_id == poll_id)))
            .scalars()
            .all()
        )

        remaining_game_ids = [v.game_id for v in remaining_votes]
        assert 1 not in remaining_game_ids, "Vote for SmallGame1 should be pruned"
        assert 2 not in remaining_game_ids, "Vote for SmallGame2 should be pruned"
        assert 3 in remaining_game_ids, "Vote for BigGame1 should survive"
        assert len(remaining_votes) == 1


# ============================================================================
# Test: Leave prunes stale votes
# ============================================================================


@pytest.mark.asyncio
async def test_leave_prunes_stale_votes(mock_update, mock_context):
    """Player leaves -> games with higher min_players removed -> stale votes pruned."""
    chat_id = 12345
    poll_id = "poll_prune_test"

    # Games: one requires min 3 players, one requires min 2
    await _setup_poll_scenario(
        chat_id=chat_id,
        poll_id=poll_id,
        games=[
            {"id": 1, "name": "BigGroupGame", "min_players": 3, "max_players": 6},
            {"id": 2, "name": "SmallGroupGame", "min_players": 2, "max_players": 6},
        ],
        players=[111, 222, 333],  # 3 players
        votes=[
            {"user_id": 111, "game_id": 1},  # Vote on min-3 game
            {"user_id": 222, "game_id": 1},  # Vote on min-3 game
            {"user_id": 111, "game_id": 2},  # Vote on min-2 game
        ],
        vote_limit=VoteLimit.UNLIMITED,
    )

    # Player 333 leaves -> only 2 players -> BigGroupGame (min 3) invalid
    mock_update.callback_query.from_user.id = 333
    mock_update.callback_query.from_user.first_name = "User333"
    mock_update.callback_query.message.chat.id = chat_id
    mock_update.callback_query.message.message_id = 888

    await leave_lobby_callback(mock_update, mock_context)

    # Verify: votes for game 1 pruned, vote for game 2 survives
    async with db.AsyncSessionLocal() as session:
        remaining_votes = (
            (await session.execute(select(PollVote).where(PollVote.poll_id == poll_id)))
            .scalars()
            .all()
        )

        remaining_game_ids = [v.game_id for v in remaining_votes]
        assert 1 not in remaining_game_ids, "Votes for BigGroupGame should be pruned"
        assert 2 in remaining_game_ids, "Vote for SmallGroupGame should survive"
        assert len(remaining_votes) == 1


# ============================================================================
# Test: Leave auto-refreshes poll
# ============================================================================


@pytest.mark.asyncio
async def test_leave_auto_refreshes_poll(mock_update, mock_context):
    """Verify render_poll_message is called when a player leaves with an active custom poll."""
    chat_id = 12345
    poll_id = "poll_prune_test"

    await _setup_poll_scenario(
        chat_id=chat_id,
        poll_id=poll_id,
        games=[
            {"id": 1, "name": "Game1", "min_players": 2, "max_players": 6},
        ],
        players=[111, 222, 333],
        votes=[],
    )

    mock_update.callback_query.from_user.id = 333
    mock_update.callback_query.from_user.first_name = "User333"
    mock_update.callback_query.message.chat.id = chat_id
    mock_update.callback_query.message.message_id = 888

    mock_context.bot.edit_message_text.reset_mock()

    await leave_lobby_callback(mock_update, mock_context)

    # context.bot.edit_message_text should be called at least once for the poll refresh
    # (the lobby message update goes through query.edit_message_text, not bot.edit_message_text)
    assert mock_context.bot.edit_message_text.call_count >= 1, (
        "Poll should be auto-refreshed on leave"
    )


# ============================================================================
# Test: Prune notification sent
# ============================================================================


@pytest.mark.asyncio
async def test_prune_notification_sent(mock_update, mock_context):
    """Verify send_message is called with stale vote notification text."""
    chat_id = 12345
    poll_id = "poll_prune_test"

    await _setup_poll_scenario(
        chat_id=chat_id,
        poll_id=poll_id,
        games=[
            {"id": 1, "name": "SmallGame", "min_players": 2, "max_players": 4},
            {"id": 2, "name": "BigGame", "min_players": 2, "max_players": 6},
        ],
        players=[111, 222, 333, 444],
        votes=[
            {"user_id": 111, "game_id": 1},  # Will become stale
        ],
        vote_limit=3,
    )

    # 5th player joins -> SmallGame removed -> 1 stale vote
    async with db.AsyncSessionLocal() as session:
        session.add(User(telegram_id=555, telegram_name="User555"))
        await session.commit()

    mock_update.callback_query.from_user.id = 555
    mock_update.callback_query.from_user.first_name = "User555"
    mock_update.callback_query.message.chat.id = chat_id
    mock_update.callback_query.message.message_id = 888

    mock_context.bot.send_message.reset_mock()

    await join_lobby_callback(mock_update, mock_context)

    # Check that a notification about stale votes was sent
    send_calls = mock_context.bot.send_message.call_args_list
    stale_notification = [
        c for c in send_calls
        if "stale vote" in str(c).lower()
    ]
    assert len(stale_notification) > 0, "Should send notification about pruned stale votes"


# ============================================================================
# Test: No prune when no stale votes
# ============================================================================


@pytest.mark.asyncio
async def test_no_prune_when_no_stale_votes(mock_update, mock_context):
    """Player joins but all votes remain valid -> no notification, no deletions."""
    chat_id = 12345
    poll_id = "poll_prune_test"

    # All games support up to 6 players, so a 5th player won't invalidate anything
    await _setup_poll_scenario(
        chat_id=chat_id,
        poll_id=poll_id,
        games=[
            {"id": 1, "name": "Game1", "min_players": 2, "max_players": 6},
            {"id": 2, "name": "Game2", "min_players": 2, "max_players": 6},
        ],
        players=[111, 222, 333, 444],
        votes=[
            {"user_id": 111, "game_id": 1},
            {"user_id": 111, "game_id": 2},
        ],
        vote_limit=3,
    )

    async with db.AsyncSessionLocal() as session:
        session.add(User(telegram_id=555, telegram_name="User555"))
        await session.commit()

    mock_update.callback_query.from_user.id = 555
    mock_update.callback_query.from_user.first_name = "User555"
    mock_update.callback_query.message.chat.id = chat_id
    mock_update.callback_query.message.message_id = 888

    mock_context.bot.send_message.reset_mock()

    await join_lobby_callback(mock_update, mock_context)

    # All votes should survive
    async with db.AsyncSessionLocal() as session:
        remaining_votes = (
            (await session.execute(select(PollVote).where(PollVote.poll_id == poll_id)))
            .scalars()
            .all()
        )
        assert len(remaining_votes) == 2, "All votes should survive"

    # No "stale vote" notification should be sent
    send_calls = mock_context.bot.send_message.call_args_list
    stale_notification = [
        c for c in send_calls
        if "stale vote" in str(c).lower()
    ]
    assert len(stale_notification) == 0, "Should NOT send stale vote notification"
