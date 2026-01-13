import pytest
from sqlalchemy import select

from src.bot.handlers import (
    add_guest,
    create_poll,
    guest_game,
    join_lobby_callback,
    leave_lobby_callback,
    start_night,
)
from src.core import db
from src.core.models import Collection, Game, Session, SessionPlayer, User

# ============================================================================
# /gamenight command tests
# ============================================================================


@pytest.mark.asyncio
async def test_startnight_creates_session(mock_update, mock_context):
    """Test /gamenight creates a new session."""
    await start_night(mock_update, mock_context)

    async with db.AsyncSessionLocal() as session:
        sess = await session.get(Session, 12345)
        assert sess is not None
        assert sess.is_active is True


@pytest.mark.asyncio
async def test_startnight_shows_confirmation_when_players_exist(mock_update, mock_context):
    """Test /gamenight shows confirmation when session already has players."""
    # Setup: Create session with a player
    async with db.AsyncSessionLocal() as session:
        sess = Session(chat_id=12345, is_active=True)
        session.add(sess)
        user = User(telegram_id=111, telegram_name="Test")
        session.add(user)
        await session.flush()
        player = SessionPlayer(session_id=12345, user_id=111)
        session.add(player)
        await session.commit()

    # Start new night - should show confirmation instead of clearing
    await start_night(mock_update, mock_context)

    # Verify confirmation message shown with Resume/Restart options
    call_args = mock_update.message.reply_text.call_args
    message_text = call_args.kwargs.get("text") or call_args.args[0]
    assert "Already Running" in message_text
    assert "Test" in message_text  # Should show current player


@pytest.mark.asyncio
async def test_startnight_clears_when_no_players(mock_update, mock_context):
    """Test /gamenight starts fresh when session exists but has no players."""
    # Setup: Create session without players
    async with db.AsyncSessionLocal() as session:
        sess = Session(chat_id=12345, is_active=True)
        session.add(sess)
        await session.commit()

    # Start new night - should start fresh since no players
    await start_night(mock_update, mock_context)

    # Verify standard lobby message shown
    call_args = mock_update.message.reply_text.call_args
    message_text = call_args.kwargs.get("text") or call_args.args[0]
    assert "Who is in?" in message_text


@pytest.mark.asyncio
async def test_startnight_with_guests_shows_confirmation(mock_update, mock_context):
    """Test /gamenight shows confirmation when session has guests."""
    # Setup: Old session with a guest
    async with db.AsyncSessionLocal() as session:
        session.add(Session(chat_id=12345, is_active=True))
        guest = User(telegram_id=-555, telegram_name="OldGuest", is_guest=True)
        session.add(guest)
        session.add(SessionPlayer(session_id=12345, user_id=-555))
        await session.commit()

    # Start new night - should show confirmation since guest exists
    await start_night(mock_update, mock_context)

    # Verify confirmation dialog shown (guest still exists)
    call_args = mock_update.message.reply_text.call_args
    message_text = call_args.kwargs.get("text") or call_args.args[0]
    assert "Already Running" in message_text
    assert "OldGuest" in message_text


# ============================================================================
# join_lobby_callback tests
# ============================================================================


@pytest.mark.asyncio
async def test_join_lobby_creates_user(mock_update, mock_context):
    """Test joining lobby creates user if doesn't exist."""
    # Setup session first
    async with db.AsyncSessionLocal() as session:
        sess = Session(chat_id=12345, is_active=True)
        session.add(sess)
        await session.commit()

    await join_lobby_callback(mock_update, mock_context)

    async with db.AsyncSessionLocal() as session:
        user = await session.get(User, 111)
        assert user is not None
        stmt = select(SessionPlayer).where(
            SessionPlayer.session_id == 12345, SessionPlayer.user_id == 111
        )
        player = (await session.execute(stmt)).scalar_one_or_none()
        assert player is not None


