import json
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from telegram import CallbackQuery, Message, Update
from telegram.ext import ContextTypes

from src.bot import handlers
from src.core import db
from src.core.models import Base, GameNightPoll, PollBallot, Session

# Configure logging for tests
logging.basicConfig(level=logging.INFO)


@pytest.fixture(scope="function", autouse=True)
async def setup_test_db():
    """
    Override the production DB engine with an in-memory SQLite engine for tests.
    This ensures tests are isolated and don't affect the file-based DB.
    """
    # Create in-memory engine
    test_engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

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
    update.effective_user.last_name = None
    update.effective_user.username = None
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
    update.callback_query.from_user.last_name = None
    update.callback_query.from_user.username = None
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

    # Configure edit_message_text
    context.bot.edit_message_text = AsyncMock()

    # Configure pin/unpin
    context.bot.pin_chat_message = AsyncMock()
    context.bot.unpin_chat_message = AsyncMock()

    # Widgets go through raw API calls; sendWidget must hand back a message id.
    sent_widgets = iter(range(7000, 8000))

    async def do_api_request(endpoint, api_kwargs=None, **_):
        return {"message_id": next(sent_widgets)} if endpoint == "sendWidget" else True

    context.bot.do_api_request = AsyncMock(side_effect=do_api_request)
    context.bot.delete_message = AsyncMock()
    context.bot.username = "test_bot"

    # Default to no job queue so vote handlers take the inline-render fallback
    # path (which calls edit_message_text directly, as these tests assert).
    # Debounce-specific tests inject their own fake job queue.
    context.job_queue = None

    context.args = []
    return context


class BallotDriver:
    """Acts as a voter's ballot widget: sends the queries Telegram would deliver."""

    def __init__(self, context):
        self.context = context

    def api_calls(self, endpoint: str) -> list[dict]:
        """api_kwargs of every raw Bot API call made to one endpoint, in order."""
        return [
            call.kwargs["api_kwargs"]
            for call in self.context.bot.do_api_request.call_args_list
            if call.args[0] == endpoint
        ]

    async def open(self, poll_id: str, user_id: int):
        """Give a user an up-to-date ballot on record. Returns (ballot, message_id)."""
        async with db.AsyncSessionLocal() as session:
            game_poll = await session.get(GameNightPoll, poll_id)
            valid, priority_ids = await handlers.get_session_valid_games(session, game_poll.chat_id)
            games = await handlers._merge_added_games(session, poll_id, valid)
            session_obj = await session.get(Session, game_poll.chat_id)
            ballot = handlers._build_poll_ballot(game_poll, games, priority_ids, session_obj)
            row = await session.scalar(
                select(PollBallot).where(
                    PollBallot.poll_id == poll_id, PollBallot.user_id == user_id
                )
            )
            if row is None:
                row = PollBallot(
                    poll_id=poll_id,
                    user_id=user_id,
                    message_id=5000 + user_id,
                    catalog_hash=ballot.catalog_hash,
                )
                session.add(row)
            else:
                row.catalog_hash = ballot.catalog_hash
            await session.commit()
            return ballot, row.message_id

    async def send(self, user_id: int, message_id: int, data: str, first_name="Voter") -> dict:
        """Deliver one widget query and return the state the bot answered with."""
        query = {
            "id": "query-1",
            "from": {"id": user_id, "is_bot": False, "first_name": first_name},
            "chat": {"id": user_id, "type": "private", "first_name": first_name},
            "message_id": message_id,
            "chat_instance": "1",
            "type": {"type": "data"},
            "data": data,
        }
        update = Update.de_json({"update_id": 1, "widget_query": query}, None)
        answered = len(self.api_calls("answerWidgetQuery"))
        await handlers.widget_query_callback(update, self.context)
        answers = self.api_calls("answerWidgetQuery")
        assert len(answers) == answered + 1, "every widget query must be answered exactly once"
        state: dict = json.loads(answers[-1]["data"])
        return state

    async def tap(
        self, poll_id: str, user_id: int, *, game_id: int | None = None, level: int | None = None
    ) -> dict:
        """Tap a game (or a complexity level's random row) on the user's ballot."""
        ballot, message_id = await self.open(poll_id, user_id)
        data = f"c:{level}" if game_id is None else f"v:{ballot.game_ids.index(game_id)}"
        return await self.send(user_id, message_id, data)


@pytest.fixture
def refresh_poll(mock_context):
    """Re-render a poll the way any vote, join or collection change does."""

    async def refresh(poll_id: str) -> None:
        async with db.AsyncSessionLocal() as session:
            game_poll = await session.get(GameNightPoll, poll_id)
            games, priority_ids = await handlers.get_session_valid_games(session, game_poll.chat_id)
            await handlers.render_poll_message(
                mock_context.bot,
                game_poll.chat_id,
                game_poll.message_id,
                session,
                poll_id,
                games,
                priority_ids,
            )

    return refresh


@pytest.fixture
def ballot(mock_context):
    """Vote the way players do now: through their ballot widget."""
    return BallotDriver(mock_context)
