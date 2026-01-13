from unittest.mock import patch

import pytest
from sqlalchemy import select

from src.bot.handlers import help_command, start
from src.bot.handlers import test_mode as handler_test_mode
from src.core import db
from src.core.models import Collection, Game, SessionPlayer, User

# ============================================================================
# /start command tests
# ============================================================================


@pytest.mark.asyncio
async def test_start_command(mock_update, mock_context):
    """Test /start shows welcome message."""
    with patch("os.path.exists", return_value=False):
        await start(mock_update, mock_context)

    mock_update.message.reply_text.assert_called_once()
    call_args = mock_update.message.reply_text.call_args[0][0]
    assert "Welcome to Game Night Decider!" in call_args
    assert "Quick Start" in call_args
    assert "/setbgg" in call_args
    assert "/addgame" in call_args
    assert "/help" in call_args


@pytest.mark.asyncio
async def test_help_command(mock_update, mock_context):
    """Test /help shows command list."""
    await help_command(mock_update, mock_context)

    mock_update.message.reply_text.assert_called_once()
    call_args = mock_update.message.reply_text.call_args[0][0]

    assert "Command List" in call_args
    assert "/setbgg" in call_args
    assert "/poll" in call_args
    assert "/addguest" in call_args


# ============================================================================
# /testmode command tests
# ============================================================================


@pytest.mark.asyncio
async def test_testmode_creates_users_and_games(mock_update, mock_context):
    """Test /testmode creates fake users with test games (default 2 users)."""
    mock_context.args = []  # No args = default 2 users
    await handler_test_mode(mock_update, mock_context)

    async with db.AsyncSessionLocal() as session:
        # Check fake users created
        u1 = await session.get(User, 999001)
        u2 = await session.get(User, 999002)
        assert u1 is not None
        assert u2 is not None
        assert u1.telegram_name == "TestUser1"

        # Check test games created
        g = await session.get(Game, -1001)
        assert g is not None
        assert g.name == "Test Catan"

        # Check collections created
        stmt = select(Collection).where(Collection.user_id == 999001)
        cols = (await session.execute(stmt)).scalars().all()
        assert len(cols) == 4  # 4 test games


@pytest.mark.asyncio
async def test_testmode_adds_to_lobby(mock_update, mock_context):
    """Test /testmode adds fake users to current lobby (default 2 users)."""
    # Use unique chat_id for this test
    mock_update.effective_chat.id = 99999
    mock_context.args = []  # Default 2 users

    await handler_test_mode(mock_update, mock_context)

    async with db.AsyncSessionLocal() as session:
        stmt = select(SessionPlayer).where(SessionPlayer.session_id == 99999)
        players = (await session.execute(stmt)).scalars().all()
        assert len(players) == 3  # 2 fake users + calling user


@pytest.mark.asyncio
async def test_testmode_custom_player_count(mock_update, mock_context):
    """Test /testmode with custom player count."""
    mock_update.effective_chat.id = 88888
    mock_context.args = ["5"]  # 5 fake users

    await handler_test_mode(mock_update, mock_context)

    async with db.AsyncSessionLocal() as session:
        # Check 5 fake users created
        for i in range(1, 6):
            user = await session.get(User, 999000 + i)
            assert user is not None, f"TestUser{i} not created"
            assert user.telegram_name == f"TestUser{i}"

        # Check session has 6 players (5 fake + calling user)
        stmt = select(SessionPlayer).where(SessionPlayer.session_id == 88888)
        players = (await session.execute(stmt)).scalars().all()
        assert len(players) == 6


@pytest.mark.asyncio
async def test_testmode_clamps_player_count(mock_update, mock_context):
    """Test /testmode clamps player count to 1-10 range."""
    mock_update.effective_chat.id = 77777
    mock_context.args = ["20"]  # Should be clamped to 10

    await handler_test_mode(mock_update, mock_context)

    async with db.AsyncSessionLocal() as session:
        stmt = select(SessionPlayer).where(SessionPlayer.session_id == 77777)
        players = (await session.execute(stmt)).scalars().all()
        assert len(players) == 11  # 10 fake users + calling user


@pytest.mark.asyncio
async def test_testmode_run_twice_existing_users(mock_update, mock_context):
    """Test /testmode running a second time handles existing users correctly."""
    mock_update.effective_chat.id = 66666

    # Run once
    mock_context.args = []
    await handler_test_mode(mock_update, mock_context)

    # Run twice
    await handler_test_mode(mock_update, mock_context)

    async with db.AsyncSessionLocal() as session:
        # Verify users still exist and have collections
        u1 = await session.get(User, 999001)
        assert u1 is not None

        # Check collection (should have 4 items)
        stmt = select(Collection).where(Collection.user_id == 999001)
        cols = (await session.execute(stmt)).scalars().all()
        assert len(cols) == 4

        # Check session players
        stmt = select(SessionPlayer).where(SessionPlayer.session_id == 66666)
        players = (await session.execute(stmt)).scalars().all()
        assert len(players) == 3
