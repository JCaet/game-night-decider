import logging

from sqlalchemy import delete, func, select
from sqlalchemy import update as sa_update
from sqlalchemy.orm import selectinload
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from src.core import db
from src.core.bgg import BGGClient
from src.core.logic import split_games
from src.core.models import Collection, Game, Session, SessionPlayer, User

logger = logging.getLogger(__name__)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Welcome message."""
    """Welcome message."""
    # Try to send photo if it exists, otherwise fall back to text
    import os

    banner_path = "assets/welcome_banner.png"

    caption = (
        "🎲 **Welcome to Game Night Decider!**\n\n"
        'I\'m here to solve the _"What should we play?"_ dilemma.\n\n'
        "**Quick Start:**\n"
        "1️⃣ /setbgg `<username>` - Sync your collection\n"
        "2️⃣ /gamenight - Open a lobby for friends to join\n"
        "3️⃣ /poll - Let democracy decide!\n\n"
        "**Other Commands:**\n"
        "• /addgame `<name>` - Search BGG and add game\n"
        "• /markplayed `<game>` - Mark game as played\n"
        "• /help - Show all available commands\n\n"
        "_Add me to a group chat for the best experience!_"
    )

    if os.path.exists(banner_path):
        await update.message.reply_photo(
            photo=open(banner_path, "rb"), caption=caption, parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(caption, parse_mode="Markdown")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show help message."""
    help_text = (
        "📚 **Game Night Decider - Command List**\n\n"
        "**Setup & Profile:**\n"
        "• /setbgg `<username>` - Link your BoardGameGeek account\n"
        "• /addgame `<name>` - Add a game to your collection (searches BGG)\n"
        "• /exclude `<game>` - Exclude a game from future polls\n"
        "• /priority `<game>` - Toggle high priority ⭐ on a game\n"
        "• /markplayed `<game>` - Mark a game as played\n\n"
        "**Game Night:**\n"
        "• /gamenight - Start a new game night lobby\n"
        "• /poll - Create a poll from joined players' collections\n"
        "• /addguest `<name>` - Add a guest player\n"
        "• /guestgame `<name> <game>` - Add game to guest's list\n\n"
        "**Other:**\n"
        "• /help - Show this message\n"
        "• /start - Show welcome message"
    )
    await update.message.reply_text(help_text, parse_mode="Markdown")


