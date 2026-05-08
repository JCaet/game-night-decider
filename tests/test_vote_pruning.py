"""
Tests for vote suspension when player count changes.

When a player joins or leaves mid-poll, the list of valid games may change
(filtered by player count).  Votes for removed games become "suspended":
they remain in the database so they can resume automatically if the game
becomes eligible again (e.g. the player who caused the count change leaves).
"""

import pytest
from sqlalchemy import select

from src.bot.handlers import join_lobby_callback, leave_lobby_callback
from src.core import db
from src.core.logic import group_games_by_complexity
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
from src.core.poll_service import PollService

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

        # Add all games to every player's collection
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
# Test: Join suspends (not deletes) stale votes
# ============================================================================


@pytest.mark.asyncio
async def test_join_suspends_stale_votes(mock_update, mock_context):
    """Player has votes on 4-max games. 5th player joins -> those games removed.
    Votes MUST remain in the DB (suspended, not deleted)."""
    chat_id = 12345
    poll_id = "poll_prune_test"

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

    # 5th player joins -> SmallGame1 and SmallGame2 (max 4) suspended
    async with db.AsyncSessionLocal() as session:
        session.add(User(telegram_id=555, telegram_name="User555"))
        await session.commit()

    mock_update.callback_query.from_user.id = 555
    mock_update.callback_query.from_user.first_name = "User555"
    mock_update.callback_query.message.chat.id = chat_id
    mock_update.callback_query.message.message_id = 888

    await join_lobby_callback(mock_update, mock_context)

    # All 3 votes must still be in the database — none deleted
    async with db.AsyncSessionLocal() as session:
        all_votes = (
            (await session.execute(select(PollVote).where(PollVote.poll_id == poll_id)))
            .scalars()
            .all()
        )
        game_ids_in_db = {v.game_id for v in all_votes}

        assert 1 in game_ids_in_db, "Vote for SmallGame1 must survive (suspended, not deleted)"
        assert 2 in game_ids_in_db, "Vote for SmallGame2 must survive (suspended, not deleted)"
        assert 3 in game_ids_in_db, "Vote for BigGame1 must survive"
        assert len(all_votes) == 3


# ============================================================================
# Test: Leave suspends (not deletes) stale votes
# ============================================================================


@pytest.mark.asyncio
async def test_leave_suspends_stale_votes(mock_update, mock_context):
    """Player leaves, reducing count -> games with higher min_players removed.
    Votes MUST remain in the DB (suspended, not deleted)."""
    chat_id = 12345
    poll_id = "poll_prune_test"

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

    # Player 333 leaves -> only 2 players -> BigGroupGame (min 3) suspended
    mock_update.callback_query.from_user.id = 333
    mock_update.callback_query.from_user.first_name = "User333"
    mock_update.callback_query.message.chat.id = chat_id
    mock_update.callback_query.message.message_id = 888

    await leave_lobby_callback(mock_update, mock_context)

    # All 3 votes must still be in the database
    async with db.AsyncSessionLocal() as session:
        all_votes = (
            (await session.execute(select(PollVote).where(PollVote.poll_id == poll_id)))
            .scalars()
            .all()
        )
        game_ids_in_db = {v.game_id for v in all_votes}

        assert 1 in game_ids_in_db, "Votes for BigGroupGame must survive (suspended)"
        assert 2 in game_ids_in_db, "Vote for SmallGroupGame must survive"
        assert len(all_votes) == 3


# ============================================================================
# Test: KEY SCENARIO — join then leave restores votes
# ============================================================================


