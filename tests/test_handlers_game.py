from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from src.bot.handlers import add_game, set_bgg
from src.core import db
from src.core.models import Collection, Game, GameState, User

# ============================================================================
# /setbgg command tests
# ============================================================================


@pytest.mark.asyncio
async def test_setbgg_no_args(mock_update, mock_context):
    """Test /setbgg without username shows usage."""
    mock_context.args = []
    await set_bgg(mock_update, mock_context)

    # Check that usage message was sent (now includes [force] option)
    call_args = mock_update.message.reply_text.call_args[0][0]
    assert "Usage: /setbgg" in call_args
    assert "<username>" in call_args


@pytest.mark.asyncio
async def test_setbgg_creates_user(mock_update, mock_context):
    """Test /setbgg creates user and updates name."""
    mock_context.args = ["testbgguser"]

    with patch("src.bot.handlers.BGGClient") as MockBGG:
        mock_client = MockBGG.return_value
        mock_client.fetch_collection = AsyncMock(return_value=[])
        mock_client.fetch_expansions = AsyncMock(return_value=[])

        await set_bgg(mock_update, mock_context)

        async with db.AsyncSessionLocal() as session:
            user = await session.get(User, 111)
            assert user is not None
            assert user.bgg_username == "testbgguser"
            assert user.telegram_name == "TestUser"


@pytest.mark.asyncio
async def test_setbgg_bgg_fetch_fails(mock_update, mock_context):
    """Test /setbgg handles BGG API failure gracefully."""
    mock_context.args = ["invaliduser"]

    with patch("src.bot.handlers.BGGClient") as MockBGG:
        mock_client = MockBGG.return_value
        mock_client.fetch_collection = AsyncMock(side_effect=Exception("API Error"))

        await set_bgg(mock_update, mock_context)

        # Should send two messages: initial feedback + error
        assert mock_update.message.reply_text.call_count == 2
        error_call = mock_update.message.reply_text.call_args_list[1][0][0]
        assert "temporarily unavailable" in error_call or "try again later" in error_call


@pytest.mark.asyncio
async def test_setbgg_resync_adds_new_games(mock_update, mock_context):
    """Test /setbgg re-sync adds newly added BGG games to collection."""
    mock_context.args = ["testuser"]

    # Setup: User already has some games
    async with db.AsyncSessionLocal() as session:
        user = User(telegram_id=111, telegram_name="TestUser", bgg_username="testuser")
        session.add(user)

        # Existing games
        g1 = Game(
            id=1, name="OldGame1", min_players=2, max_players=4, playing_time=60, complexity=2.0
        )
        g2 = Game(
            id=2, name="OldGame2", min_players=2, max_players=4, playing_time=60, complexity=2.0
        )
        session.add_all([g1, g2])
        await session.flush()

        # User already has these in collection
        c1 = Collection(user_id=111, game_id=1)
        c2 = Collection(user_id=111, game_id=2)
        session.add_all([c1, c2])
        await session.commit()

    # Mock BGG response with original + new games
    g3 = Game(id=3, name="NewGame", min_players=2, max_players=4, playing_time=60, complexity=2.5)
    mock_games = [
        Game(id=1, name="OldGame1", min_players=2, max_players=4, playing_time=60, complexity=2.0),
        Game(id=2, name="OldGame2", min_players=2, max_players=4, playing_time=60, complexity=2.0),
        g3,
    ]

    with patch("src.bot.handlers.BGGClient") as MockBGG:
        mock_client = MockBGG.return_value
        mock_client.fetch_collection = AsyncMock(return_value=mock_games)
        mock_client.fetch_expansions = AsyncMock(return_value=[])

        await set_bgg(mock_update, mock_context)

    # Verify new game was added to collection
    async with db.AsyncSessionLocal() as session:
        stmt = select(Collection).where(Collection.user_id == 111)
        collections = (await session.execute(stmt)).scalars().all()
        collection_ids = {c.game_id for c in collections}
        assert collection_ids == {1, 2, 3}

    # Verify feedback message mentions new game
    # The command now updates the status message via edit_text
    status_msg = mock_update.message.reply_text.return_value

    # Get all calls to edit_text
    edit_calls = [c[0][0] for c in status_msg.edit_text.call_args_list]
    success_msg = next((m for m in edit_calls if "Sync Complete" in m), None)

    assert success_msg is not None
    assert "3 games" in success_msg
    assert "1 new" in success_msg