@pytest.mark.asyncio
async def test_join_lobby_updates_telegram_name(mock_update, mock_context):
    """Test joining lobby updates user's telegram name."""
    # Setup: create user with old name
    async with db.AsyncSessionLocal() as session:
        sess = Session(chat_id=12345, is_active=True)
        session.add(sess)
        user = User(telegram_id=111, telegram_name="OldName")
        session.add(user)
        await session.commit()

    mock_update.callback_query.from_user.first_name = "NewName"
    await join_lobby_callback(mock_update, mock_context)

    async with db.AsyncSessionLocal() as session:
        user = await session.get(User, 111)
        assert user.telegram_name == "NewName"


@pytest.mark.asyncio
async def test_join_lobby_prevents_double_join(mock_update, mock_context):
    """Test user can't join lobby twice."""
    # Setup
    async with db.AsyncSessionLocal() as session:
        sess = Session(chat_id=12345, is_active=True)
        session.add(sess)
        user = User(telegram_id=111, telegram_name="Test")
        session.add(user)
        await session.commit()

    # Join twice
    await join_lobby_callback(mock_update, mock_context)
    await join_lobby_callback(mock_update, mock_context)

    async with db.AsyncSessionLocal() as session:
        stmt = select(SessionPlayer).where(
            SessionPlayer.session_id == 12345, SessionPlayer.user_id == 111
        )
        players = (await session.execute(stmt)).scalars().all()
        assert len(players) == 1  # Only one entry


@pytest.mark.asyncio
async def test_join_lobby_sends_notification(mock_update, mock_context):
    """Test joining lobby sends notification to chat."""
    chat_id = 12345
    user_id = 999

    # Setup session
    async with db.AsyncSessionLocal() as session:
        session.add(Session(chat_id=chat_id, is_active=True))
        await session.commit()

    mock_update.callback_query.message.chat.id = chat_id
    mock_update.callback_query.from_user.id = user_id
    mock_update.callback_query.from_user.first_name = "NewUser"

    await join_lobby_callback(mock_update, mock_context)

    # Verify notification sent
    mock_context.bot.send_message.assert_called_once()
    call_args = mock_context.bot.send_message.call_args

    # Handle both positional and keyword possibilities for robustness
    if "text" in call_args.kwargs:
        text = call_args.kwargs["text"]
    elif len(call_args.args) >= 2:
        text = call_args.args[1]
    else:
        text = ""

    assert "joined the game night" in text
    assert "NewUser" in text


# ============================================================================
# leave_lobby_callback tests
# ============================================================================


@pytest.mark.asyncio
async def test_leave_lobby_removes_player(mock_update, mock_context):
    """Test leaving lobby removes player from session."""
    # Setup: Create session with a player
    async with db.AsyncSessionLocal() as session:
        sess = Session(chat_id=12345, is_active=True)
        session.add(sess)
        user = User(telegram_id=111, telegram_name="TestUser")
        session.add(user)
        await session.flush()
        player = SessionPlayer(session_id=12345, user_id=111)
        session.add(player)
        await session.commit()

    await leave_lobby_callback(mock_update, mock_context)

    # Verify player removed
    async with db.AsyncSessionLocal() as session:
        stmt = select(SessionPlayer).where(
            SessionPlayer.session_id == 12345, SessionPlayer.user_id == 111
        )
        player = (await session.execute(stmt)).scalar_one_or_none()
        assert player is None


@pytest.mark.asyncio
async def test_leave_lobby_not_in_session(mock_update, mock_context):
    """Test leaving when not in session shows error."""
    # Setup: Create session but no player
    async with db.AsyncSessionLocal() as session:
        sess = Session(chat_id=12345, is_active=True)
        session.add(sess)
        await session.commit()

    await leave_lobby_callback(mock_update, mock_context)

    # Verify error message sent
    mock_context.bot.send_message.assert_called_once()
    call_kwargs = mock_context.bot.send_message.call_args.kwargs
    assert "not in this game night" in call_kwargs["text"]