@pytest.mark.asyncio
async def test_votes_restored_after_player_joins_then_leaves(mock_update, mock_context):
    """
    Core regression test for the join-then-leave vote loss bug.

    Timeline:
      1. 4 players. User 111 votes for SmallGame (max 4).
      2. 5th player joins -> SmallGame suspended -> vote stays in DB.
      3. 5th player leaves -> SmallGame eligible again.
      4. The vote for SmallGame must be counted again (it was never lost).
    """
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
            {"user_id": 111, "game_id": 1},  # The vote that must survive
            {"user_id": 222, "game_id": 2},
        ],
        vote_limit=VoteLimit.UNLIMITED,
    )

    # Step 2: 5th player joins
    async with db.AsyncSessionLocal() as session:
        session.add(User(telegram_id=555, telegram_name="User555"))
        await session.commit()

    mock_update.callback_query.from_user.id = 555
    mock_update.callback_query.from_user.first_name = "User555"
    mock_update.callback_query.message.chat.id = chat_id
    mock_update.callback_query.message.message_id = 888
    await join_lobby_callback(mock_update, mock_context)

    # Verify vote still in DB after join (suspended)
    async with db.AsyncSessionLocal() as session:
        votes = (
            (await session.execute(select(PollVote).where(PollVote.poll_id == poll_id)))
            .scalars()
            .all()
        )
        assert any(v.game_id == 1 for v in votes), (
            "Vote for SmallGame must remain in DB after join (suspended)"
        )

    # Step 3: 5th player leaves
    mock_update.callback_query.from_user.id = 555
    mock_update.callback_query.from_user.first_name = "User555"
    await leave_lobby_callback(mock_update, mock_context)

    # Step 4: SmallGame must be valid again and its vote must be counted
    async with db.AsyncSessionLocal() as session:
        from src.bot.handlers import get_session_valid_games

        valid_games, _ = await get_session_valid_games(session, chat_id)
        valid_game_ids = {g.id for g in valid_games}

        assert 1 in valid_game_ids, "SmallGame must be valid again after 5th player leaves"

        remaining_votes = (
            (await session.execute(select(PollVote).where(PollVote.poll_id == poll_id)))
            .scalars()
            .all()
        )
        small_game_votes = [v for v in remaining_votes if v.game_id == 1]
        assert len(small_game_votes) == 1, (
            "Vote for SmallGame must be in DB and countable after player leaves"
        )
        assert small_game_votes[0].user_id == 111


# ============================================================================
# Test: Render excludes suspended game votes from displayed totals
# ============================================================================


@pytest.mark.asyncio
async def test_render_does_not_count_suspended_game_votes(mock_update, mock_context):
    """render_poll_message must not count a vote whose game is not in the
    current valid_games list — even though the vote is still in the DB."""
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
            {"user_id": 111, "game_id": 1},  # Will be suspended when 5th joins
            {"user_id": 111, "game_id": 2},
        ],
        vote_limit=VoteLimit.UNLIMITED,
    )

    # 5th player joins — SmallGame suspended, vote stays in DB
    async with db.AsyncSessionLocal() as session:
        session.add(User(telegram_id=555, telegram_name="User555"))
        await session.commit()

    mock_update.callback_query.from_user.id = 555
    mock_update.callback_query.from_user.first_name = "User555"
    mock_update.callback_query.message.chat.id = chat_id
    mock_update.callback_query.message.message_id = 888
    await join_lobby_callback(mock_update, mock_context)

    # Capture the edit_message_text call made by render_poll_message
    calls = mock_context.bot.edit_message_text.call_args_list
    assert calls, "render_poll_message should have been called"

    last_render_text = calls[-1].kwargs.get("text") or calls[-1].args[0]

    # SmallGame must NOT appear in the render (not valid with 5 players)
    assert "SmallGame" not in last_render_text, "SmallGame should not appear in poll when suspended"

    # Only 1 active vote (BigGame) must be reflected in the count header
    assert "1 votes" in last_render_text or "1 vote" in last_render_text, (
        "Only the active BigGame vote should be counted in the total"
    )


# ============================================================================
# Test: Close poll ignores suspended game votes in winner calculation
# ============================================================================