@pytest.mark.asyncio
async def test_setbgg_resync_removes_deleted_games(mock_update, mock_context):
    """Test /setbgg re-sync removes games no longer in BGG collection."""
    mock_context.args = ["testuser"]

    # Setup: User has 3 games
    async with db.AsyncSessionLocal() as session:
        user = User(telegram_id=111, telegram_name="TestUser", bgg_username="testuser")
        session.add(user)

        g1 = Game(id=1, name="Game1", min_players=2, max_players=4, playing_time=60, complexity=2.0)
        g2 = Game(id=2, name="Game2", min_players=2, max_players=4, playing_time=60, complexity=2.0)
        g3 = Game(id=3, name="Game3", min_players=2, max_players=4, playing_time=60, complexity=2.0)
        session.add_all([g1, g2, g3])
        await session.flush()

        c1 = Collection(user_id=111, game_id=1)
        c2 = Collection(user_id=111, game_id=2)
        c3 = Collection(user_id=111, game_id=3)
        session.add_all([c1, c2, c3])
        await session.commit()

    # Mock BGG response with only games 1 and 2 (game 3 removed)
    mock_games = [
        Game(id=1, name="Game1", min_players=2, max_players=4, playing_time=60, complexity=2.0),
        Game(id=2, name="Game2", min_players=2, max_players=4, playing_time=60, complexity=2.0),
    ]

    with patch("src.bot.handlers.BGGClient") as MockBGG:
        mock_client = MockBGG.return_value
        mock_client.fetch_collection = AsyncMock(return_value=mock_games)
        mock_client.fetch_expansions = AsyncMock(return_value=[])

        await set_bgg(mock_update, mock_context)

    # Verify game 3 was removed from collection
    async with db.AsyncSessionLocal() as session:
        stmt = select(Collection).where(Collection.user_id == 111)
        collections = (await session.execute(stmt)).scalars().all()
        collection_ids = {c.game_id for c in collections}
        assert collection_ids == {1, 2}

    # Verify feedback message mentions removed game
    status_msg = mock_update.message.reply_text.return_value
    edit_calls = [c[0][0] for c in status_msg.edit_text.call_args_list]
    success_msg = next((m for m in edit_calls if "Sync Complete" in m), None)

    assert success_msg is not None
    assert "2 games" in success_msg
    assert "1 removed" in success_msg


@pytest.mark.asyncio
async def test_setbgg_resync_no_changes(mock_update, mock_context):
    """Test /setbgg re-sync with no changes shows appropriate message."""
    mock_context.args = ["testuser"]

    # Setup: User has games
    async with db.AsyncSessionLocal() as session:
        user = User(telegram_id=111, telegram_name="TestUser", bgg_username="testuser")
        session.add(user)

        g1 = Game(id=1, name="Game1", min_players=2, max_players=4, playing_time=60, complexity=2.0)
        g2 = Game(id=2, name="Game2", min_players=2, max_players=4, playing_time=60, complexity=2.0)
        session.add_all([g1, g2])
        await session.flush()

        c1 = Collection(user_id=111, game_id=1)
        c2 = Collection(user_id=111, game_id=2)
        session.add_all([c1, c2])
        await session.commit()

    # Mock BGG response with same games
    mock_games = [
        Game(id=1, name="Game1", min_players=2, max_players=4, playing_time=60, complexity=2.0),
        Game(id=2, name="Game2", min_players=2, max_players=4, playing_time=60, complexity=2.0),
    ]

    with patch("src.bot.handlers.BGGClient") as MockBGG:
        mock_client = MockBGG.return_value
        mock_client.fetch_collection = AsyncMock(return_value=mock_games)
        mock_client.fetch_expansions = AsyncMock(return_value=[])

        await set_bgg(mock_update, mock_context)

    # Verify feedback message shows no changes
    status_msg = mock_update.message.reply_text.return_value
    edit_calls = [c[0][0] for c in status_msg.edit_text.call_args_list]
    success_msg = next((m for m in edit_calls if "Sync Complete" in m), None)

    assert success_msg is not None
    assert "2 games" in success_msg
    # "no changes" isn't explicitly printed, the absence of "new" or "removed"
    # or "updated" implies it. We just check the base message is there
    assert "Sync Complete" in success_msg