@pytest.mark.asyncio
async def test_leave_lobby_guest_cleanup(mock_update, mock_context):
    """Test leaving as guest cleans up collection and user."""
    # Setup: Create guest user with games
    async with db.AsyncSessionLocal() as session:
        sess = Session(chat_id=12345, is_active=True)
        session.add(sess)

        # Create guest user
        guest = User(
            telegram_id=111, telegram_name="GuestUser", is_guest=True, added_by_user_id=999
        )
        session.add(guest)
        await session.flush()

        # Add game to guest's collection
        game = Game(
            id=-1, name="GuestGame", min_players=2, max_players=4, playing_time=60, complexity=2.0
        )
        session.add(game)
        await session.flush()

        col = Collection(user_id=111, game_id=-1)
        session.add(col)

        # Add guest to session
        player = SessionPlayer(session_id=12345, user_id=111)
        session.add(player)
        await session.commit()

    await leave_lobby_callback(mock_update, mock_context)

    # Verify guest and collection removed
    async with db.AsyncSessionLocal() as session:
        user = await session.get(User, 111)
        assert user is None

        stmt = select(Collection).where(Collection.user_id == 111)
        collections = (await session.execute(stmt)).scalars().all()
        assert len(collections) == 0


@pytest.mark.asyncio
async def test_leave_lobby_updates_message(mock_update, mock_context):
    """Test leaving updates lobby message."""
    # Setup: Create session with two players
    async with db.AsyncSessionLocal() as session:
        sess = Session(chat_id=12345, is_active=True)
        session.add(sess)

        u1 = User(telegram_id=111, telegram_name="User1")
        u2 = User(telegram_id=222, telegram_name="User2")
        session.add_all([u1, u2])
        await session.flush()

        sp1 = SessionPlayer(session_id=12345, user_id=111)
        sp2 = SessionPlayer(session_id=12345, user_id=222)
        session.add_all([sp1, sp2])
        await session.commit()

    await leave_lobby_callback(mock_update, mock_context)

    # Verify message was updated
    mock_update.callback_query.edit_message_text.assert_called_once()
    call_args = mock_update.callback_query.edit_message_text.call_args[0][0]

    # Should show remaining player
    assert "User2" in call_args
    # Original user should not be listed
    assert "User1" not in call_args or "Joined (1)" in call_args


@pytest.mark.asyncio
async def test_leave_lobby_last_player_updates_message(mock_update, mock_context):
    """Test leaving as last player shows empty lobby message."""
    # Setup: Create session with one player
    async with db.AsyncSessionLocal() as session:
        sess = Session(chat_id=12345, is_active=True)
        session.add(sess)

        user = User(telegram_id=111, telegram_name="TestUser")
        session.add(user)
        await session.flush()

        player = SessionPlayer(session_id=12345, user_id=111)
        session.add(player)
        await session.commit()

    await leave_lobby_callback(mock_update, mock_context)

    # Verify message shows empty lobby
    mock_update.callback_query.edit_message_text.assert_called_once()
    call_args = mock_update.callback_query.edit_message_text.call_args[0][0]
    assert "Who is in?" in call_args


@pytest.mark.asyncio
async def test_leave_lobby_sends_notification(mock_update, mock_context):
    """Test leaving sends notification to chat."""
    # Setup: Create session with player
    async with db.AsyncSessionLocal() as session:
        sess = Session(chat_id=12345, is_active=True)
        session.add(sess)

        user = User(telegram_id=111, telegram_name="TestUser")
        session.add(user)
        await session.flush()

        player = SessionPlayer(session_id=12345, user_id=111)
        session.add(player)
        await session.commit()

    await leave_lobby_callback(mock_update, mock_context)

    # Verify leave notification sent
    calls = mock_context.bot.send_message.call_args_list
    leave_call = calls[0]  # First call should be the leave notification

    if "text" in leave_call.kwargs:
        text = leave_call.kwargs["text"]
    elif len(leave_call.args) >= 2:
        text = leave_call.args[1]
    else:
        text = ""

    assert "has left the game night" in text
    assert "TestUser" in text