@pytest.mark.asyncio
async def test_close_poll_ignores_suspended_game_votes():
    """When closing a poll, votes for games not in valid_games must not
    influence the winner — even when those votes are still in the DB."""
    chat_id = 12345
    poll_id = "poll_close_suspended"

    await _setup_poll_scenario(
        chat_id=chat_id,
        poll_id=poll_id,
        games=[
            {"id": 1, "name": "SmallGame", "min_players": 2, "max_players": 4},
            {"id": 2, "name": "BigGame", "min_players": 2, "max_players": 6},
        ],
        players=[111, 222, 333, 444, 555],  # 5 players
        votes=[
            # SmallGame has 3 votes but is invalid for 5 players
            {"user_id": 111, "game_id": 1},
            {"user_id": 222, "game_id": 1},
            {"user_id": 333, "game_id": 1},
            # BigGame has 1 vote and IS valid
            {"user_id": 444, "game_id": 2},
        ],
        vote_limit=VoteLimit.UNLIMITED,
    )

    async with db.AsyncSessionLocal() as session:
        from src.bot.handlers import get_session_valid_games

        valid_games, priority_ids = await get_session_valid_games(session, chat_id)
        valid_names = {g.name for g in valid_games}
        assert "SmallGame" not in valid_names, "SmallGame must not be valid with 5 players"
        assert "BigGame" in valid_names

        winners, scores, _ = await PollService.close_poll(
            session, poll_id, chat_id, valid_games, priority_ids
        )

    # BigGame (1 active vote) must win; SmallGame's 3 suspended votes must not count
    assert winners == ["BigGame"], (
        f"BigGame should win; suspended SmallGame votes must not count. Got: {winners}"
    )
    assert scores.get("SmallGame", 0) == 0 or "SmallGame" not in scores


# ============================================================================
# Test: Vote limit counts only active votes
# ============================================================================


@pytest.mark.asyncio
async def test_vote_limit_ignores_suspended_votes(mock_update, mock_context):
    """A user whose votes are entirely suspended must NOT be blocked from
    voting for currently-valid games."""
    chat_id = 12345
    poll_id = "poll_prune_test"

    # vote_limit=2; user 111 has 2 votes that will both be suspended
    await _setup_poll_scenario(
        chat_id=chat_id,
        poll_id=poll_id,
        games=[
            {"id": 1, "name": "SmallGame1", "min_players": 2, "max_players": 4},
            {"id": 2, "name": "SmallGame2", "min_players": 2, "max_players": 4},
            {"id": 3, "name": "BigGame", "min_players": 2, "max_players": 6},
        ],
        players=[111, 222, 333, 444],
        votes=[
            {"user_id": 111, "game_id": 1},
            {"user_id": 111, "game_id": 2},
        ],
        vote_limit=2,
    )

    # 5th player joins -> SmallGame1 and SmallGame2 suspended for user 111
    async with db.AsyncSessionLocal() as session:
        session.add(User(telegram_id=555, telegram_name="User555"))
        await session.commit()

    mock_update.callback_query.from_user.id = 555
    mock_update.callback_query.from_user.first_name = "User555"
    mock_update.callback_query.message.chat.id = chat_id
    mock_update.callback_query.message.message_id = 888
    await join_lobby_callback(mock_update, mock_context)

    # User 111 tries to vote for BigGame — must succeed despite 2 suspended votes in DB
    async with db.AsyncSessionLocal() as session:
        from src.bot.handlers import get_session_valid_games

        valid_games, _ = await get_session_valid_games(session, chat_id)
        valid_game_ids = {g.id for g in valid_games}
        valid_category_levels = set(group_games_by_complexity(valid_games).keys())

        result = await PollService.cast_vote(
            session=session,
            poll_id=poll_id,
            user_id=111,
            target_id=3,
            vote_type=VoteType.GAME,
            user_name="User111",
            vote_limit=2,
            game_count=len(valid_games),
            valid_game_ids=valid_game_ids,
            valid_category_levels=valid_category_levels,
        )

    assert result.success, (
        f"Voting for an active game must succeed when existing votes are all suspended. "
        f"Got: {result.message}"
    )
    assert not result.is_removal


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

    assert mock_context.bot.edit_message_text.call_count >= 1, (
        "Poll should be auto-refreshed on leave"
    )