@pytest.mark.asyncio
async def test_setbgg_initial_sync_feedback(mock_update, mock_context):
    """Test /setbgg initial sync shows correct feedback."""
    mock_context.args = ["newuser"]

    # Mock BGG response
    mock_games = [
        Game(id=1, name="Game1", min_players=2, max_players=4, playing_time=60, complexity=2.0),
        Game(id=2, name="Game2", min_players=2, max_players=4, playing_time=60, complexity=2.0),
        Game(id=3, name="Game3", min_players=2, max_players=4, playing_time=60, complexity=2.0),
    ]

    with patch("src.bot.handlers.BGGClient") as MockBGG:
        mock_client = MockBGG.return_value
        mock_client.fetch_collection = AsyncMock(return_value=mock_games)
        mock_client.fetch_expansions = AsyncMock(return_value=[])

        await set_bgg(mock_update, mock_context)

    # Verify feedback messages
    # 1. Initial reply_text ("Fetching collection...")
    # 2. edit_text ("Fetching computed complexity...")
    # 3. edit_text ("Syncing expansions...")
    # 4. edit_text ("Sync Complete!")

    assert mock_update.message.reply_text.call_count == 1
    status_msg = mock_update.message.reply_text.return_value

    edit_calls = [c[0][0] for c in status_msg.edit_text.call_args_list]
    # We expect at least one update (complexity/expansion) and one final result
    assert len(edit_calls) >= 2

    # Check for specific updates
    assert any("computed complexity" in c for c in edit_calls) or any(
        "Syncing expansions" in c for c in edit_calls
    )

    final_msg = edit_calls[-1]
    assert "Sync Complete" in final_msg
    assert "3 games" in final_msg
    assert "3 new" in final_msg


# ============================================================================
# /addgame command tests
# ============================================================================


@pytest.mark.asyncio
async def test_addgame_no_args(mock_update, mock_context):
    """Test /addgame without arguments shows usage."""
    mock_context.args = []
    await add_game(mock_update, mock_context)

    call_args = mock_update.message.reply_text.call_args[0][0]
    assert "Usage:" in call_args
    assert "Search BGG" in call_args


@pytest.mark.asyncio
async def test_addgame_searches_bgg_when_name_only(mock_update, mock_context):
    """Test /addgame with only name searches BGG."""
    mock_context.args = ["Catan"]

    mock_game = Game(
        id=13,
        name="Catan",
        min_players=3,
        max_players=4,
        playing_time=60,
        complexity=2.32,
        thumbnail="http://example.com/catan.jpg",
    )

    with patch("src.bot.handlers.BGGClient") as MockBGG:
        mock_client = MockBGG.return_value
        mock_client.search_games = AsyncMock(
            return_value=[{"id": 13, "name": "Catan", "year_published": "1995"}]
        )
        mock_client.get_game_details = AsyncMock(return_value=mock_game)

        await add_game(mock_update, mock_context)

        mock_client.search_games.assert_called_once_with("Catan", limit=10)
        mock_client.get_game_details.assert_called_once_with(13)

    # Verify game was added
    async with db.AsyncSessionLocal() as session:
        game = await session.get(Game, 13)
        assert game is not None
        assert game.name == "Catan"
        assert game.min_players == 3
        assert game.max_players == 4


@pytest.mark.asyncio
async def test_addgame_multiword_name_searches_bgg(mock_update, mock_context):
    """Test /addgame with multi-word name searches BGG."""
    mock_context.args = ["Ticket", "to", "Ride"]

    mock_game = Game(
        id=9209,
        name="Ticket to Ride",
        min_players=2,
        max_players=5,
        playing_time=60,
        complexity=1.85,
    )

    with patch("src.bot.handlers.BGGClient") as MockBGG:
        mock_client = MockBGG.return_value
        mock_client.search_games = AsyncMock(return_value=[{"id": 9209, "name": "Ticket to Ride"}])
        mock_client.get_game_details = AsyncMock(return_value=mock_game)

        await add_game(mock_update, mock_context)

        # Should join args into search query
        mock_client.search_games.assert_called_once_with("Ticket to Ride", limit=10)


@pytest.mark.asyncio
async def test_addgame_bgg_not_found(mock_update, mock_context):
    """Test /addgame when BGG search returns no results."""
    mock_context.args = ["NonexistentGame123"]

    with patch("src.bot.handlers.BGGClient") as MockBGG:
        mock_client = MockBGG.return_value
        mock_client.search_games = AsyncMock(return_value=[])

        await add_game(mock_update, mock_context)

    # Should suggest manual entry
    call_args = mock_update.message.reply_text.call_args[0][0]
    assert "Could not find" in call_args
    assert "manual entry" in call_args.lower()


@pytest.mark.asyncio
async def test_addgame_bgg_error_fallback(mock_update, mock_context):
    """Test /addgame handles BGG API error gracefully."""
    mock_context.args = ["Catan"]

    with patch("src.bot.handlers.BGGClient") as MockBGG:
        mock_client = MockBGG.return_value
        mock_client.search_games = AsyncMock(side_effect=Exception("API Error"))

        await add_game(mock_update, mock_context)

    call_args = mock_update.message.reply_text.call_args[0][0]
    assert "Error searching BGG" in call_args


