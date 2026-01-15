from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from src.bot.handlers import add_game, set_bgg
from src.core import db
from src.core.models import Collection, Game, User

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

        await set_bgg(mock_update, mock_context)

    # Verify new game was added to collection
    async with db.AsyncSessionLocal() as session:
        stmt = select(Collection).where(Collection.user_id == 111)
        collections = (await session.execute(stmt)).scalars().all()
        collection_ids = {c.game_id for c in collections}
        assert collection_ids == {1, 2, 3}

    # Verify feedback message mentions new game
    calls = [call[0][0] for call in mock_update.message.reply_text.call_args_list]
    success_msg = calls[-1]  # Last message should be success
    assert "3 games total" in success_msg
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

        await set_bgg(mock_update, mock_context)

    # Verify game 3 was removed from collection
    async with db.AsyncSessionLocal() as session:
        stmt = select(Collection).where(Collection.user_id == 111)
        collections = (await session.execute(stmt)).scalars().all()
        collection_ids = {c.game_id for c in collections}
        assert collection_ids == {1, 2}

    # Verify feedback message mentions removed game
    calls = [call[0][0] for call in mock_update.message.reply_text.call_args_list]
    success_msg = calls[-1]
    assert "2 games total" in success_msg
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

        await set_bgg(mock_update, mock_context)

    # Verify feedback message shows no changes
    calls = [call[0][0] for call in mock_update.message.reply_text.call_args_list]
    success_msg = calls[-1]
    assert "2 games total" in success_msg
    assert "no changes" in success_msg


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

        await set_bgg(mock_update, mock_context)

    # Verify feedback messages
    calls = [call[0][0] for call in mock_update.message.reply_text.call_args_list]
    assert len(calls) == 2
    assert "Fetching collection..." in calls[0]
    success_msg = calls[1]
    assert "3 games total" in success_msg
    assert "3 new" in success_msg


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