# ============================================================================
# Guest Feature Tests
# ============================================================================


@pytest.mark.asyncio
async def test_addguest_no_args(mock_update, mock_context):
    """Test /addguest without name shows usage."""
    mock_context.args = []
    await add_guest(mock_update, mock_context)

    # Check that reply contains "Usage"
    mock_update.message.reply_text.assert_called_once()
    assert "Usage" in mock_update.message.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_addguest_no_session(mock_update, mock_context):
    """Test /addguest without active session shows error."""
    # Ensure no session exists
    mock_context.args = ["GuestName"]

    await add_guest(mock_update, mock_context)

    mock_update.message.reply_text.assert_called_with("No active game night! Use /gamenight first.")


@pytest.mark.asyncio
async def test_addguest_success(mock_update, mock_context):
    """Test /addguest successfully adds a guest."""
    # Setup session
    async with db.AsyncSessionLocal() as session:
        session.add(Session(chat_id=12345, is_active=True))
        await session.commit()

    mock_context.args = ["John Doe"]
    mock_update.effective_user.id = 111  # Added by User 111

    await add_guest(mock_update, mock_context)

    # Verify response
    call_args = mock_update.message.reply_text.call_args[0][0]
    assert "Guest **John Doe** added!" in call_args

    # Verify DB
    async with db.AsyncSessionLocal() as session:
        # Check User created
        stmt = select(User).where(User.telegram_name == "John Doe", User.is_guest.is_(True))
        guest = (await session.execute(stmt)).scalar_one_or_none()
        assert guest is not None
        assert guest.added_by_user_id == 111
        assert guest.telegram_id < 0  # Should be negative

        # Check added to session
        sp_stmt = select(SessionPlayer).where(
            SessionPlayer.session_id == 12345, SessionPlayer.user_id == guest.telegram_id
        )
        sp = (await session.execute(sp_stmt)).scalar_one_or_none()
        assert sp is not None


@pytest.mark.asyncio
async def test_guestgame_success(mock_update, mock_context):
    """Test /guestgame adds game to guest collection (single word name)."""
    # Setup session and guest
    async with db.AsyncSessionLocal() as session:
        session.add(Session(chat_id=12345, is_active=True))
        guest = User(telegram_id=-123, telegram_name="GuestJohn", is_guest=True)
        session.add(guest)
        session.add(SessionPlayer(session_id=12345, user_id=-123))
        await session.commit()

    # Args: GuestName GameName Min Max Complexity
    mock_context.args = ["GuestJohn", "Catan", "3", "4", "2.5"]

    await guest_game(mock_update, mock_context)

    mock_update.message.reply_text.assert_called_with("Added 'Catan' to GuestJohn's collection!")

    # Verify collection
    async with db.AsyncSessionLocal() as session:
        # Find game
        g_stmt = select(Game).where(Game.name == "Catan")
        game = (await session.execute(g_stmt)).scalar_one()

        # Check collection
        stmt = select(Collection).where(Collection.user_id == -123, Collection.game_id == game.id)
        col = (await session.execute(stmt)).scalar_one_or_none()
        assert col is not None


