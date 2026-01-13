from unittest.mock import AsyncMock, patch

import pytest

from src.bot.handlers import priority_game
from src.core.models import Collection, Game


class MockAsyncSession:
    def __init__(self, result_data=None):
        self.result_data = result_data or []
        self.execute = AsyncMock()
        # Setup execute return value
        mock_result = AsyncMock()
        mock_result.all = lambda: self.result_data
        # execute() returns a coroutine that returns result
        self.execute.return_value = mock_result
        self.commit = AsyncMock()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass


@pytest.mark.asyncio
async def test_priority_game_scoping():
    """Test that priority_game only finds games in user's collection."""
    mock_update = AsyncMock()
    mock_context = AsyncMock()
    mock_context.args = ["Nemesis"]
    mock_update.effective_user.id = 12345

    # Mock DB returns empty list
    mock_session = MockAsyncSession(result_data=[])

    with patch("src.bot.handlers.db.AsyncSessionLocal", return_value=mock_session):
        await priority_game(mock_update, mock_context)

    # Verify we get "not found in your collection"
    mock_update.message.reply_text.assert_called_with("Game not found in your collection.")


@pytest.mark.asyncio
async def test_priority_game_exact_match():
    """Test that exact match is preferred over partial matches."""
    mock_update = AsyncMock()
    mock_context = AsyncMock()
    mock_context.args = ["Nemesis"]
    mock_update.effective_user.id = 12345

    # Mock games
    game1 = Game(id=1, name="Nemesis")
    col1 = Collection(user_id=12345, game_id=1, is_priority=False)

    game2 = Game(id=2, name="Nemesis: Expansion")
    col2 = Collection(user_id=12345, game_id=2, is_priority=False)

    mock_session = MockAsyncSession(result_data=[(col1, game1), (col2, game2)])

    with patch("src.bot.handlers.db.AsyncSessionLocal", return_value=mock_session):
        await priority_game(mock_update, mock_context)

    # Should toggle priority on game1 (exact match)
    assert col1.is_priority is True
    assert col2.is_priority is False
    mock_session.commit.assert_called_once()
    mock_update.message.reply_text.assert_called_with("⭐ Game 'Nemesis' is now HIGH PRIORITY!")


@pytest.mark.asyncio
async def test_priority_game_ambiguous():
    """Test behavior when multiple partial matches exist without exact match."""
    mock_update = AsyncMock()
    mock_context = AsyncMock()
    mock_context.args = ["Catan"]
    mock_update.effective_user.id = 12345

    game1 = Game(id=1, name="Catan: Cities")
    col1 = Collection(user_id=12345, game_id=1)

    game2 = Game(id=2, name="Catan: Seafarers")
    col2 = Collection(user_id=12345, game_id=2)

    mock_session = MockAsyncSession(result_data=[(col1, game1), (col2, game2)])

    with patch("src.bot.handlers.db.AsyncSessionLocal", return_value=mock_session):
        await priority_game(mock_update, mock_context)

    # Should ask for specificity
    mock_update.message.reply_text.assert_called_with(
        "Found multiple games in your collection: Catan: Cities, Catan: Seafarers. Be more specific."
    )