# ============================================================================
# Test: Suspension notification sent
# ============================================================================


@pytest.mark.asyncio
async def test_suspension_notification_sent(mock_update, mock_context):
    """Verify send_message is called with 'suspended' text when votes are affected."""
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
            {"user_id": 111, "game_id": 1},  # Will be suspended
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

    send_calls = mock_context.bot.send_message.call_args_list
    suspension_notification = [c for c in send_calls if "suspended" in str(c).lower()]
    assert len(suspension_notification) > 0, "Should send notification about suspended votes"


# ============================================================================
# Test: No notification when no votes are affected
# ============================================================================


@pytest.mark.asyncio
async def test_no_notification_when_no_votes_suspended(mock_update, mock_context):
    """Player joins but all votes remain valid -> no suspension notification, no deletions."""
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

    # All votes should still be in DB
    async with db.AsyncSessionLocal() as session:
        remaining_votes = (
            (await session.execute(select(PollVote).where(PollVote.poll_id == poll_id)))
            .scalars()
            .all()
        )
        assert len(remaining_votes) == 2, "All votes should survive"

    # No "suspended" notification should be sent
    send_calls = mock_context.bot.send_message.call_args_list
    suspension_notification = [c for c in send_calls if "suspended" in str(c).lower()]
    assert len(suspension_notification) == 0, "Should NOT send suspension notification"


# ============================================================================
# Test: Leaving player's votes are excluded from the rendered poll (issue #49)
# ============================================================================


@pytest.mark.asyncio
async def test_leaver_votes_excluded_from_render(mock_update, mock_context):
    """When a player leaves, their already-cast votes must not appear in the
    rendered poll text or count toward totals — even when the games they
    voted for are still valid for the remaining lobby."""
    chat_id = 12345
    poll_id = "poll_prune_test"

    await _setup_poll_scenario(
        chat_id=chat_id,
        poll_id=poll_id,
        games=[
            {"id": 1, "name": "GameA", "min_players": 2, "max_players": 6},
            {"id": 2, "name": "GameB", "min_players": 2, "max_players": 6},
        ],
        players=[111, 222, 333],
        votes=[
            {"user_id": 111, "game_id": 1},
            {"user_id": 222, "game_id": 2},
            {"user_id": 333, "game_id": 1},  # User 333 will leave
        ],
        vote_limit=VoteLimit.UNLIMITED,
    )

    mock_update.callback_query.from_user.id = 333
    mock_update.callback_query.from_user.first_name = "User333"
    mock_update.callback_query.message.chat.id = chat_id
    mock_update.callback_query.message.message_id = 888

    mock_context.bot.edit_message_text.reset_mock()
    await leave_lobby_callback(mock_update, mock_context)

    # User 333's vote must remain in the DB (suspended, will resume on rejoin)
    async with db.AsyncSessionLocal() as session:
        all_votes = (
            (await session.execute(select(PollVote).where(PollVote.poll_id == poll_id)))
            .scalars()
            .all()
        )
        assert any(v.user_id == 333 for v in all_votes), (
            "Leaver's vote must be kept in DB so it resumes on rejoin"
        )

    # Render must reflect only 2 active votes (111 and 222), not 3
    calls = mock_context.bot.edit_message_text.call_args_list
    assert calls, "render_poll_message should have been called on leave"
    last_render_text = calls[-1].kwargs.get("text") or calls[-1].args[0]

    assert "2 votes" in last_render_text or "2 vote" in last_render_text, (
        f"Only the 2 remaining members' votes should be counted. Got: {last_render_text!r}"
    )
    # GameA was voted by both 111 (active) and 333 (left) — display must show 1, not 2
    assert "**1** - GameA" in last_render_text, (
        f"GameA should show 1 active vote (333's vote suspended). Got: {last_render_text!r}"
    )


# ============================================================================
# Test: Leaver's votes are not counted in close_poll winner calculation
# ============================================================================