@pytest.mark.asyncio
async def test_guestgame_uses_existing_game(mock_update, mock_context):
    """Test /guestgame links to existing game instead of creating manual entry."""
    # Setup session and guest
    async with db.AsyncSessionLocal() as session:
        session.add(Session(chat_id=12345, is_active=True))
        guest = User(telegram_id=-555, telegram_name="GuestUser", is_guest=True)
        session.add(guest)
        session.add(SessionPlayer(session_id=12345, user_id=-555))

        # Pre-populate exact match game (Real BGG ID)
        real_game = Game(
            id=999, name="RealGame", min_players=2, max_players=4, playing_time=60, complexity=3.0
        )
        session.add(real_game)
        await session.commit()

    mock_context.args = ["GuestUser", "RealGame"]

    await guest_game(mock_update, mock_context)

    async with db.AsyncSessionLocal() as session:
        # Check collection links to ID 999
        stmt = select(Collection).where(Collection.user_id == -555, Collection.game_id == 999)
        col = (await session.execute(stmt)).scalar_one_or_none()
        assert col is not None

        # Ensure no manual game was created (count games with name "RealGame")
        g_stmt = select(Game).where(Game.name == "RealGame")
        games = (await session.execute(g_stmt)).scalars().all()
        assert len(games) == 1
        assert games[0].id == 999


@pytest.mark.asyncio
async def test_guestgame_multiword_names(mock_update, mock_context):
    """Test /guestgame with multi-word guest name and game name."""
    # Setup session and guest
    async with db.AsyncSessionLocal() as session:
        session.add(Session(chat_id=12345, is_active=True))
        # Multi-word name
        guest = User(telegram_id=-456, telegram_name="John Doe", is_guest=True)
        session.add(guest)
        session.add(SessionPlayer(session_id=12345, user_id=-456))
        await session.commit()

    # Args: John Doe Ticket to Ride 2 5
    mock_context.args = ["John", "Doe", "Ticket", "to", "Ride", "2", "5"]

    await guest_game(mock_update, mock_context)

    call_args = mock_update.message.reply_text.call_args[0][0]
    assert "Added 'Ticket to Ride' to John Doe's collection!" in call_args

    # Verify
    async with db.AsyncSessionLocal() as session:
        g_stmt = select(Game).where(Game.name == "Ticket to Ride")
        game = (await session.execute(g_stmt)).scalar_one_or_none()
        assert game is not None
        assert game.min_players == 2
        assert game.max_players == 5

        # Verify collection
        c_stmt = select(Collection).where(Collection.user_id == -456, Collection.game_id == game.id)
        assert (await session.execute(c_stmt)).scalar_one_or_none() is not None


# ============================================================================
# /poll command tests
# ============================================================================


@pytest.mark.asyncio
async def test_poll_no_players(mock_update, mock_context):
    """Test /poll with no players shows error."""
    async with db.AsyncSessionLocal() as session:
        sess = Session(chat_id=12345, is_active=True)
        session.add(sess)
        await session.commit()

    await create_poll(mock_update, mock_context)

    mock_update.message.reply_text.assert_called_with("No players in lobby! Use /gamenight first.")


@pytest.mark.asyncio
async def test_poll_only_one_player(mock_update, mock_context):
    """Test /poll with only one player shows error."""
    async with db.AsyncSessionLocal() as session:
        sess = Session(chat_id=12345, is_active=True)
        session.add(sess)
        user = User(telegram_id=111, telegram_name="Test")
        session.add(user)
        await session.flush()
        player = SessionPlayer(session_id=12345, user_id=111)
        session.add(player)
        await session.commit()

    await create_poll(mock_update, mock_context)

    mock_update.message.reply_text.assert_called_with("Need at least 2 players to start a poll!")


@pytest.mark.asyncio
async def test_poll_no_games_in_collections(mock_update, mock_context):
    """Test /poll with players who have no games in their collections."""
    async with db.AsyncSessionLocal() as session:
        sess = Session(chat_id=12345, is_active=True)
        session.add(sess)
        # Two users with no games
        u1 = User(telegram_id=111, telegram_name="User1")
        u2 = User(telegram_id=222, telegram_name="User2")
        session.add_all([u1, u2])
        await session.flush()

        # No games added to collections
        sp1 = SessionPlayer(session_id=12345, user_id=111)
        sp2 = SessionPlayer(session_id=12345, user_id=222)
        session.add_all([sp1, sp2])
        await session.commit()

    await create_poll(mock_update, mock_context)

    mock_update.message.reply_text.assert_called_with(
        "No games in any player's collection! Use /setbgg or /addgame to add games first."
    )