@pytest.mark.asyncio
async def test_addgame_manual_with_all_params(mock_update, mock_context):
    """Test /addgame with all parameters uses manual mode (bypasses BGG)."""
    mock_context.args = ["CustomGame", "3", "4", "2.3"]

    with patch("src.bot.handlers.BGGClient") as MockBGG:
        await add_game(mock_update, mock_context)

        # Should NOT call BGG (manual mode)
        MockBGG.assert_not_called()

    async with db.AsyncSessionLocal() as session:
        stmt = select(Game).where(Game.name == "CustomGame")
        game = (await session.execute(stmt)).scalar_one_or_none()
        assert game is not None
        assert game.min_players == 3
        assert game.max_players == 4
        assert game.complexity == 2.3
        assert game.id < 0  # Negative ID for manual games


@pytest.mark.asyncio
async def test_addgame_creates_user(mock_update, mock_context):
    """Test /addgame creates user if doesn't exist."""
    mock_update.effective_user.id = 999  # New user
    mock_context.args = ["TestGame", "2", "6", "2.5"]  # Manual mode

    await add_game(mock_update, mock_context)

    async with db.AsyncSessionLocal() as session:
        user = await session.get(User, 999)
        assert user is not None
        assert user.telegram_name == "TestUser"


@pytest.mark.asyncio
async def test_addgame_uses_cache(mock_update, mock_context):
    """Test /addgame uses local cache if available."""
    mock_context.args = ["CachedGame"]

    # Pre-populate DB with the game
    async with db.AsyncSessionLocal() as session:
        game = Game(
            id=12345,
            name="CachedGame",
            min_players=1,
            max_players=4,
            playing_time=30,
            complexity=2.0,
        )
        session.add(game)
        await session.commit()

    with patch("src.bot.handlers.BGGClient") as MockBGG:
        mock_client = MockBGG.return_value
        # Search returns the ID we have locally
        mock_client.search_games = AsyncMock(return_value=[{"id": 12345, "name": "CachedGame"}])
        # get_game_details should NOT be called
        mock_client.get_game_details = AsyncMock()

        await add_game(mock_update, mock_context)

        mock_client.search_games.assert_called_once()
        mock_client.get_game_details.assert_not_called()

    # Verify user collection updated
    async with db.AsyncSessionLocal() as session:
        stmt = select(Collection).where(Collection.user_id == 111, Collection.game_id == 12345)
        col = (await session.execute(stmt)).scalar_one_or_none()
        assert col is not None


# ============================================================================
# Auto-Star Feature Tests
# ============================================================================


@pytest.mark.asyncio
async def test_setbgg_incremental_sync_auto_stars_new_games(mock_update, mock_context):
    """Test /setbgg incremental sync marks NEW games as STARRED."""
    mock_context.args = ["testuser"]

    # Setup: User has existing games (first sync happened long ago)
    async with db.AsyncSessionLocal() as session:
        user = User(telegram_id=111, telegram_name="TestUser", bgg_username="testuser")
        session.add(user)
        # Existing game
        g1 = Game(
            id=1, name="OldGame", min_players=2, max_players=4, playing_time=60, complexity=2.0
        )
        session.add(g1)
        # Existing collection entry (normal state)
        c1 = Collection(user_id=111, game_id=1, state=GameState.INCLUDED)
        session.add(c1)
        await session.commit()

    # Mock BGG: OldGame + NewGame
    g2 = Game(id=2, name="NewGame", min_players=2, max_players=4, playing_time=60, complexity=2.5)
    mock_games = [
        Game(id=1, name="OldGame", min_players=2, max_players=4, playing_time=60, complexity=2.0),
        g2,
    ]

    with patch("src.bot.handlers.BGGClient") as MockBGG:
        mock_client = MockBGG.return_value
        mock_client.fetch_collection = AsyncMock(return_value=mock_games)
        mock_client.fetch_expansions = AsyncMock(return_value=[])

        await set_bgg(mock_update, mock_context)

    async with db.AsyncSessionLocal() as session:
        # Check OldGame is still INCLUDED (not changed)
        # Check OldGame is still INCLUDED (not changed)
        stmt = select(Collection).where(Collection.user_id == 111, Collection.game_id == 1)
        c1 = (await session.execute(stmt)).scalar_one()
        assert c1.state == GameState.INCLUDED

        # Check NewGame is STARRED
        stmt = select(Collection).where(Collection.user_id == 111, Collection.game_id == 2)
        c2 = (await session.execute(stmt)).scalar_one()
        assert c2.state == GameState.STARRED