async def set_bgg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Link BGG username."""
    if not context.args:
        await update.message.reply_text(
            "Usage: /setbgg <username> [force]\n\n"
            "Add 'force' to update existing games with fresh data from BGG."
        )
        return

    bgg_username = context.args[0].strip()
    force_update = len(context.args) > 1 and context.args[1].lower() == "force"
    telegram_id = update.effective_user.id

    async with db.AsyncSessionLocal() as session:
        # Check if user exists
        stmt = select(User).where(User.telegram_id == telegram_id)
        result = await session.execute(stmt)
        user = result.scalar_one_or_none()

        if not user:
            user = User(telegram_id=telegram_id, telegram_name=update.effective_user.first_name)
            session.add(user)

        user.bgg_username = bgg_username
        user.telegram_name = update.effective_user.first_name  # Update name on each call
        await session.commit()

    # Show initial feedback
    mode_text = " (force update)" if force_update else ""
    await update.message.reply_text(
        f"Linked BGG account: {bgg_username}. Fetching collection{mode_text}..."
    )

    try:
        bgg = BGGClient()
        games = await bgg.fetch_collection(bgg_username)

        async with db.AsyncSessionLocal() as session:
            # Re-fetch user to attach to session
            user = await session.get(User, telegram_id)
            if not user:
                return

            # Get current collection to track changes
            current_stmt = select(Collection).where(Collection.user_id == telegram_id)
            current_collections = (await session.execute(current_stmt)).scalars().all()
            current_collection_ids = {c.game_id for c in current_collections}

            # Get BGG game IDs
            bgg_game_ids = {g.id for g in games}

            # Calculate differences
            new_game_ids = bgg_game_ids - current_collection_ids
            removed_game_ids = current_collection_ids - bgg_game_ids
            updated_count = 0

            # Add/Update games in DB
            for g in games:
                existing_game = await session.get(Game, g.id)
                if not existing_game:
                    session.add(g)
                elif force_update:
                    # Update existing game with fresh data
                    existing_game.name = g.name
                    existing_game.min_players = g.min_players
                    existing_game.max_players = g.max_players
                    existing_game.playing_time = g.playing_time
                    existing_game.complexity = g.complexity
                    existing_game.thumbnail = g.thumbnail
                    updated_count += 1

            # Add new games to collection
            for game_id in new_game_ids:
                col = Collection(user_id=telegram_id, game_id=game_id)
                session.add(col)

            # Remove games no longer in BGG collection
            for game_id in removed_game_ids:
                delete_stmt = delete(Collection).where(
                    Collection.user_id == telegram_id, Collection.game_id == game_id
                )
                await session.execute(delete_stmt)

            await session.commit()

        # Build informative feedback message
        total_games = len(games)
        new_count = len(new_game_ids)
        removed_count = len(removed_game_ids)

        if new_count > 0 or removed_count > 0 or updated_count > 0:
            parts = [f"Collection synced! {total_games} games total"]
            changes = []
            if new_count > 0:
                changes.append(f"{new_count} new")
            if updated_count > 0:
                changes.append(f"{updated_count} updated")
            if removed_count > 0:
                changes.append(f"{removed_count} removed")
            if changes:
                parts.append(f"({', '.join(changes)})")
            message = " ".join(parts)
        else:
            message = f"Collection synced! {total_games} games total (no changes)"

        await update.message.reply_text(message)

    except ValueError as e:
        # User not found
        logger.warning(f"BGG user not found: {bgg_username}")
        await update.message.reply_text(f"❌ {str(e)}\n\nPlease check the username and try again.")
    except Exception as e:
        logger.error(f"Failed to fetch collection for {bgg_username}: {e}")
        await update.message.reply_text(
            "Failed to fetch collection from BGG. The service might be temporarily unavailable. Please try again later."
        )


async def start_night(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start a lobby."""
    chat_id = update.effective_chat.id

    async with db.AsyncSessionLocal() as session:
        stmt = select(Session).where(Session.chat_id == chat_id)
        result = await session.execute(stmt)
        db_session = result.scalar_one_or_none()

        # Check if there's an active session with players
        if db_session and db_session.is_active:
            players_stmt = (
                select(SessionPlayer)
                .where(SessionPlayer.session_id == chat_id)
                .options(selectinload(SessionPlayer.user))
            )
            players = (await session.execute(players_stmt)).scalars().all()

            if players:
                # There's an active game night with players - ask for confirmation
                names = []
                for p in players:
                    name = p.user.telegram_name or p.user.bgg_username or f"User {p.user_id}"
                    if p.user.is_guest:
                        name += " 👤"
                    names.append(name)

                keyboard = [
                    [
                        InlineKeyboardButton("Resume", callback_data="resume_night"),
                        InlineKeyboardButton("End & Start New", callback_data="restart_night"),
                    ]
                ]
                await update.message.reply_text(
                    f"⚠️ **Game Night Already Running!**\n\n"
                    f"**Current players ({len(names)}):**\n"
                    + "\n".join([f"- {n}" for n in names])
                    + "\n\nResume or start a new one?",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode="Markdown",
                )
                return

        # No active session or no players - start fresh
        if not db_session:
            db_session = Session(chat_id=chat_id)
            session.add(db_session)

        # Clear players
        await session.execute(delete(SessionPlayer).where(SessionPlayer.session_id == chat_id))

        # Clean up orphaned guests from previous session
        await session.execute(delete(User).where(User.is_guest == True))

        db_session.is_active = True
        await session.commit()

    # Send welcome banner first (if exists)
    from pathlib import Path

    # Robustly find assets directory relative to this file
    # src/bot/handlers.py -> .../src/bot -> .../src -> .../ -> assets
    project_root = Path(__file__).parent.parent.parent
    banner_path = project_root / "assets" / "welcome_banner.png"

    if banner_path.exists():
        await update.message.reply_photo(
            photo=open(banner_path, "rb"),
            caption="🎲 **Game Night Started!**",
            parse_mode="Markdown",
        )

    keyboard = [
        [
            InlineKeyboardButton("Join", callback_data="join_lobby"),
            InlineKeyboardButton("Leave", callback_data="leave_lobby"),
        ],
        [InlineKeyboardButton("📊 Poll", callback_data="start_poll")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_night")],
    ]
    await update.message.reply_text(
        "Who is in?", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown"
    )


async def join_lobby_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle join button."""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    chat_id = query.message.chat.id

    async with db.AsyncSessionLocal() as session:
        # Get or create user (no BGG required to join!)
        user = await session.get(User, user_id)
        if not user:
            user = User(telegram_id=user_id, telegram_name=query.from_user.first_name)
            session.add(user)
            await session.commit()
        else:
            # Update telegram name if changed
            if user.telegram_name != query.from_user.first_name:
                user.telegram_name = query.from_user.first_name
                await session.commit()

        # Check existing join
        stmt = select(SessionPlayer).where(
            SessionPlayer.session_id == chat_id, SessionPlayer.user_id == user_id
        )
        if (await session.execute(stmt)).scalar_one_or_none():
            return  # Already joined

        player = SessionPlayer(session_id=chat_id, user_id=user_id)
        session.add(player)
        await session.commit()

        # Notify chat that a new user has joined
        await context.bot.send_message(
            chat_id=chat_id, text=f"📢 {query.from_user.first_name} has joined the game night!"
        )

        # Update message list
        players_stmt = (
            select(SessionPlayer)
            .where(SessionPlayer.session_id == chat_id)
            .options(selectinload(SessionPlayer.user))
        )
        players = (await session.execute(players_stmt)).scalars().all()

        # Use Telegram name (fallback to BGG username if not set)
        names = []
        for p in players:
            name = p.user.telegram_name or p.user.bgg_username or f"User {p.user_id}"
            if p.user.is_guest:
                name += " 👤"
            names.append(name)

    keyboard = [
        [
            InlineKeyboardButton("Join", callback_data="join_lobby"),
            InlineKeyboardButton("Leave", callback_data="leave_lobby"),
        ],
        [InlineKeyboardButton("📊 Poll", callback_data="start_poll")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_night")],
    ]
    await query.edit_message_text(
        f"🎲 **Game Night Started!**\n\n**Joined ({len(names)}):**\n"
        + "\n".join([f"- {n}" for n in names]),
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


async def leave_lobby_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle leave button."""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    chat_id = query.message.chat.id

    async with db.AsyncSessionLocal() as session:
        # Check if user is in the session
        stmt = select(SessionPlayer).where(
            SessionPlayer.session_id == chat_id, SessionPlayer.user_id == user_id
        )
        player = (await session.execute(stmt)).scalar_one_or_none()

        if not player:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"❌ {query.from_user.first_name}, you are not in this game night!",
            )
            return

        # Remove player from session
        await session.delete(player)

        # Check if the user is a guest - if so, clean up their collection
        user = await session.get(User, user_id)
        if user and user.is_guest:
            # Delete guest's collection entries
            await session.execute(delete(Collection).where(Collection.user_id == user_id))
            # Delete the guest user
            await session.delete(user)

        await session.commit()

        # Notify chat that user has left
        await context.bot.send_message(
            chat_id=chat_id, text=f"👋 {query.from_user.first_name} has left the game night."
        )

        # Update message list
        players_stmt = (
            select(SessionPlayer)
            .where(SessionPlayer.session_id == chat_id)
            .options(selectinload(SessionPlayer.user))
        )
        players = (await session.execute(players_stmt)).scalars().all()

        # Build names list
        names = []
        for p in players:
            name = p.user.telegram_name or p.user.bgg_username or f"User {p.user_id}"
            if p.user.is_guest:
                name += " 👤"
            names.append(name)

    keyboard = [
        [
            InlineKeyboardButton("Join", callback_data="join_lobby"),
            InlineKeyboardButton("Leave", callback_data="leave_lobby"),
        ],
        [InlineKeyboardButton("📊 Poll", callback_data="start_poll")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_night")],
    ]

    if names:
        message_text = f"🎲 **Game Night Started!**\n\n**Joined ({len(names)}):**\n" + "\n".join(
            [f"- {n}" for n in names]
        )
    else:
        message_text = "🎲 **Game Night Started!**\n\nWho is in?"

    await query.edit_message_text(
        message_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown"
    )


async def create_poll(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Generate polls based on lobby."""
    chat_id = update.effective_chat.id

    async with db.AsyncSessionLocal() as session:
        # Get Players
        stmt = select(SessionPlayer).where(SessionPlayer.session_id == chat_id)
        players = (await session.execute(stmt)).scalars().all()

        if not players:
            await update.message.reply_text("No players in lobby! Use /gamenight first.")
            return

        if len(players) < 2:
            await update.message.reply_text("Need at least 2 players to start a poll!")
            return

        player_count = len(players)
        player_ids = [p.user_id for p in players]

        # Check if players collectively have at least 1 game
        total_games_query = select(func.count(Collection.game_id.distinct())).where(
            Collection.user_id.in_(player_ids)
        )
        total_games_result = await session.execute(total_games_query)
        total_games = total_games_result.scalar() or 0

        if total_games == 0:
            await update.message.reply_text(
                "No games in any player's collection! Use /setbgg or /addgame to add games first."
            )
            return

        # Find games owned by ANY player that support the current player count
        # Changed from strict intersection to union - typical game nights don't require
        # everyone to own every game, just that someone owns it

        query = (
            select(Game)
            .join(Collection)
            .where(
                Collection.user_id.in_(player_ids),
                Collection.is_excluded == False,
                Game.min_players <= player_count,
                Game.max_players >= player_count,
            )
            .distinct()
        )

        # SQLAlchemy func needs import
        # Let's import func at top

        result = await session.execute(query)
        valid_games = result.scalars().all()

        # Get games marked as priority by ANY player in session
        priority_query = (
            select(Collection.game_id)
            .where(Collection.user_id.in_(player_ids), Collection.is_priority == True)
            .distinct()
        )
        priority_result = await session.execute(priority_query)
        priority_game_ids = set(priority_result.scalars().all())

    if not valid_games:
        await update.message.reply_text(
            f"No games found matching {player_count} players (intersection of collections)."
        )
        return

    # Filter/Sort
    chunks = split_games(valid_games)

    for label, games_chunk in chunks:
        options = []
        for g in games_chunk:
            # formatting - add ⭐ for priority games
            name = g.name
            if g.id in priority_game_ids:
                name = f"⭐ {name}"
            options.append(name)

        # Send Poll
        # Telegram Poll max 10 options. split_games handles this.
        if len(options) > 10:
            # Fallback if split_games returned > 10 (should not happen if logic is right)
            options = options[:10]

        await context.bot.send_poll(
            chat_id=chat_id,
            question=f"Vote: {label}",
            options=options,
            is_anonymous=False,
            allows_multiple_answers=True,
        )


async def mark_played(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mark a game as played (remove 'New' status)."""
    if not context.args:
        await update.message.reply_text("Usage: /markplayed <game name>")
        return

    game_name_query = " ".join(context.args)
    user_id = update.effective_user.id

    async with db.AsyncSessionLocal() as session:
        # Find the game first (exact match or like?)
        # Let's do simple ILIKE
        stmt = select(Game).where(Game.name.ilike(f"%{game_name_query}%"))
        result = await session.execute(stmt)
        games = result.scalars().all()

        if not games:
            await update.message.reply_text("Game not found.")
            return

        if len(games) > 1:
            await update.message.reply_text(
                f"Found multiple games: {', '.join([g.name for g in games])}. Be more specific."
            )
            return

        game = games[0]

        # Update Collection entry for this user
        # NOTE: 'is_new' is per user collection in our model.
        # If the group played it, should we mark it played for EVERYONE?
        # The prompt said: "bot knows which game won/was played... /markplayed <game> used after session to update history".
        # If I mark it played, presumably it's no longer "New" for anyone who played it?
        # But we only track `Collection` (User-Game).
        # Let's Update it for ALL users in the current active session?
        # Or just the user running the command?
        # "Option B: A command like /markplayed <game_name> ... to update the history"
        # Let's assume it updates for the user running it, or maybe all players in session.
        # Safe bet: Update for user running it.

        # Update collection
        upd = (
            sa_update(Collection)
            .where(Collection.user_id == user_id, Collection.game_id == game.id)
            .values(is_new=False)
        )
        await session.execute(upd)
        await session.commit()

        await update.message.reply_text(f"Marked '{game.name}' as played!")


async def exclude_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Exclude a game from polls."""
    if not context.args:
        await update.message.reply_text("Usage: /exclude <game name>")
        return

    game_name_query = " ".join(context.args)
    user_id = update.effective_user.id

    async with db.AsyncSessionLocal() as session:
        stmt = select(Game).where(Game.name.ilike(f"%{game_name_query}%"))
        games = (await session.execute(stmt)).scalars().all()

        if not games:
            await update.message.reply_text("Game not found.")
            return

        if len(games) > 1:
            await update.message.reply_text(
                f"Found multiple games: {', '.join([g.name for g in games])}. Be more specific."
            )
            return

        game = games[0]

        # Toggle exclusion
        # First check current state
        col_stmt = select(Collection).where(
            Collection.user_id == user_id, Collection.game_id == game.id
        )
        col = (await session.execute(col_stmt)).scalar_one_or_none()

        if col:
            new_state = not col.is_excluded
            col.is_excluded = new_state
            await session.commit()
            status = "EXCLUDED" if new_state else "INCLUDED"
            await update.message.reply_text(f"Game '{game.name}' is now {status}.")
        else:
            await update.message.reply_text("You don't own this game.")


async def priority_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle priority status on a game."""
    if not context.args:
        await update.message.reply_text("Usage: /priority <game name>")
        return

    game_name_query = " ".join(context.args)
    user_id = update.effective_user.id

    async with db.AsyncSessionLocal() as session:
        # Search ONLY within user's collection
        # We need the Collection object to update it, but we want to match on Game.name
        stmt = (
            select(Collection, Game)
            .join(Game)
            .where(Collection.user_id == user_id, Game.name.ilike(f"%{game_name_query}%"))
        )
        results = (await session.execute(stmt)).all()

        if not results:
            await update.message.reply_text("Game not found in your collection.")
            return

        # Results is list of (Collection, Game) tuples
        collections_games = [(r[0], r[1]) for r in results]

        # Smart Matching Logic
        # 1. Check for exact match (case-insensitive)
        exact_match = next(
            (pair for pair in collections_games if pair[1].name.lower() == game_name_query.lower()),
            None,
        )

        if exact_match:
            target_col, target_game = exact_match
            # Toggle priority directly
            new_state = not target_col.is_priority
            target_col.is_priority = new_state
            await session.commit()

            if new_state:
                await update.message.reply_text(
                    f"⭐ Game '{target_game.name}' is now HIGH PRIORITY!"
                )
            else:
                await update.message.reply_text(f"Game '{target_game.name}' priority removed.")

        elif len(collections_games) == 1:
            target_col, target_game = collections_games[0]
            # Toggle priority directly
            new_state = not target_col.is_priority
            target_col.is_priority = new_state
            await session.commit()

            if new_state:
                await update.message.reply_text(
                    f"⭐ Game '{target_game.name}' is now HIGH PRIORITY!"
                )
            else:
                await update.message.reply_text(f"Game '{target_game.name}' priority removed.")
        else:
            # Multiple matches and no exact match - Show Buttons!
            keyboard = []
            for col, game in collections_games[:10]:  # Limit to 10 buttons
                keyboard.append([InlineKeyboardButton(game.name, callback_data=f"prio:{game.id}")])

            await update.message.reply_text(
                f"Found multiple games matching '{game_name_query}'. Select one:",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )


async def priority_select_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle priority selection button."""
    query = update.callback_query
    await query.answer()

    # data is "prio:<game_id>"
    try:
        game_id = int(query.data.split(":")[1])
    except (ValueError, IndexError):
        await query.edit_message_text("Invalid game ID.")
        return

    user_id = query.from_user.id

    async with db.AsyncSessionLocal() as session:
        col_stmt = (
            select(Collection, Game)
            .join(Game)
            .where(Collection.user_id == user_id, Collection.game_id == game_id)
        )
        result = (await session.execute(col_stmt)).first()

        if not result:
            await query.edit_message_text("Game not found in your collection.")
            return

        col, game = result

        # Toggle priority
        new_state = not col.is_priority
        col.is_priority = new_state
        await session.commit()

        if new_state:
            text = f"⭐ Game '{game.name}' is now HIGH PRIORITY!"
        else:
            text = f"Game '{game.name}' priority removed."

        await query.edit_message_text(text)


async def add_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add a game to collection. Searches BGG when only name is provided."""
    if not context.args:
        await update.message.reply_text(
            "Usage:\n"
            "• /addgame <name> - Search BGG and add\n"
            "• /addgame <name> <min> <max> <complexity> - Add manually\n\n"
            "Example: /addgame Catan\n"
            "Example: /addgame MyGame 2 6 2.5"
        )
        return

    args = context.args
    telegram_id = update.effective_user.id

    # If only name provided (1-2 args), search BGG
    # Name can be multi-word like "Ticket to Ride", so we join if len > 1 but no numeric args
    # Better heuristic: if len(args) >= 3 and args[1] is numeric, it's manual mode
    is_manual_mode = len(args) >= 3 and args[1].isdigit()

    if is_manual_mode:
        # Manual mode: /addgame Name 3 4 2.3
        name = args[0]
        min_players = int(args[1])
        max_players = int(args[2]) if len(args) > 2 else 6
        complexity = float(args[3]) if len(args) > 3 else 2.5

        async with db.AsyncSessionLocal() as session:
            user = await session.get(User, telegram_id)
            if not user:
                user = User(telegram_id=telegram_id, telegram_name=update.effective_user.first_name)
                session.add(user)

            # Generate negative ID for manual games
            import hashlib

            game_id = -abs(int(hashlib.md5(name.encode()).hexdigest()[:8], 16))

            existing = await session.get(Game, game_id)
            if not existing:
                game = Game(
                    id=game_id,
                    name=name,
                    min_players=min_players,
                    max_players=max_players,
                    playing_time=60,
                    complexity=complexity,
                )
                session.add(game)

            col_stmt = select(Collection).where(
                Collection.user_id == telegram_id, Collection.game_id == game_id
            )
            if not (await session.execute(col_stmt)).scalar_one_or_none():
                col = Collection(user_id=telegram_id, game_id=game_id)
                session.add(col)

            await session.commit()

        await update.message.reply_text(f"Added '{name}' to your collection (manual entry).")
        return

    # BGG search mode: /addgame Catan or /addgame Ticket to Ride
    search_query = " ".join(args)
    await update.message.reply_text(f"🔍 Searching BGG for '{search_query}'...")

    try:
        bgg = BGGClient()
        # Search for more results to find exact match
        results = await bgg.search_games(search_query, limit=10)

        if not results:
            await update.message.reply_text(
                f"Could not find '{search_query}' on BGG.\n"
                f"Try manual entry: /addgame {args[0]} 2 6 2.5"
            )
            return

        # Normalize name for comparison (remove punctuation, extra spaces)
        def normalize_name(name: str) -> str:
            import re

            # Remove punctuation except alphanumeric and spaces
            normalized = re.sub(r"[^\w\s]", "", name)
            # Normalize whitespace
            normalized = " ".join(normalized.lower().split())
            return normalized

        # Look for exact match (case-insensitive, punctuation-insensitive)
        search_normalized = normalize_name(search_query)
        exact_match = next(
            (g for g in results if normalize_name(g["name"]) == search_normalized), None
        )

        if exact_match:
            bgg_id = exact_match["id"]

            # Check if game exists locally first (cache check)
            async with db.AsyncSessionLocal() as session:
                existing_game = await session.get(Game, bgg_id)

            if existing_game:
                # Use cached game, skip API call
                game = existing_game
                game_data = None  # Mark as using cache
            else:
                # Fetch from BGG
                game_data = await bgg.get_game_details(bgg_id)

                if not game_data:
                    # Should not happen if search found it, but theoretically possible
                    await update.message.reply_text("Error fetching details for the game from BGG.")
                    return
                game = game_data
        else:
            # No exact match found - do not guess!
            # List suggestions
            suggestions = "\n".join([f"• {g['name']}" for g in results[:5]])
            await update.message.reply_text(
                f"Found similar games, but no exact match for '{search_query}'.\n"
                f"Did you mean:\n{suggestions}\n\n"
                "Please use the exact name."
            )
            return
        # We have 'game' from either cache (existing_game) or from BGG (game_data)

        async with db.AsyncSessionLocal() as session:
            # If we fetched from BGG, add the game
            if game_data is not None:
                session.add(game)

            # Add to database (User & Collection)
            # async with db.AsyncSessionLocal() as session:  <-- already in session
            user = await session.get(User, telegram_id)
            if not user:
                user = User(telegram_id=telegram_id, telegram_name=update.effective_user.first_name)
                session.add(user)

            # existing = await session.get(Game, game.id) <-- logic handled above
            # if not existing: session.add(game)

            col_stmt = select(Collection).where(
                Collection.user_id == telegram_id, Collection.game_id == game.id
            )
            if not (await session.execute(col_stmt)).scalar_one_or_none():
                col = Collection(user_id=telegram_id, game_id=game.id)
                session.add(col)

            await session.commit()

            # Extract data for reply before session closes
            g_name = game.name
            g_min = game.min_players
            g_max = game.max_players
            g_comp = game.complexity
            g_time = game.playing_time

        await update.message.reply_text(
            f"✅ Added '{g_name}' to your collection!\n\n"
            f"📊 **Details from BGG:**\n"
            f"• Players: {g_min}-{g_max}\n"
            f"• Complexity: {g_comp:.2f}/5\n"
            f"• Play time: {g_time} min",
            parse_mode="Markdown",
        )

    except Exception as e:
        logger.error(f"Error adding game from BGG: {e}")
        await update.message.reply_text(
            f"Error searching BGG. Try manual entry:\n/addgame {args[0]} 2 6 2.5"
        )


async def test_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add fake test users with collections for testing."""
    chat_id = update.effective_chat.id

    # Parse optional player count argument (default: 2, range: 1-10)
    num_players = 2
    if context.args:
        try:
            num_players = int(context.args[0])
            num_players = max(1, min(10, num_players))  # Clamp between 1-10
        except ValueError:
            await update.message.reply_text(
                "Usage: /testmode [number_of_players]\nExample: /testmode 4"
            )
            return

    try:
        async with db.AsyncSessionLocal() as session:
            # Create fake users based on requested count
            fake_users = [(999000 + i, f"TestUser{i}") for i in range(1, num_players + 1)]

            # Create some test games
            test_games = [
                Game(
                    id=-1001,
                    name="Test Catan",
                    min_players=3,
                    max_players=4,
                    playing_time=60,
                    complexity=2.3,
                ),
                Game(
                    id=-1002,
                    name="Test Ticket to Ride",
                    min_players=2,
                    max_players=5,
                    playing_time=45,
                    complexity=1.8,
                ),
                Game(
                    id=-1003,
                    name="Test Wingspan",
                    min_players=1,
                    max_players=5,
                    playing_time=60,
                    complexity=2.4,
                ),
                Game(
                    id=-1004,
                    name="Test Gloomhaven",
                    min_players=1,
                    max_players=4,
                    playing_time=120,
                    complexity=3.9,
                ),
            ]

            for game in test_games:
                existing = await session.get(Game, game.id)
                if not existing:
                    session.add(game)

            for user_id, name in fake_users:
                existing_user = await session.get(User, user_id)
                if not existing_user:
                    user = User(telegram_id=user_id, telegram_name=name)
                    session.add(user)
                    await session.flush()

                # Add all test games to their collection (ensure for both new and existing users)
                for game in test_games:
                    # Check if collection exists
                    stmt = select(Collection).where(
                        Collection.user_id == user_id, Collection.game_id == game.id
                    )
                    existing_col = (await session.execute(stmt)).scalar_one_or_none()

                    if not existing_col:
                        col = Collection(user_id=user_id, game_id=game.id)
                        session.add(col)

            # Ensure session exists and is active
            db_session = await session.get(Session, chat_id)
            if not db_session:
                db_session = Session(chat_id=chat_id)
                session.add(db_session)

            # Clear existing session players to start fresh
            await session.execute(delete(SessionPlayer).where(SessionPlayer.session_id == chat_id))
            # Clean up orphaned guests from previous sessions
            await session.execute(delete(User).where(User.is_guest == True))

            db_session.is_active = True

            # Add fake users to the lobby
            for user_id, _ in fake_users:
                sp = SessionPlayer(session_id=chat_id, user_id=user_id)
                session.add(sp)

            # Also add the current user to the lobby
            calling_user_id = update.effective_user.id
            calling_user = await session.get(User, calling_user_id)
            if not calling_user:
                calling_user = User(
                    telegram_id=calling_user_id, telegram_name=update.effective_user.first_name
                )
                session.add(calling_user)

            # Check if calling user is already in session (re-add safety)
            sp_stmt = select(SessionPlayer).where(
                SessionPlayer.session_id == chat_id, SessionPlayer.user_id == calling_user_id
            )
            if not (await session.execute(sp_stmt)).scalar_one_or_none():
                sp = SessionPlayer(session_id=chat_id, user_id=calling_user_id)
                session.add(sp)

            await session.commit()

        # Build user list for display
        user_names = ", ".join([name for _, name in fake_users])

        await update.message.reply_text(
            f"🧪 **Test Mode Activated!**\n\n"
            f"Added {num_players} fake users ({user_names}) with 4 test games:\n"
            "- Test Catan (3-4p, 2.3 complexity)\n"
            "- Test Ticket to Ride (2-5p, 1.8)\n"
            "- Test Wingspan (1-5p, 2.4)\n"
            "- Test Gloomhaven (1-4p, 3.9)\n\n"
            "Use /poll to start voting!",
            parse_mode="Markdown",
        )

    except Exception as e:
        logger.error(f"Error in test_mode: {e}", exc_info=True)
        await update.message.reply_text(f"❌ Error activating test mode: {e}")


async def add_guest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add a guest participant to the current session."""
    if not context.args:
        await update.message.reply_text("Usage: /addguest <name>")
        return

    guest_name = " ".join(context.args)
    chat_id = update.effective_chat.id
    added_by = update.effective_user.id

    # Generate unique negative ID for guest (hash of name + timestamp)
    import hashlib
    import time

    guest_id = -abs(int(hashlib.md5(f"{guest_name}{time.time()}".encode()).hexdigest()[:8], 16))

    async with db.AsyncSessionLocal() as session:
        # Ensure session exists
        db_session = await session.get(Session, chat_id)
        if not db_session or not db_session.is_active:
            await update.message.reply_text("No active game night! Use /gamenight first.")
            return

        # Create guest user
        guest = User(
            telegram_id=guest_id, telegram_name=guest_name, is_guest=True, added_by_user_id=added_by
        )
        session.add(guest)
        await session.flush()

        # Add to session
        sp = SessionPlayer(session_id=chat_id, user_id=guest_id)
        session.add(sp)
        await session.commit()

    await update.message.reply_text(
        f"👤 Guest **{guest_name}** added!\n\n"
        f"Use `/guestgame {guest_name} <game>` to add their games.",
        parse_mode="Markdown",
    )


async def guest_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add a game to a guest's collection."""
    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage: /guestgame <guest_name> <game_name> [min] [max] [complexity]\n"
            "Example: /guestgame John Doe Catan 3 4 2.3"
        )
        return

    args = list(context.args)
    numeric_values = []

    # extracted numeric args from the end (max 3: complexity, max, min)
    # We iterate backwards
    while args and len(numeric_values) < 3:
        last_arg = args[-1]
        try:
            val = float(last_arg)
            # If it's the 1st or 2nd arg (min/max), it should be int-able conceptually
            # but float parsing is safe generic check.
            numeric_values.insert(0, args.pop())
        except ValueError:
            break

    # Assign defaults
    min_players = 2
    max_players = 6
    complexity = 2.5

    if len(numeric_values) >= 1:
        min_players = int(float(numeric_values[0]))
    if len(numeric_values) >= 2:
        max_players = int(float(numeric_values[1]))
    if len(numeric_values) >= 3:
        complexity = float(numeric_values[2])

    # Remaining args are "Guest Name Game Name"
    if not args:
        await update.message.reply_text("Please provide guest name and game name.")
        return

    full_text = " ".join(args)
    chat_id = update.effective_chat.id

    async with db.AsyncSessionLocal() as session:
        # Fetch all guests in the current session
        stmt = (
            select(User)
            .join(SessionPlayer, User.telegram_id == SessionPlayer.user_id)
            .where(SessionPlayer.session_id == chat_id, User.is_guest == True)
        )
        guests = (await session.execute(stmt)).scalars().all()

        if not guests:
            await update.message.reply_text("No guests found in this session.")
            return

        # Find matching guest (longest prefix match)
        # e.g. text="John Doe Catan", guests=["John", "John Doe"]
        # "John Doe" is longer match than "John".

        matched_guest = None
        game_name_str = ""

        # Sort guests by name length descending to ensure longest match first
        guests_sorted = sorted(guests, key=lambda g: len(g.telegram_name), reverse=True)

        for guest in guests_sorted:
            g_name = guest.telegram_name
            # Case insensitive check
            if full_text.lower().startswith(g_name.lower()):
                # Potential match. verify boundary (space or end of string)
                # "John" matches "Johnny" prefix? No, we want distinct words if possible,
                # but "John" matches "John Catan"

                # Check if it's the whole string or followed by space
                remaining = full_text[len(g_name) :]
                if not remaining or remaining.startswith(" "):
                    matched_guest = guest
                    game_name_str = remaining.strip()
                    break

        if not matched_guest:
            names = [g.telegram_name for g in guests]
            await update.message.reply_text(
                f"Could not find a matching guest in: {full_text}.\nActive guests: {', '.join(names)}"
            )
            return

        guest_display_name = matched_guest.telegram_name

        if not game_name_str:
            await update.message.reply_text(
                f"Found guest '{guest_display_name}' but no game name provided."
            )
            return

        # Try to find game in local DB first!
        # This allows guests to link to real BGG games if they exist in DB
        game = None

        # Simple exact match (case insensitive)
        game_stmt = select(Game).where(Game.name.ilike(game_name_str))
        game_result = await session.execute(game_stmt)
        found_games = game_result.scalars().all()

        if found_games:
            # Pick the best match? Prioritize real BGG games (positive ID)
            found_games.sort(key=lambda x: x.id > 0, reverse=True)
            game = found_games[0]

        if not game:
            # Not found, create manual manual game
            import hashlib

            game_id = -abs(int(hashlib.md5(game_name_str.encode()).hexdigest()[:8], 16))

            existing_game = await session.get(Game, game_id)
            if not existing_game:
                game = Game(
                    id=game_id,
                    name=game_name_str,
                    min_players=min_players,
                    max_players=max_players,
                    playing_time=60,
                    complexity=complexity,
                )
                session.add(game)
            else:
                game = existing_game

        col_stmt = select(Collection).where(
            Collection.user_id == matched_guest.telegram_id, Collection.game_id == game.id
        )
        if not (await session.execute(col_stmt)).scalar_one_or_none():
            col = Collection(user_id=matched_guest.telegram_id, game_id=game.id)
            session.add(col)

        await session.commit()

        # Extract for reply
        final_game_name = game.name

    await update.message.reply_text(
        f"Added '{final_game_name}' to {guest_display_name}'s collection!"
    )


async def resume_night_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle resume button - just show current lobby state."""
    query = update.callback_query
    await query.answer()

    chat_id = query.message.chat.id

    async with db.AsyncSessionLocal() as session:
        players_stmt = (
            select(SessionPlayer)
            .where(SessionPlayer.session_id == chat_id)
            .options(selectinload(SessionPlayer.user))
        )
        players = (await session.execute(players_stmt)).scalars().all()

        names = []
        for p in players:
            name = p.user.telegram_name or p.user.bgg_username or f"User {p.user_id}"
            if p.user.is_guest:
                name += " 👤"
            names.append(name)

    keyboard = [
        [
            InlineKeyboardButton("Join", callback_data="join_lobby"),
            InlineKeyboardButton("Leave", callback_data="leave_lobby"),
        ],
        [InlineKeyboardButton("📊 Poll", callback_data="start_poll")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_night")],
    ]

    if names:
        message_text = f"🎲 **Game Night Resumed!**\n\n**Joined ({len(names)}):**\n" + "\n".join(
            [f"- {n}" for n in names]
        )
    else:
        message_text = "🎲 **Game Night Started!**\n\nWho is in?"

    await query.edit_message_text(
        message_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown"
    )


async def restart_night_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle restart button - clear and start fresh."""
    query = update.callback_query
    await query.answer()

    chat_id = query.message.chat.id

    async with db.AsyncSessionLocal() as session:
        db_session = await session.get(Session, chat_id)

        if db_session:
            # Clear players
            await session.execute(delete(SessionPlayer).where(SessionPlayer.session_id == chat_id))

            # Clean up orphaned guests
            await session.execute(delete(User).where(User.is_guest == True))

            db_session.is_active = True
            await session.commit()

    keyboard = [
        [
            InlineKeyboardButton("Join", callback_data="join_lobby"),
            InlineKeyboardButton("Leave", callback_data="leave_lobby"),
        ],
        [InlineKeyboardButton("📊 Poll", callback_data="start_poll")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_night")],
    ]

    await query.edit_message_text(
        "🎲 **Game Night Started!**\n\nWho is in?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


async def start_poll_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle poll button - create poll from callback."""
    query = update.callback_query
    await query.answer()

    chat_id = query.message.chat.id

    async with db.AsyncSessionLocal() as session:
        # Get Players
        stmt = select(SessionPlayer).where(SessionPlayer.session_id == chat_id)
        players = (await session.execute(stmt)).scalars().all()

        if not players:
            await context.bot.send_message(
                chat_id=chat_id, text="No players in lobby! Click Join first."
            )
            return

        if len(players) < 2:
            await context.bot.send_message(
                chat_id=chat_id, text="Need at least 2 players to start a poll!"
            )
            return

        player_count = len(players)
        player_ids = [p.user_id for p in players]

        # Check if players collectively have at least 1 game
        total_games_query = select(func.count(Collection.game_id.distinct())).where(
            Collection.user_id.in_(player_ids)
        )
        total_games_result = await session.execute(total_games_query)
        total_games = total_games_result.scalar() or 0

        if total_games == 0:
            await context.bot.send_message(
                chat_id=chat_id,
                text="No games in any player's collection! Use /setbgg or /addgame to add games first.",
            )
            return

        # Find games owned by ANY player that support the current player count
        game_query = (
            select(Game)
            .join(Collection)
            .where(
                Collection.user_id.in_(player_ids),
                Collection.is_excluded.is_(False),
                Game.min_players <= player_count,
                Game.max_players >= player_count,
            )
            .distinct()
        )

        result = await session.execute(game_query)
        valid_games = result.scalars().all()

        # Get games marked as priority by ANY player in session
        priority_query = (
            select(Collection.game_id)
            .where(Collection.user_id.in_(player_ids), Collection.is_priority.is_(True))
            .distinct()
        )
        priority_result = await session.execute(priority_query)
        priority_game_ids = set(priority_result.scalars().all())

    if not valid_games:
        await context.bot.send_message(
            chat_id=chat_id, text=f"No games found matching {player_count} players."
        )
        return

    # Filter/Sort
    chunks = split_games(valid_games)

    for label, games_chunk in chunks:
        options = []
        for g in games_chunk:
            name = g.name
            if g.id in priority_game_ids:
                name = f"⭐ {name}"
            options.append(name)

        # Telegram Poll max 10 options
        if len(options) > 10:
            options = options[:10]

        await context.bot.send_poll(
            chat_id=chat_id,
            question=f"Vote: {label}",
            options=options,
            is_anonymous=False,
            allows_multiple_answers=True,
        )


async def cancel_night_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle cancel button - end the game night."""
    query = update.callback_query
    await query.answer()

    chat_id = query.message.chat.id

    async with db.AsyncSessionLocal() as session:
        db_session = await session.get(Session, chat_id)

        if db_session:
            # Clear players
            await session.execute(delete(SessionPlayer).where(SessionPlayer.session_id == chat_id))

            # Clean up orphaned guests
            await session.execute(delete(User).where(User.is_guest.is_(True)))

            # Mark session as inactive
            db_session.is_active = False
            await session.commit()

    await query.edit_message_text(
        "🎲 **Game Night Cancelled.**\n\nUse /gamenight to start a new one!", parse_mode="Markdown"
    )