@pytest.mark.asyncio
async def test_poll_no_matching_games(mock_update, mock_context):
    """Test /poll with games that don't support the player count."""
    async with db.AsyncSessionLocal() as session:
        sess = Session(chat_id=12345, is_active=True)
        session.add(sess)
        # Two users
        u1 = User(telegram_id=111, telegram_name="User1")
        u2 = User(telegram_id=222, telegram_name="User2")
        session.add_all([u1, u2])
        await session.flush()

        # Games that require 3-4 players (won't match 2 players)
        g1 = Game(id=1, name="Game1", min_players=3, max_players=4, playing_time=60, complexity=2.0)
        g2 = Game(id=2, name="Game2", min_players=3, max_players=4, playing_time=60, complexity=2.0)
        session.add_all([g1, g2])
        await session.flush()

        # Both users have these games but they don't support 2 players
        c1 = Collection(user_id=111, game_id=1)
        c2 = Collection(user_id=222, game_id=2)
        session.add_all([c1, c2])

        sp1 = SessionPlayer(session_id=12345, user_id=111)
        sp2 = SessionPlayer(session_id=12345, user_id=222)
        session.add_all([sp1, sp2])
        await session.commit()

    await create_poll(mock_update, mock_context)

    call_args = mock_update.message.reply_text.call_args[0][0]
    assert "No games found matching 2 players" in call_args


@pytest.mark.asyncio
async def test_poll_with_valid_games(mock_update, mock_context):
    """Test /poll creates poll with valid intersecting games."""
    async with db.AsyncSessionLocal() as session:
        sess = Session(chat_id=12345, is_active=True)
        session.add(sess)

        u1 = User(telegram_id=111, telegram_name="User1")
        u2 = User(telegram_id=222, telegram_name="User2")
        session.add_all([u1, u2])
        await session.flush()

        game = Game(
            id=1, name="SharedGame", min_players=2, max_players=4, playing_time=60, complexity=2.5
        )
        session.add(game)
        await session.flush()

        # Both users own the same game
        c1 = Collection(user_id=111, game_id=1)
        c2 = Collection(user_id=222, game_id=1)
        session.add_all([c1, c2])

        sp1 = SessionPlayer(session_id=12345, user_id=111)
        sp2 = SessionPlayer(session_id=12345, user_id=222)
        session.add_all([sp1, sp2])
        await session.commit()

    await create_poll(mock_update, mock_context)

    mock_context.bot.send_poll.assert_called_once()
    call_kwargs = mock_context.bot.send_poll.call_args[1]
    assert "SharedGame" in call_kwargs["options"][0]


@pytest.mark.asyncio
async def test_poll_excludes_excluded_games(mock_update, mock_context):
    """Test /poll respects is_excluded flag."""
    async with db.AsyncSessionLocal() as session:
        sess = Session(chat_id=12345, is_active=True)
        session.add(sess)

        u1 = User(telegram_id=111, telegram_name="User1")
        u2 = User(telegram_id=222, telegram_name="User2")
        session.add_all([u1, u2])
        await session.flush()

        game = Game(
            id=1, name="ExcludedGame", min_players=2, max_players=4, playing_time=60, complexity=2.5
        )
        session.add(game)
        await session.flush()

        # Both users have excluded the game
        c1 = Collection(user_id=111, game_id=1, is_excluded=True)
        c2 = Collection(user_id=222, game_id=1, is_excluded=True)
        session.add_all([c1, c2])

        sp1 = SessionPlayer(session_id=12345, user_id=111)
        sp2 = SessionPlayer(session_id=12345, user_id=222)
        session.add_all([sp1, sp2])
        await session.commit()

    await create_poll(mock_update, mock_context)

    # Should not find any games (all excluded)
    mock_context.bot.send_poll.assert_not_called()