@pytest.mark.asyncio
async def test_setbgg_first_sync_no_auto_star(mock_update, mock_context):
    """Test /setbgg first-time sync does NOT auto-star games."""
    mock_context.args = ["newuser"]

    # No existing user/collection

    mock_games = [
        Game(id=1, name="Game1", min_players=2, max_players=4, playing_time=60, complexity=2.0),
        Game(id=2, name="Game2", min_players=2, max_players=4, playing_time=60, complexity=2.0),
    ]

    with patch("src.bot.handlers.BGGClient") as MockBGG:
        mock_client = MockBGG.return_value
        mock_client.fetch_collection = AsyncMock(return_value=mock_games)
        mock_client.fetch_expansions = AsyncMock(return_value=[])

        await set_bgg(mock_update, mock_context)

    async with db.AsyncSessionLocal() as session:
        stmt = select(Collection).where(Collection.user_id == 111)
        cols = (await session.execute(stmt)).scalars().all()
        assert len(cols) == 2
        for c in cols:
            assert c.state == GameState.INCLUDED  # Not starred


@pytest.mark.asyncio
async def test_setbgg_force_sync_no_auto_star(mock_update, mock_context):
    """Test /setbgg force sync does NOT auto-star new games."""
    mock_context.args = ["testuser", "force"]

    # Setup: User has existing games
    async with db.AsyncSessionLocal() as session:
        user = User(telegram_id=111, telegram_name="TestUser", bgg_username="testuser")
        session.add(user)
        g1 = Game(
            id=1, name="OldGame", min_players=2, max_players=4, playing_time=60, complexity=2.0
        )
        session.add(g1)
        c1 = Collection(user_id=111, game_id=1, state=GameState.INCLUDED)
        session.add(c1)
        await session.commit()

    # Mock BGG: OldGame + NewGame (NewGame found during Force Sync shouldn't star)
    # Actually, rationale says "during a first sync or force sync... shouldn't all be starred"
    # Wait, if I do a force sync and a NEW game appears, should it be starred?
    # Rationale: "Force sync... importing entire existing collection... shouldn't all be starred".
    # Logic implemented: should_auto_star = not is_first_sync and not force_update
    # So ANY force sync disables auto-starring, even for new games
    # found during it. This matches requirement.

    g2 = Game(id=2, name="NewGame", min_players=2, max_players=4, playing_time=60, complexity=2.5)
    mock_games = [
        Game(id=1, name="OldGame", min_players=2, max_players=4, playing_time=60, complexity=2.0),
        g2,
    ]

    with patch("src.bot.handlers.BGGClient") as MockBGG:
        mock_client = MockBGG.return_value
        mock_client.fetch_collection = AsyncMock(return_value=mock_games)
        mock_client.fetch_expansions = AsyncMock(return_value=[])

        await set_bgg(mock_update, mock_context)

    async with db.AsyncSessionLocal() as session:
        # Check NewGame is INCLUDED (not starred)
        stmt = select(Collection).where(Collection.user_id == 111, Collection.game_id == 2)
        c2 = (await session.execute(stmt)).scalar_one()
        assert c2.state == GameState.INCLUDED


@pytest.mark.asyncio
async def test_addgame_manual_auto_stars(mock_update, mock_context):
    """Test /addgame manual entry auto-stars the game."""
    mock_context.args = ["ManualGame", "2", "4", "2.0"]  # Manual args

    with patch("src.bot.handlers.BGGClient"):
        await add_game(mock_update, mock_context)

    async with db.AsyncSessionLocal() as session:
        stmt = select(Collection).join(Game).where(Game.name == "ManualGame")
        col = (await session.execute(stmt)).scalar_one()
        assert col.state == GameState.STARRED


@pytest.mark.asyncio
async def test_addgame_bgg_search_auto_stars(mock_update, mock_context):
    """Test /addgame BGG search auto-stars the game."""
    mock_context.args = ["SearchGame"]

    mock_game = Game(
        id=100,
        name="SearchGame",
        min_players=2,
        max_players=4,
        playing_time=60,
        complexity=2.0,
    )

    with patch("src.bot.handlers.BGGClient") as MockBGG:
        mock_client = MockBGG.return_value
        mock_client.search_games = AsyncMock(return_value=[{"id": 100, "name": "SearchGame"}])
        mock_client.get_game_details = AsyncMock(return_value=mock_game)

        await add_game(mock_update, mock_context)

    async with db.AsyncSessionLocal() as session:
        stmt = select(Collection).where(Collection.game_id == 100)
        col = (await session.execute(stmt)).scalar_one()
        assert col.state == GameState.STARRED
