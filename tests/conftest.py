import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from telegram import CallbackQuery, Message, Update
from telegram.ext import ContextTypes

from src.core import db
from src.core.models import Base

# Configure logging for tests
logging.basicConfig(level=logging.INFO)


@pytest.fixture(scope="function", autouse=True)
async def setup_test_db():
    """
    Override the production DB engine with an in-memory SQLite engine for tests.
    This ensures tests are isolated and don't affect the file-based DB.
    """
    # Create in-memory engine
    test_engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)

    # Patch the global engine and sessionmaker in src.core.db
    # Since handlers.py imports 'db' and calls 'db.AsyncSessionLocal()', this works!
    original_engine = db.engine
    original_sessionmaker = db.AsyncSessionLocal

    db.engine = test_engine
    db.AsyncSessionLocal = async_sessionmaker(
        test_engine, class_=AsyncSession, expire_on_commit=False
    )

    # Create Tables
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield

    # Teardown
    await db.engine.dispose()

    # Restore (optional, but good practice if tests shared process)
    db.engine = original_engine
    db.AsyncSessionLocal = original_sessionmaker


@pytest.fixture
def mock_update():
    """Create a mock Telegram Update object."""
    update = MagicMock(spec=Update)
    update.effective_chat.id = 12345
    update.effective_user.id = 111
    update.effective_user.first_name = "TestUser"
    update.message = MagicMock(spec=Message)

    # Configure reply_text to return a proper message with message_id
    mock_reply_message = MagicMock()
    mock_reply_message.message_id = 997
    mock_reply_message.edit_text = AsyncMock()  # Must be awaitable
    update.message.reply_text = AsyncMock(return_value=mock_reply_message)

    update.callback_query = MagicMock(spec=CallbackQuery)
    update.callback_query.message.chat.id = 12345
    update.callback_query.from_user.id = 111
    update.callback_query.from_user.first_name = "TestUser"
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    return update


@pytest.fixture
def mock_context():
    """Create a mock Telegram Context object."""
    context = MagicMock(spec=ContextTypes.DEFAULT_TYPE)

    # Configure send_message to return a proper mock with message_id
    mock_message = MagicMock()
    mock_message.message_id = 998
    context.bot.send_message = AsyncMock(return_value=mock_message)

    # Configure send_poll to return a proper mock with poll.id
    mock_poll_message = MagicMock()
    mock_poll_message.poll.id = "test_poll_id"
    mock_poll_message.message_id = 999
    context.bot.send_poll = AsyncMock(return_value=mock_poll_message)

    # Configure edit_message_text
    context.bot.edit_message_text = AsyncMock()

    context.args = []
    return context