@pytest.mark.asyncio
async def test_close_poll_ignores_leaver_votes(mock_update, mock_context):
    """When closing a poll, votes from users who left the lobby must not
    influence the winner."""
    chat_id = 12345
    poll_id = "poll_close_leaver"

    await _setup_poll_scenario(
        chat_id=chat_id,
        poll_id=poll_id,
        games=[
            {"id": 1, "name": "GameA", "min_players": 2, "max_players": 6},
            {"id": 2, "name": "GameB", "min_players": 2, "max_players": 6},
        ],
        players=[111, 222, 333],
        votes=[
            # Without filtering, GameA wins 2-1; once 333 leaves, GameB wins 1-1
            # ... actually 1-1 is a tie. Let's tilt GameB.
            {"user_id": 111, "game_id": 1},
            {"user_id": 333, "game_id": 1},  # 333 leaves -> this vote should NOT count
            {"user_id": 222, "game_id": 2},
            {"user_id": 222, "game_id": 1},  # makes GameA tie if 333 stayed
        ],
        vote_limit=VoteLimit.UNLIMITED,
    )

    # 333 leaves before the poll closes
    mock_update.callback_query.from_user.id = 333
    mock_update.callback_query.from_user.first_name = "User333"
    mock_update.callback_query.message.chat.id = chat_id
    mock_update.callback_query.message.message_id = 888
    await leave_lobby_callback(mock_update, mock_context)

    async with db.AsyncSessionLocal() as session:
        from src.bot.handlers import get_session_valid_games

        valid_games, priority_ids = await get_session_valid_games(session, chat_id)
        winners, scores, _ = await PollService.close_poll(
            session, poll_id, chat_id, valid_games, priority_ids
        )

    # Active votes after 333 leaves: 111->A, 222->A, 222->B  → A wins 2-1.
    # But 333's suspended vote on A should NOT push A to 3.
    # The crucial check: scores must reflect only active-member votes.
    assert scores.get("GameA", 0) == 2, (
        f"GameA should have 2 active votes (333's vote excluded). Got scores: {scores}"
    )
    assert scores.get("GameB", 0) == 1, f"GameB should have 1 vote. Got scores: {scores}"
    assert winners == ["GameA"], (
        f"GameA wins 2-1 with leaver's vote excluded. Got winners: {winners}"
    )


# ============================================================================
# Test: Leave-then-rejoin restores the leaver's votes
# ============================================================================


@pytest.mark.asyncio
async def test_votes_restored_after_player_leaves_then_rejoins(mock_update, mock_context):
    """A leaver's votes are suspended (kept in DB), so if they rejoin the
    same lobby their votes resume counting automatically."""
    chat_id = 12345
    poll_id = "poll_prune_test"

    await _setup_poll_scenario(
        chat_id=chat_id,
        poll_id=poll_id,
        games=[
            {"id": 1, "name": "GameA", "min_players": 2, "max_players": 6},
        ],
        players=[111, 222, 333],
        votes=[
            {"user_id": 333, "game_id": 1},
        ],
        vote_limit=VoteLimit.UNLIMITED,
    )

    # 333 leaves -> vote suspended
    mock_update.callback_query.from_user.id = 333
    mock_update.callback_query.from_user.first_name = "User333"
    mock_update.callback_query.message.chat.id = chat_id
    mock_update.callback_query.message.message_id = 888
    await leave_lobby_callback(mock_update, mock_context)

    mock_context.bot.edit_message_text.reset_mock()

    # 333 rejoins
    await join_lobby_callback(mock_update, mock_context)

    # Vote is back in the active tally
    calls = mock_context.bot.edit_message_text.call_args_list
    assert calls, "render_poll_message should have been called on rejoin"
    last_render_text = calls[-1].kwargs.get("text") or calls[-1].args[0]

    assert "1 vote" in last_render_text, (
        f"333's vote should resume counting after rejoin. Got: {last_render_text!r}"
    )
    assert "**1** - GameA" in last_render_text, (
        f"GameA should show 1 active vote after rejoin. Got: {last_render_text!r}"
    )
