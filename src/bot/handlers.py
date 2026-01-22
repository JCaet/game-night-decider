import contextlib
import logging
import math
import random
from collections import namedtuple

from sqlalchemy import delete, func, select
from sqlalchemy.orm import selectinload
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from src.core import db
from src.core.bgg import BGGClient
from src.core.logic import (
    STAR_BOOST,
    calculate_poll_winner,
    group_games_by_complexity,
    split_games,
)
from src.core.models import (
    Collection,
    Expansion,
    Game,
    GameNightPoll,
    GameState,
    PollType,
    PollVote,
    Session,
    SessionPlayer,
    User,
    UserExpansion,
    VoteLimit,
)

# Named tuple for resolved votes (after category resolution)
ResolvedVote = namedtuple("ResolvedVote", ["game_id", "user_id"])

logger = logging.getLogger(__name__)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        "• /manage - Toggle game availability (⬜→🌟→❌)\n"
        "• /help - Show all available commands\n\n"
        "_Add me to a group chat for the best experience!_"
    )

    if os.path.exists(banner_path):
        with open(banner_path, "rb") as f:
            await update.message.reply_photo(photo=f, caption=caption, parse_mode="Markdown")
    else:
        await update.message.reply_text(caption, parse_mode="Markdown")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show help message."""
    help_text = (
        "📚 **Game Night Decider - Command List**\n\n"
        "**Setup & Profile:**\n"
        "• /setbgg `<username>` - Link your BoardGameGeek account\n"
        "• /addgame `<name>` - Add a game to your collection (searches BGG)\n"
        "• /manage - Manage collection (⬜ Included → 🌟 Starred → ❌ Excluded)\n\n"
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

    # Show initial feedback and keep reference to message to update it
    mode_text = " (force update)" if force_update else ""
    status_msg = await update.message.reply_text(
        f"⏳ Linked BGG account: {bgg_username}. Syncing collection{mode_text}..."
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

            # Determine if this is a "safe" sync where we should auto-star new games
            # Don't auto-star on: first sync (no existing games) or force sync
            is_first_sync = len(current_collection_ids) == 0
            should_auto_star = not is_first_sync and not force_update

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
                    existing_game.thumbnail = g.thumbnail
                    # Only update complexity if we got a valid value from collection API
                    if g.complexity and g.complexity > 0:
                        existing_game.complexity = g.complexity
                    updated_count += 1

            # Add new games to collection
            for game_id in new_game_ids:
                # Auto-star new games in incremental syncs
                initial_state = GameState.STARRED if should_auto_star else GameState.INCLUDED
                col = Collection(user_id=telegram_id, game_id=game_id, state=initial_state)
                session.add(col)

            # Remove games no longer in BGG collection
            for game_id in removed_game_ids:
                delete_stmt = delete(Collection).where(
                    Collection.user_id == telegram_id, Collection.game_id == game_id
                )
                await session.execute(delete_stmt)

            await session.commit()

            # Find ALL games in collection that still need complexity
            games_needing_complexity = []
            for g in games:
                db_game = await session.get(Game, g.id)
                if db_game and (not db_game.complexity or db_game.complexity <= 0):
                    games_needing_complexity.append(g.id)

            # Fetch detailed complexity for games that need it
            complexity_updated = 0
            if games_needing_complexity:
                # Update status message instead of sending new one
                with contextlib.suppress(Exception):
                    await status_msg.edit_text(
                        f"⏳ Linked BGG account: {bgg_username}\n"
                        f"• Fetching computed complexity for {len(games_needing_complexity)} "
                        "games..."
                    )

                import asyncio

                for game_id in games_needing_complexity:
                    try:
                        details = await bgg.get_game_details(game_id)
                        if details and details.complexity and details.complexity > 0:
                            game_obj = await session.get(Game, game_id)
                            if game_obj:
                                game_obj.complexity = details.complexity
                                complexity_updated += 1
                        await asyncio.sleep(0.5)  # Rate limit
                    except Exception as e:
                        logger.warning(f"Failed to fetch complexity for game {game_id}: {e}")
                        continue
                await session.commit()

        # Gather collection stats
        total_games = len(games)
        new_count = len(new_game_ids)
        removed_count = len(removed_game_ids)

        # Phase 2: Fetch and sync expansions
        expansions_processed = 0
        player_count_updates = 0

        try:
            # Update status for expansion sync
            with contextlib.suppress(Exception):
                await status_msg.edit_text(
                    f"⏳ Linked BGG account: {bgg_username}\n• Syncing expansions..."
                )

            expansions_data = await bgg.fetch_expansions(bgg_username)

            if expansions_data:
                import asyncio

                async with db.AsyncSessionLocal() as session:
                    for exp_data in expansions_data:
                        try:
                            # Get expansion details (base game link, player count)
                            exp_info = await bgg.get_expansion_info(exp_data["id"])
                            await asyncio.sleep(0.3)  # Rate limit

                            if not exp_info or not exp_info.get("base_game_id"):
                                continue

                            base_game_id = exp_info["base_game_id"]

                            # Check if base game is in user's collection
                            base_game = await session.get(Game, base_game_id)
                            if not base_game:
                                continue

                            # Create or update expansion record
                            existing_exp = await session.get(Expansion, exp_data["id"])
                            if not existing_exp:
                                expansion = Expansion(
                                    id=exp_data["id"],
                                    name=exp_info["name"],
                                    base_game_id=base_game_id,
                                    new_max_players=exp_info.get("new_max_players"),
                                    complexity_delta=None,  # Future use
                                )
                                session.add(expansion)
                            else:
                                # Update existing expansion
                                existing_exp.name = exp_info["name"]
                                existing_exp.base_game_id = base_game_id
                                existing_exp.new_max_players = exp_info.get("new_max_players")

                            # Link expansion to user
                            user_exp_stmt = select(UserExpansion).where(
                                UserExpansion.user_id == telegram_id,
                                UserExpansion.expansion_id == exp_data["id"],
                            )
                            if not (await session.execute(user_exp_stmt)).scalar_one_or_none():
                                user_exp = UserExpansion(
                                    user_id=telegram_id,
                                    expansion_id=exp_data["id"],
                                )
                                session.add(user_exp)

                            # Update effective_max_players on Collection if expansion adds players
                            exp_max = exp_info.get("new_max_players")
                            if exp_max and exp_max > base_game.max_players:
                                col_stmt = select(Collection).where(
                                    Collection.user_id == telegram_id,
                                    Collection.game_id == base_game_id,
                                )
                                collection_entry = (
                                    await session.execute(col_stmt)
                                ).scalar_one_or_none()
                                if collection_entry:
                                    # Update if new max is higher than current effective
                                    current_eff = (
                                        collection_entry.effective_max_players
                                        or base_game.max_players
                                    )
                                    if exp_max > current_eff:
                                        collection_entry.effective_max_players = exp_max
                                        player_count_updates += 1

                            expansions_processed += 1

                        except Exception as e:
                            logger.warning(f"Failed to process expansion {exp_data.get('id')}: {e}")
                            continue

                    await session.commit()

        except Exception as e:
            logger.warning(f"Expansion sync failed (non-critical): {e}")
            # Don't fail the whole sync if expansion sync fails

        # --- Final Summary Message ---
        summary_lines = ["✅ **Sync Complete!**"]

        # Collection stats
        collection_details = []
        if new_count > 0:
            collection_details.append(f"{new_count} new")
        if removed_count > 0:
            collection_details.append(f"{removed_count} removed")
        if updated_count > 0:
            collection_details.append(f"{updated_count} updated")

        col_line = f"• **Collection:** {total_games} games"
        if collection_details:
            col_line += f" ({', '.join(collection_details)})"
        summary_lines.append(col_line)

        # Complexity stats
        if complexity_updated > 0:
            summary_lines.append(f"• **Complexity:** Updated for {complexity_updated} games")

        # Expansion stats
        if expansions_processed > 0:
            exp_line = f"• **Expansions:** {expansions_processed} synced"
            if player_count_updates > 0:
                exp_line += f" ({player_count_updates} player count updates)"
            summary_lines.append(exp_line)

        final_message = "\n".join(summary_lines)

        # Try to edit the status message, fallback to reply
        try:
            await status_msg.edit_text(final_message, parse_mode="Markdown")
        except Exception:
            await update.message.reply_text(final_message, parse_mode="Markdown")

    except ValueError as e:
        # User not found
        logger.warning(f"BGG user not found: {bgg_username}")
        await update.message.reply_text(f"❌ {str(e)}\n\nPlease check the username and try again.")
    except Exception as e:
        logger.error(f"Failed to fetch collection for {bgg_username}: {e}")
        await update.message.reply_text(
            "Failed to fetch collection from BGG. "
            "The service might be temporarily unavailable. Please try again later."
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
        await session.execute(delete(User).where(User.is_guest))

        db_session.is_active = True
        await session.commit()

    # Send welcome banner first (if exists)
    from pathlib import Path

    # Robustly find assets directory relative to this file
    # src/bot/handlers.py -> .../src/bot -> .../src -> .../ -> assets
    project_root = Path(__file__).parent.parent.parent
    banner_path = project_root / "assets" / "welcome_banner.png"

    if banner_path.exists():
        with open(banner_path, "rb") as f:
            await update.message.reply_photo(
                photo=f,
                caption="🎲 **Game Night Started!**",
                parse_mode="Markdown",
            )

    keyboard = [
        [
            InlineKeyboardButton("Join", callback_data="join_lobby"),
            InlineKeyboardButton("Leave", callback_data="leave_lobby"),
        ],
        [InlineKeyboardButton("📊 Poll", callback_data="start_poll")],
        [InlineKeyboardButton("⚙️ Poll Settings", callback_data="poll_settings")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_night")],
    ]
    msg = await update.message.reply_text(
        "Who is in?", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown"
    )

    async with db.AsyncSessionLocal() as session:
        session_obj = await session.get(Session, chat_id)
        if session_obj:
            session_obj.message_id = msg.message_id
            await session.commit()


async def join_lobby_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle join button."""
    query = update.callback_query
    # await query.answer() # Moved below for conditional answering

    user = query.from_user
    chat_id = query.message.chat.id
    message_id = query.message.message_id

    async with db.AsyncSessionLocal() as session:
        # Validate Session Message ID
        session_obj = await session.get(Session, chat_id)
        if session_obj and session_obj.message_id and session_obj.message_id != message_id:
            await query.answer(
                "This session is expired. Please use the active Game Night message.",
                show_alert=True,
            )
            return

        stmt = select(User).where(User.telegram_id == user.id)
        result = await session.execute(stmt)
        db_user = result.scalar_one_or_none()

        if not db_user:
            # Create user if not exists
            db_user = User(telegram_id=user.id, telegram_name=user.first_name)
            session.add(db_user)
            await session.commit()
        elif db_user.telegram_name != user.first_name:
            # Update name if changed
            db_user.telegram_name = user.first_name
            session.add(db_user)
            await session.commit()

    await query.answer()

    async with db.AsyncSessionLocal() as session:
        # Check if already joined
        # Need to re-fetch user within this session? No, just use ID.
        player_stmt = select(SessionPlayer).where(
            SessionPlayer.session_id == chat_id, SessionPlayer.user_id == user.id
        )
        existing = (await session.execute(player_stmt)).scalar_one_or_none()

        if existing:
            # Already joined
            pass
        else:
            # Join
            player = SessionPlayer(session_id=chat_id, user_id=user.id)
            session.add(player)
            await session.commit()

            # Send join notification
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"👋 **{user.first_name}** joined the game night!",
                parse_mode="Markdown",
            )

        # Update message
        # Get all players
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

        # Get session settings
        session_obj = await session.get(Session, chat_id)

    keyboard = [
        [
            InlineKeyboardButton("Join", callback_data="join_lobby"),
            InlineKeyboardButton("Leave", callback_data="leave_lobby"),
        ],
        [InlineKeyboardButton("📊 Poll", callback_data="start_poll")],
        [InlineKeyboardButton("⚙️ Poll Settings", callback_data="poll_settings")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_night")],
    ]

    await query.edit_message_text(
        f"🎲 **Game Night Started!**\n\n**Joined ({len(names)}):**\n"
        + "\n".join([f"- {n}" for n in names]),
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )

    # Auto-Refresh Custom Poll if active
    # We open a new session to check for any active polls and update them if needed
    async with db.AsyncSessionLocal() as session:
        active_poll_stmt = select(GameNightPoll).where(GameNightPoll.chat_id == chat_id)
        # Handle potential multiple polls gracefully by taking the most recent (or first)
        # Ideally only one exists, but we don't want to crash.
        active_poll = (await session.execute(active_poll_stmt)).scalars().first()

        if active_poll:
            # Check if session is in Custom mode (heuristic to decide if we should update message)
            # Or just try to update it regardless?
            # If it's a native poll, render_poll_message might fail if we pass a native poll ID?
            # Native IDs are long integers usually? Custom are message IDs (int).
            # Let's check session type.
            session_obj = await session.get(Session, chat_id)
            if session_obj and session_obj.poll_type == PollType.CUSTOM:
                try:
                    valid_games, priority_ids = await get_session_valid_games(session, chat_id)
                    await render_poll_message(
                        context.bot,
                        chat_id,
                        active_poll.message_id,
                        session,
                        active_poll.poll_id,
                        valid_games,
                        priority_ids,
                    )
                except Exception as e:
                    logger.warning(f"Failed to auto-refresh poll on join: {e}")


async def leave_lobby_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle leave button."""
    query = update.callback_query
    # await query.answer() # Moved below for conditional answering

    user_id = query.from_user.id
    chat_id = query.message.chat.id
    message_id = query.message.message_id
    # await query.answer() # Answer below

    async with db.AsyncSessionLocal() as session:
        # Validate Session Message ID
        session_obj = await session.get(Session, chat_id)
        if session_obj and session_obj.message_id and session_obj.message_id != message_id:
            await query.answer("This session is expired.", show_alert=True)
            return

        await query.answer()

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

        # Get session settings
        session_obj = await session.get(Session, chat_id)

    keyboard = [
        [
            InlineKeyboardButton("Join", callback_data="join_lobby"),
            InlineKeyboardButton("Leave", callback_data="leave_lobby"),
        ],
        [InlineKeyboardButton("📊 Poll", callback_data="start_poll")],
        [InlineKeyboardButton("⚙️ Poll Settings", callback_data="poll_settings")],
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
                Collection.state != GameState.EXCLUDED,  # Excludes games in EXCLUDED state
                Game.min_players <= player_count,
                # Use effective_max_players if set (from owned expansions), else base game max
                func.coalesce(Collection.effective_max_players, Game.max_players) >= player_count,
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
            .where(Collection.user_id.in_(player_ids), Collection.state == GameState.STARRED)
            .distinct()
        )
        priority_result = await session.execute(priority_query)
        priority_game_ids = set(priority_result.scalars().all())

    if not valid_games:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"No games found matching {player_count} players (intersection of collections).",
        )
        return

    # Check Poll Mode
    session_obj = await session.get(Session, chat_id)
    if session_obj and session_obj.poll_type == PollType.CUSTOM:
        await create_custom_poll(update, context, session, list(valid_games), priority_game_ids)
        return

    # Filter/Sort
    chunks = split_games(list(valid_games))

    for label, games_chunk in chunks:
        options = []
        for g in games_chunk:
            # formatting - add ⭐ for priority games
            name = g.name
            if g.id in priority_game_ids:
                name = f"⭐ {name}"
            options.append(name)

        # Telegram Poll requires at least 2 options
        if len(options) < 2:
            # Send as message instead of poll
            if len(options) == 1:
                await context.bot.send_message(
                    chat_id=chat_id, text=f"📋 {label}: {options[0]} (only 1 game - no poll needed)"
                )
            continue

        message = await context.bot.send_poll(
            chat_id=chat_id,
            question=f"Vote: {label}",
            options=options,
            is_anonymous=False,
            allows_multiple_answers=True,
        )

        # Save poll to DB
        async with db.AsyncSessionLocal() as session:
            poll = GameNightPoll(
                poll_id=message.poll.id, chat_id=chat_id, message_id=message.message_id
            )
            session.add(poll)
            await session.commit()


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
                # Auto-star manually added games
                col = Collection(user_id=telegram_id, game_id=game_id, state=GameState.STARRED)
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
                # Auto-star manually added games (via search)
                col = Collection(user_id=telegram_id, game_id=game.id, state=GameState.STARRED)
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

            # Delete any existing session completely to start fresh
            existing_session = await session.get(Session, chat_id)
            if existing_session:
                # Clear session players first
                await session.execute(
                    delete(SessionPlayer).where(SessionPlayer.session_id == chat_id)
                )
                # Delete the session itself
                await session.delete(existing_session)
                await session.flush()

            # Clean up orphaned guests from previous sessions
            await session.execute(delete(User).where(User.is_guest))

            # Create a fresh new session
            db_session = Session(chat_id=chat_id, is_active=True)
            session.add(db_session)

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
    numeric_values: list[str] = []

    # extracted numeric args from the end (max 3: complexity, max, min)
    # We iterate backwards
    while args and len(numeric_values) < 3:
        last_arg = args[-1]
        try:
            float(last_arg)
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
            .where(SessionPlayer.session_id == chat_id, User.is_guest)
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
                f"Could not find a matching guest in: {full_text}.\n"
                f"Active guests: {', '.join(names)}"
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
            # scalar() returns a Game object, scalars().all() returns a list
            # We need to sort the list
            found_list = list(found_games)
            found_list.sort(key=lambda x: x.id > 0, reverse=True)
            game = found_list[0]

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
    message_id = query.message.message_id

    async with db.AsyncSessionLocal() as session:
        session_obj = await session.get(Session, chat_id)
        if session_obj:
            # Transfer session ownership to this message
            # (handles "Resume" from start_night conflict)
            session_obj.message_id = message_id
            await session.commit()

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
        [InlineKeyboardButton("⚙️ Poll Settings", callback_data="poll_settings")],
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
            # Cancel the OLD lobby message if it exists
            old_message_id = db_session.message_id
            if old_message_id:
                with contextlib.suppress(Exception):
                    await context.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=old_message_id,
                        text="🎲 **Game Night Cancelled** (A new one was started)",
                        parse_mode="Markdown",
                        reply_markup=None,
                    )

            # Auto-Close Previous Polls
            existing_polls_stmt = select(GameNightPoll).where(GameNightPoll.chat_id == chat_id)
            existing_polls = (await session.execute(existing_polls_stmt)).scalars().all()

            if existing_polls:
                for p in existing_polls:
                    try:
                        await context.bot.stop_poll(chat_id, p.message_id)
                    except Exception:
                        with contextlib.suppress(Exception):
                            await context.bot.edit_message_text(
                                chat_id=chat_id,
                                message_id=p.message_id,
                                text="🛑 **Poll Closed** (Game Night Restarted)",
                                parse_mode="Markdown",
                                reply_markup=None,
                            )
                    await session.delete(p)

            # Clear players
            await session.execute(delete(SessionPlayer).where(SessionPlayer.session_id == chat_id))

            # Clean up orphaned guests
            await session.execute(delete(User).where(User.is_guest))

            db_session.is_active = True
            await session.commit()

    # Get session for settings (not really needed for keyboard anymore but good
    # for consistancy if we add back)
    # Actually, we don't need to fetch session just to show the basic keyboard
    # anymore if weights are gone

    keyboard = [
        [
            InlineKeyboardButton("Join", callback_data="join_lobby"),
            InlineKeyboardButton("Leave", callback_data="leave_lobby"),
        ],
        [InlineKeyboardButton("📊 Poll", callback_data="start_poll")],
        [InlineKeyboardButton("⚙️ Poll Settings", callback_data="poll_settings")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_night")],
    ]

    msg = await query.edit_message_text(
        "🎲 **Game Night Started!**\n\nWho is in?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )

    async with db.AsyncSessionLocal() as session:
        db_session = await session.get(Session, chat_id)
        if db_session and isinstance(msg, object) and hasattr(msg, "message_id"):
            db_session.message_id = msg.message_id
            await session.commit()


async def toggle_weights_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle toggle weights button."""
    query = update.callback_query
    await query.answer()

    chat_id = query.message.chat.id
    message_id = query.message.message_id

    async with db.AsyncSessionLocal() as session:
        session_obj = await session.get(Session, chat_id)
        if session_obj:
            if session_obj.message_id and session_obj.message_id != message_id:
                await query.answer("This session is expired.", show_alert=True)
                return

            session_obj.settings_weighted = not session_obj.settings_weighted
            await session.commit()

            # Refresh SETTINGS view (not lobby)
            is_weighted = session_obj.settings_weighted
            is_custom = session_obj.poll_type == PollType.CUSTOM
            hide_voters = session_obj.hide_voters
            vote_limit = session_obj.vote_limit

            weight_icon = "✅" if is_weighted else "❌"
            mode_text = "Custom (Single)" if is_custom else "Native (Multiple)"
            hide_icon = "✅" if hide_voters else "❌"
            limit_text = get_vote_limit_display(vote_limit)

            keyboard = [
                [InlineKeyboardButton(f"Mode: {mode_text}", callback_data="toggle_poll_mode")],
                [InlineKeyboardButton(f"Weights: {weight_icon}", callback_data="toggle_weights")],
                [
                    InlineKeyboardButton(
                        f"Anonymous Voting: {hide_icon}", callback_data="toggle_hide_voters"
                    )
                ],
                [
                    InlineKeyboardButton(
                        f"Vote Limit: {limit_text}", callback_data="cycle_vote_limit"
                    )
                ],
                [InlineKeyboardButton("🔙 Back to Lobby", callback_data="resume_night")],
            ]

            await query.edit_message_text(
                "**Poll Settings**\n\n"
                "• **Custom (Single)**: One message with buttons. Good for large lists.\n"
                "• **Native (Multiple)**: Standard Telegram polls. Split if >10 games.\n"
                "• **Weights**: Starred games get +0.5 votes.\n"
                "• **Vote Limit**: Max votes per player (Auto scales with game count).",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown",
            )


async def start_poll_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle poll button - create poll from callback."""
    query = update.callback_query
    await query.answer()

    chat_id = query.message.chat.id
    message_id = query.message.message_id

    async with db.AsyncSessionLocal() as session:
        # Validate Session Message ID
        session_obj = await session.get(Session, chat_id)
        if session_obj and session_obj.message_id and session_obj.message_id != message_id:
            await query.answer("This session is expired.", show_alert=True)
            return

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

        # Auto-Close Existing Polls for this session
        existing_polls_stmt = select(GameNightPoll).where(GameNightPoll.chat_id == chat_id)
        existing_polls = (await session.execute(existing_polls_stmt)).scalars().all()

        if existing_polls:
            for p in existing_polls:
                try:
                    # Try to stop native poll
                    await context.bot.stop_poll(chat_id, p.message_id)
                except Exception:
                    # If failed (e.g. not a poll), try to edit message (Custom Poll)
                    with contextlib.suppress(Exception):
                        await context.bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=p.message_id,
                            text="🛑 **Poll Closed** (New poll started)",
                            parse_mode="Markdown",
                            reply_markup=None,
                        )

                await session.delete(p)
            await session.commit()

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
                text=(
                    "No games in any player's collection! "
                    "Use /setbgg or /addgame to add games first."
                ),
            )
            return

        # Find games owned by ANY player that support the current player count
        game_query = (
            select(Game)
            .join(Collection)
            .where(
                Collection.user_id.in_(player_ids),
                Collection.state != GameState.EXCLUDED,
                Game.min_players <= player_count,
                # Use effective_max_players if set (from owned expansions), else base game max
                func.coalesce(Collection.effective_max_players, Game.max_players) >= player_count,
            )
            .distinct()
        )

        result = await session.execute(game_query)
        valid_games = result.scalars().all()

        # Get games marked as priority by ANY player in session
        priority_query = (
            select(Collection.game_id)
            .where(Collection.user_id.in_(player_ids), Collection.state == GameState.STARRED)
            .distinct()
        )
        priority_result = await session.execute(priority_query)
        priority_game_ids = set(priority_result.scalars().all())

    if not valid_games:
        await context.bot.send_message(
            chat_id=chat_id, text=f"No games found matching {player_count} players."
        )
        return

    # Check Poll Mode
    session_obj = await session.get(Session, chat_id)
    if session_obj and session_obj.poll_type == PollType.CUSTOM:
        await create_custom_poll(update, context, session, list(valid_games), priority_game_ids)
        return

    # Filter/Sort
    chunks = split_games(list(valid_games))

    for label, games_chunk in chunks:
        options = []
        for g in games_chunk:
            name = g.name
            if g.id in priority_game_ids:
                name = f"⭐ {name}"
            options.append(name)

        # Telegram Poll requires at least 2 options
        if len(options) < 2:
            # Send as message instead of poll
            if len(options) == 1:
                await context.bot.send_message(
                    chat_id=chat_id, text=f"📋 {label}: {options[0]} (only 1 game - no poll needed)"
                )
            continue

        # Telegram Poll max 10 options
        if len(options) > 10:
            options = options[:10]

        message = await context.bot.send_poll(
            chat_id=chat_id,
            question=f"Vote: {label}",
            options=options,
            is_anonymous=False,
            allows_multiple_answers=True,
        )

        # Save poll to DB
        async with db.AsyncSessionLocal() as session:
            poll = GameNightPoll(
                poll_id=message.poll.id, chat_id=chat_id, message_id=message.message_id
            )
            session.add(poll)
            await session.commit()


async def cancel_night_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle cancel button - end the game night."""
    query = update.callback_query
    await query.answer()

    chat_id = query.message.chat.id
    message_id = query.message.message_id

    async with db.AsyncSessionLocal() as session:
        db_session = await session.get(Session, chat_id)

        # Validate Session Message ID
        if db_session and db_session.message_id and db_session.message_id != message_id:
            await query.answer("This session is expired.", show_alert=True)
            return

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


async def cancel_night(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel the current game night via command."""
    chat_id = update.effective_chat.id

    async with db.AsyncSessionLocal() as session:
        db_session = await session.get(Session, chat_id)

        if not db_session or not db_session.is_active:
            await update.message.reply_text(
                "No active game night to cancel! Use /gamenight to start one."
            )
            return

        # Clear players
        await session.execute(delete(SessionPlayer).where(SessionPlayer.session_id == chat_id))

        # Clean up orphaned guests
        await session.execute(delete(User).where(User.is_guest.is_(True)))

        # Mark session as inactive
        db_session.is_active = False
        await session.commit()

    await update.message.reply_text(
        "🎲 **Game Night Cancelled.**\n\nUse /gamenight to start a new one!",
        parse_mode="Markdown",
    )


async def receive_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle poll answers to track votes and auto-close."""
    answer = update.poll_answer
    poll_id = answer.poll_id
    user_id = answer.user.id
    user_name = answer.user.first_name

    # Store vote
    async with db.AsyncSessionLocal() as session:
        # Check if poll exists in our DB
        stmt = select(GameNightPoll).where(GameNightPoll.poll_id == poll_id)
        game_poll = (await session.execute(stmt)).scalar_one_or_none()

        if not game_poll:
            return  # Not a poll we care about

        chat_id = game_poll.chat_id

        # Get session for weighted settings
        session_obj = await session.get(Session, chat_id)
        is_weighted = session_obj.settings_weighted if session_obj else False

        # Update Vote Record
        # If user retracted vote (empty option_ids), remove them
        if not answer.option_ids:
            await session.execute(
                delete(PollVote).where(PollVote.poll_id == poll_id, PollVote.user_id == user_id)
            )
        else:
            # Upsert vote record (just to track *that* they voted)
            # Check exist
            vote_stmt = select(PollVote).where(
                PollVote.poll_id == poll_id, PollVote.user_id == user_id
            )
            vote = (await session.execute(vote_stmt)).scalar_one_or_none()

            if not vote:
                vote = PollVote(poll_id=poll_id, user_id=user_id, user_name=user_name)
                session.add(vote)

        await session.commit()

        # Check for Auto-Close condition
        # 1. Get total voters for this poll
        voters_count = await session.scalar(
            select(func.count(PollVote.user_id)).where(PollVote.poll_id == poll_id)
        )

        # 2. Get total players in session
        players_count = await session.scalar(
            select(func.count(SessionPlayer.user_id)).where(SessionPlayer.session_id == chat_id)
        )
        # Note: If players_count is 0 (session ended?), we should probably stop.
        if players_count == 0:
            return

        # If all players voted
        if voters_count >= players_count:
            # Close the poll!
            try:
                poll_data = await context.bot.stop_poll(
                    chat_id=chat_id, message_id=game_poll.message_id
                )

                # Calculate Winner using extensible helper
                scores, modifiers_applied = await calculate_winner_scores(
                    poll_data, chat_id, session, is_weighted
                )

                # Find max score
                if not scores:
                    winners = []
                else:
                    max_score = max(scores.values())
                    if max_score > 0:
                        winners = [name for name, s in scores.items() if s == max_score]
                    else:
                        winners = []

                if winners:
                    if len(winners) == 1:
                        text = f"🗳️ **Poll Closed!**\n\n🏆 The winner is: **{winners[0]}**! 🎉"
                    else:
                        text = "🗳️ **Poll Closed!**\n\nIt's a tie between:\n" + "\n".join(
                            [f"• {w}" for w in winners]
                        )

                    if modifiers_applied:
                        text += f"\n_{modifiers_applied}_"
                else:
                    text = "🗳️ **Poll Closed!**\n\nNo votes cast?"

                await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")

                # End the game night session
                session_obj = await session.get(Session, chat_id)
                if session_obj:
                    await session.execute(
                        delete(SessionPlayer).where(SessionPlayer.session_id == chat_id)
                    )
                    await session.execute(delete(User).where(User.is_guest.is_(True)))
                    session_obj.is_active = False
                    await session.commit()

            except Exception as e:
                logger.error(f"Failed to auto-close poll: {e}")


async def calculate_winner_scores(poll_data, chat_id: int, session, is_weighted: bool):
    """
    Calculate scores for each game option, applying modifiers.

    Returns:
        tuple: (scores_dict, modifiers_summary_string)
            - scores_dict: {game_name: final_score}
            - modifiers_summary_string: Human-readable string of applied modifiers (or empty)

    This function is designed for extensibility. Add new modifiers here.
    """
    scores = {}
    modifiers_info = []

    # Get player IDs for this session
    player_ids_stmt = select(SessionPlayer.user_id).where(SessionPlayer.session_id == chat_id)
    player_ids = [row[0] for row in (await session.execute(player_ids_stmt)).all()]

    for option in poll_data.options:
        text = option.text
        clean_name = text.replace("⭐ ", "")
        base_score = float(option.voter_count)

        modifier_score = 0.0

        # =====================================================================
        # MODIFIER 1: Star Boost (per-user who starred the game)
        # =====================================================================
        if is_weighted and "⭐" in text:
            # Find the game ID for this option
            game = (
                await session.execute(select(Game).where(Game.name == clean_name))
            ).scalar_one_or_none()

            if game:
                # Count how many session players have this game as priority
                priority_count = await session.scalar(
                    select(func.count(Collection.user_id)).where(
                        Collection.game_id == game.id,
                        Collection.user_id.in_(player_ids),
                        Collection.state == GameState.STARRED,
                    )
                )
                if priority_count > 0:
                    modifier_score += STAR_BOOST * priority_count

        # =====================================================================
        # MODIFIER 2: Starvation Boost (placeholder for future)
        # =====================================================================
        # if is_weighted:
        #     loss_count = await get_game_loss_count(game.id, session)
        #     if loss_count > 0:
        #         modifier_score += STARVATION_BOOST * loss_count
        #         modifiers_info.append(
        #             f"Starvation: +{STARVATION_BOOST * loss_count} for {clean_name}"
        #         )

        scores[clean_name] = base_score + modifier_score

    # Build summary string
    if is_weighted:
        modifiers_info.append(f"Weighted votes active: +{STAR_BOOST}/⭐ per user")

    return scores, " | ".join(modifiers_info) if modifiers_info else ""


# ---------------------------------------------------------------------------- #
# Collection Management UI
# ---------------------------------------------------------------------------- #
GAMES_PER_PAGE = 8


async def manage_collection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show user's collection with toggle buttons for availability (sent via DM)."""
    user_id = update.effective_user.id
    user_name = update.effective_user.first_name
    chat_type = update.effective_chat.type

    async with db.AsyncSessionLocal() as session:
        # Fetch user's collection with game details
        stmt = (
            select(Collection, Game)
            .join(Game)
            .where(Collection.user_id == user_id)
            .order_by(Game.name)
        )
        results = (await session.execute(stmt)).all()

    if not results:
        await update.message.reply_text(
            "Your collection is empty!\n\n"
            "Use /setbgg <username> to sync from BGG, or /addgame <name> to add games."
        )
        return

    # Build keyboard with first page
    keyboard, total_pages = _build_manage_keyboard(list(results), page=0)

    collection_message = (
        f"📚 **Your Collection** ({len(results)} games)\n"
        "Tap a game to toggle availability for game nights.\n"
        "✅ = Available | ❌ = Excluded"
    )

    # If in a group chat, send via DM and post playful message in group
    if chat_type in ("group", "supergroup"):
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=collection_message,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown",
            )
            # Playful message in group
            await update.message.reply_text(
                f"🤫 Psst, {user_name}! Your collection is *top secret* stuff.\n"
                "I've slid into your DMs with the details. Check your private chat with me! 📬",
                parse_mode="Markdown",
            )
        except Exception:
            # User hasn't started private chat with bot
            bot_username = (await context.bot.get_me()).username
            await update.message.reply_text(
                f"🙈 Oops {user_name}, I can't DM you yet!\n\n"
                f"Start a private chat with me first: @{bot_username}\n"
                "Then try `/manage` again!",
                parse_mode="Markdown",
            )
    else:
        # Already in private chat, just reply normally
        await update.message.reply_text(
            collection_message,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )


async def manage_collection_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle manage collection button clicks."""
    query = update.callback_query
    await query.answer()

    data = query.data  # "manage:toggle:<game_id>" or "manage:page:<page_num>"
    parts = data.split(":")
    action = parts[1]
    user_id = query.from_user.id

    async with db.AsyncSessionLocal() as session:
        if action == "toggle":
            game_id = int(parts[2])

            # Cycle through states: INCLUDED -> STARRED -> EXCLUDED -> INCLUDED
            col_stmt = select(Collection).where(
                Collection.user_id == user_id, Collection.game_id == game_id
            )
            col = (await session.execute(col_stmt)).scalar_one_or_none()

            if col:
                # 3-state cycle: 0 (included) -> 1 (starred) -> 2 (excluded) -> 0
                col.state = (col.state + 1) % 3
                await session.commit()

        # Fetch updated collection to rebuild keyboard
        stmt = (
            select(Collection, Game)
            .join(Game)
            .where(Collection.user_id == user_id)
            .order_by(Game.name)
        )
        results = (await session.execute(stmt)).all()

    if not results:
        await query.edit_message_text("Your collection is now empty!")
        return

    # Determine current page
    page = 0
    if action == "page":
        page = int(parts[2])
    elif action == "toggle":
        # Stay on same page - figure out which page the toggled game is on
        game_id = int(parts[2])
        for idx, (_col, game) in enumerate(results):
            if game.id == game_id:
                page = idx // GAMES_PER_PAGE
                break

    keyboard, total_pages = _build_manage_keyboard(list(results), page)

    await query.edit_message_text(
        f"📚 **Your Collection** ({len(results)} games)\n"
        "Tap a game to cycle its state:\n"
        "⬜ Included → 🌟 Starred → ❌ Excluded",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


def _build_manage_keyboard(
    results: list, page: int
) -> tuple[list[list[InlineKeyboardButton]], int]:
    """Build keyboard for manage collection view with pagination."""
    total_games = len(results)
    total_pages = (total_games + GAMES_PER_PAGE - 1) // GAMES_PER_PAGE
    page = max(0, min(page, total_pages - 1))  # Clamp page

    start_idx = page * GAMES_PER_PAGE
    end_idx = min(start_idx + GAMES_PER_PAGE, total_games)
    page_items = results[start_idx:end_idx]

    keyboard = []
    for col, game in page_items:
        # State icons: 0=included (⬜), 1=starred (🌟), 2=excluded (❌)
        state_icons = {GameState.INCLUDED: "⬜", GameState.STARRED: "🌟", GameState.EXCLUDED: "❌"}
        status = state_icons.get(col.state, "⬜")
        # Truncate long names
        name = game.name[:25] + "…" if len(game.name) > 26 else game.name
        keyboard.append(
            [InlineKeyboardButton(f"{status} {name}", callback_data=f"manage:toggle:{game.id}")]
        )

    # Navigation row
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("◀️ Prev", callback_data=f"manage:page:{page - 1}"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("Next ▶️", callback_data=f"manage:page:{page + 1}"))
    if nav_row:
        keyboard.append(nav_row)

    # Page indicator
    if total_pages > 1:
        keyboard.append(
            [InlineKeyboardButton(f"Page {page + 1}/{total_pages}", callback_data="manage:noop")]
        )

    return keyboard, total_pages


# ---------------------------------------------------------------------------- #
# Poll Settings and Custom Poll Logic
# ---------------------------------------------------------------------------- #

# Vote limit options cycle: Auto -> 3 -> 5 -> 7 -> 10 -> Unlimited -> Auto
VOTE_LIMIT_OPTIONS = [VoteLimit.AUTO, 3, 5, 7, 10, VoteLimit.UNLIMITED]


def calculate_auto_vote_limit(game_count: int) -> int:
    """Calculate automatic vote limit based on game count using log2 formula."""
    if game_count <= 0:
        return 3
    return max(3, math.ceil(math.log2(game_count)))


def get_vote_limit_display(vote_limit: int, game_count: int = 0) -> str:
    """Get display text for vote limit setting."""
    if vote_limit == VoteLimit.AUTO:
        auto_val = calculate_auto_vote_limit(game_count) if game_count > 0 else "?"
        return f"Auto ({auto_val})"
    elif vote_limit == VoteLimit.UNLIMITED:
        return "Unlimited"
    else:
        return str(vote_limit)


async def poll_settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show poll settings."""
    query = update.callback_query
    await query.answer()

    chat_id = query.message.chat.id
    message_id = query.message.message_id

    async with db.AsyncSessionLocal() as session:
        session_obj = await session.get(Session, chat_id)

        if session_obj and session_obj.message_id and session_obj.message_id != message_id:
            await query.answer("This session is expired.", show_alert=True)
            return

        if not session_obj:
            return

        is_custom = session_obj.poll_type == PollType.CUSTOM
        is_weighted = session_obj.settings_weighted
        hide_voters = session_obj.hide_voters
        vote_limit = session_obj.vote_limit
        mode_text = "Custom (Single)" if is_custom else "Native (Multiple)"
        weight_icon = "✅" if is_weighted else "❌"
        hide_icon = "✅" if hide_voters else "❌"
        limit_text = get_vote_limit_display(vote_limit)

    keyboard = [
        [InlineKeyboardButton(f"Mode: {mode_text}", callback_data="toggle_poll_mode")],
        [InlineKeyboardButton(f"Weights: {weight_icon}", callback_data="toggle_weights")],
        [
            InlineKeyboardButton(
                f"Anonymous Voting: {hide_icon}", callback_data="toggle_hide_voters"
            )
        ],
        [InlineKeyboardButton(f"Vote Limit: {limit_text}", callback_data="cycle_vote_limit")],
        [InlineKeyboardButton("🔙 Back to Lobby", callback_data="resume_night")],
    ]

    await query.edit_message_text(
        "**Poll Settings**\n\n"
        "• **Custom (Single)**: One message with buttons. Good for large lists.\n"
        "• **Native (Multiple)**: Standard Telegram polls. Split if >10 games.\n"
        "• **Weights**: Starred games get +0.5 votes.\n"
        "• **Vote Limit**: Max votes per player (Auto scales with game count).",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


async def toggle_poll_mode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle between Custom and Native poll modes."""
    query = update.callback_query
    await query.answer()

    chat_id = query.message.chat.id
    message_id = query.message.message_id

    async with db.AsyncSessionLocal() as session:
        session_obj = await session.get(Session, chat_id)

        if session_obj:
            if session_obj.message_id and session_obj.message_id != message_id:
                await query.answer("This session is expired.", show_alert=True)
                return

            # Toggle poll type
            if session_obj.poll_type == PollType.CUSTOM:
                session_obj.poll_type = PollType.NATIVE
            else:
                session_obj.poll_type = PollType.CUSTOM
            await session.commit()

            # Refresh settings view
            is_custom = session_obj.poll_type == PollType.CUSTOM
            is_weighted = session_obj.settings_weighted
            hide_voters = session_obj.hide_voters

            mode_text = "Custom (Single)" if is_custom else "Native (Multiple)"
            weight_icon = "✅" if is_weighted else "❌"
            hide_icon = "✅" if hide_voters else "❌"

            keyboard = [
                [InlineKeyboardButton(f"Mode: {mode_text}", callback_data="toggle_poll_mode")],
                [InlineKeyboardButton(f"Weights: {weight_icon}", callback_data="toggle_weights")],
                [
                    InlineKeyboardButton(
                        f"Anonymous Voting: {hide_icon}", callback_data="toggle_hide_voters"
                    )
                ],
                [InlineKeyboardButton("🔙 Back to Lobby", callback_data="resume_night")],
            ]

            await query.edit_message_text(
                "**Poll Settings**\n\n"
                "• **Custom (Single)**: One message with buttons. Good for large lists.\n"
                "• **Native (Multiple)**: Standard Telegram polls. Split if >10 games.\n"
                "• **Weights**: Starred games get +0.5 votes.\n"
                "• **Anonymous**: Hide voter names (show counts only).",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown",
            )


async def toggle_hide_voters_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle anonymous voting setting."""
    query = update.callback_query
    await query.answer()

    chat_id = query.message.chat.id
    message_id = query.message.message_id

    async with db.AsyncSessionLocal() as session:
        session_obj = await session.get(Session, chat_id)

        if session_obj:
            if session_obj.message_id and session_obj.message_id != message_id:
                await query.answer("This session is expired.", show_alert=True)
                return

            session_obj.hide_voters = not session_obj.hide_voters
            await session.commit()

            # Refresh settings view
            is_custom = session_obj.poll_type == PollType.CUSTOM
            is_weighted = session_obj.settings_weighted
            hide_voters = session_obj.hide_voters

            mode_text = "Custom (Single)" if is_custom else "Native (Multiple)"
            weight_icon = "✅" if is_weighted else "❌"
            hide_icon = "✅" if hide_voters else "❌"

            keyboard = [
                [InlineKeyboardButton(f"Mode: {mode_text}", callback_data="toggle_poll_mode")],
                [InlineKeyboardButton(f"Weights: {weight_icon}", callback_data="toggle_weights")],
                [
                    InlineKeyboardButton(
                        f"Anonymous Voting: {hide_icon}", callback_data="toggle_hide_voters"
                    )
                ],
                [InlineKeyboardButton("🔙 Back to Lobby", callback_data="resume_night")],
            ]

            await query.edit_message_text(
                "**Poll Settings**\n\n"
                "• **Custom (Single)**: One message with buttons. Good for large lists.\n"
                "• **Native (Multiple)**: Standard Telegram polls. Split if >10 games.\n"
                "• **Weights**: Starred games get +0.5 votes.\n"
                "• **Anonymous**: Hide voter names (show counts only).",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown",
            )


async def cycle_vote_limit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cycle through vote limit options."""
    query = update.callback_query
    await query.answer()

    chat_id = query.message.chat.id
    message_id = query.message.message_id

    async with db.AsyncSessionLocal() as session:
        session_obj = await session.get(Session, chat_id)

        if session_obj:
            if session_obj.message_id and session_obj.message_id != message_id:
                await query.answer("This session is expired.", show_alert=True)
                return

            # Cycle to next option
            current = session_obj.vote_limit
            try:
                current_idx = VOTE_LIMIT_OPTIONS.index(current)
                next_idx = (current_idx + 1) % len(VOTE_LIMIT_OPTIONS)
            except ValueError:
                next_idx = 0

            session_obj.vote_limit = VOTE_LIMIT_OPTIONS[next_idx]
            await session.commit()

            # Refresh settings view
            is_custom = session_obj.poll_type == PollType.CUSTOM
            is_weighted = session_obj.settings_weighted
            hide_voters = session_obj.hide_voters

            mode_text = "Custom (Single)" if is_custom else "Native (Multiple)"
            weight_icon = "✅" if is_weighted else "❌"
            hide_icon = "✅" if hide_voters else "❌"
            limit_text = get_vote_limit_display(session_obj.vote_limit)

            keyboard = [
                [InlineKeyboardButton(f"Mode: {mode_text}", callback_data="toggle_poll_mode")],
                [InlineKeyboardButton(f"Weights: {weight_icon}", callback_data="toggle_weights")],
                [
                    InlineKeyboardButton(
                        f"Anonymous Voting: {hide_icon}", callback_data="toggle_hide_voters"
                    )
                ],
                [
                    InlineKeyboardButton(
                        f"Vote Limit: {limit_text}", callback_data="cycle_vote_limit"
                    )
                ],
                [InlineKeyboardButton("🔙 Back to Lobby", callback_data="resume_night")],
            ]

            await query.edit_message_text(
                "**Poll Settings**\n\n"
                "• **Custom (Single)**: One message with buttons. Good for large lists.\n"
                "• **Native (Multiple)**: Standard Telegram polls. Split if >10 games.\n"
                "• **Weights**: Starred games get +0.5 votes.\n"
                "• **Vote Limit**: Max votes per player (Auto scales with game count).",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown",
            )


async def create_custom_poll(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    session,
    valid_games: list,
    priority_game_ids: set,
):
    """Create a single interactive message for voting on all valid games."""
    chat_id = update.effective_chat.id if update.message else update.callback_query.message.chat.id

    # Generate unique poll ID
    import time

    poll_id = f"poll_{chat_id}_{int(time.time())}"

    # Send initial placeholder
    message = await context.bot.send_message(
        chat_id=chat_id, text="📊 **Initializing Poll...**", parse_mode="Markdown"
    )

    # Create GameNightPoll entry
    db_poll = GameNightPoll(poll_id=poll_id, chat_id=chat_id, message_id=message.message_id)
    session.add(db_poll)
    await session.commit()

    # Render initial state (0 votes)
    await render_poll_message(
        context.bot, chat_id, message.message_id, session, poll_id, valid_games, priority_game_ids
    )


async def custom_poll_vote_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle votes on custom poll."""
    query = update.callback_query
    # Split data: "vote:<poll_id>:<game_id>"
    parts = query.data.split(":")
    if len(parts) < 3:
        await query.answer("Invalid vote data")
        return

    poll_id = parts[1]
    game_id = int(parts[2])
    user_id = query.from_user.id
    user_name = query.from_user.first_name

    async with db.AsyncSessionLocal() as session:
        # Get poll and session info for vote limit
        game_poll = await session.get(GameNightPoll, poll_id)
        if not game_poll:
            await query.answer("Poll not found")
            return

        chat_id = game_poll.chat_id
        session_obj = await session.get(Session, chat_id)

        # Toggle Vote
        stmt = select(PollVote).where(
            PollVote.poll_id == poll_id, PollVote.user_id == user_id, PollVote.game_id == game_id
        )
        existing_vote = (await session.execute(stmt)).scalar_one_or_none()

        if existing_vote:
            await session.delete(existing_vote)
            msg = "Vote removed"
        else:
            # Check vote limit before adding
            if session_obj:
                vote_limit = session_obj.vote_limit

                # Get user's current vote count for this poll
                user_votes_stmt = select(func.count(PollVote.game_id)).where(
                    PollVote.poll_id == poll_id, PollVote.user_id == user_id
                )
                user_vote_count = await session.scalar(user_votes_stmt) or 0

                # Calculate effective limit
                if vote_limit == VoteLimit.AUTO:
                    valid_games, _ = await get_session_valid_games(session, chat_id)
                    effective_limit = calculate_auto_vote_limit(len(valid_games))
                elif vote_limit == VoteLimit.UNLIMITED:
                    effective_limit = None  # No limit
                else:
                    effective_limit = vote_limit

                # Enforce limit
                if effective_limit is not None and user_vote_count >= effective_limit:
                    await query.answer(
                        f"Vote limit reached ({user_vote_count}/{effective_limit}). "
                        "Remove a vote first!",
                        show_alert=True,
                    )
                    return

            vote = PollVote(poll_id=poll_id, user_id=user_id, game_id=game_id, user_name=user_name)
            session.add(vote)
            msg = "Vote recorded"

        await session.commit()
        await query.answer(msg)

        # Recalculate valid games
        valid_games, priority_ids = await get_session_valid_games(session, chat_id)

        if valid_games:
            await render_poll_message(
                context.bot,
                chat_id,
                game_poll.message_id,
                session,
                poll_id,
                valid_games,
                priority_ids,
            )


async def get_session_valid_games(session, chat_id):
    """Helper to re-fetch valid games for a session."""
    players_stmt = select(SessionPlayer).where(SessionPlayer.session_id == chat_id)
    players = (await session.execute(players_stmt)).scalars().all()
    if not players:
        return [], set()

    player_count = len(players)
    player_ids = [p.user_id for p in players]

    query = (
        select(Game)
        .join(Collection)
        .where(
            Collection.user_id.in_(player_ids),
            Collection.state != GameState.EXCLUDED,
            Game.min_players <= player_count,
            func.coalesce(Collection.effective_max_players, Game.max_players) >= player_count,
        )
        .distinct()
    )
    result = await session.execute(query)
    valid_games = result.scalars().all()

    priority_query = (
        select(Collection.game_id)
        .where(Collection.user_id.in_(player_ids), Collection.state == GameState.STARRED)
        .distinct()
    )
    priority_ids = set((await session.execute(priority_query)).scalars().all())

    return valid_games, priority_ids


async def render_poll_message(bot, chat_id, message_id, session, poll_id, games, priority_ids):
    """Update custom poll message with current vote state."""
    # Fetch all votes for this poll
    votes_stmt = select(PollVote).where(PollVote.poll_id == poll_id)
    all_votes = (await session.execute(votes_stmt)).scalars().all()

    # Aggregate votes - separate game votes from category votes
    vote_counts = {g.id: 0 for g in games}
    voters_by_game = {g.id: [] for g in games}

    # Category votes (game_id is negative = -level)
    category_vote_counts = {}  # level -> count
    category_voters = {}  # level -> [user_names]

    total_votes = 0
    unique_voters = set()

    for v in all_votes:
        if v.game_id is not None and v.game_id < 0:
            # Category vote: game_id = -level
            level = -v.game_id
            category_vote_counts[level] = category_vote_counts.get(level, 0) + 1
            if level not in category_voters:
                category_voters[level] = []
            category_voters[level].append(v.user_name)
            total_votes += 1
            unique_voters.add(v.user_id)
        elif v.game_id in vote_counts:
            vote_counts[v.game_id] += 1
            voters_by_game[v.game_id].append(v.user_name)
            total_votes += 1
            unique_voters.add(v.user_id)

    # Sort games: Most votes -> Starred -> Name
    def sort_key(g):
        votes = vote_counts.get(g.id, 0)
        is_starred = g.id in priority_ids
        return (-int(is_starred), -votes, g.name)

    # Hide Voters Logic
    session_obj = await session.get(Session, chat_id)
    hide_voters = session_obj.hide_voters if session_obj else False

    # Get vote limit info for display
    vote_limit = session_obj.vote_limit if session_obj else VoteLimit.UNLIMITED
    if vote_limit == VoteLimit.AUTO:
        effective_limit = calculate_auto_vote_limit(len(games))
        limit_display = f"🗳️ Limit: Auto ({effective_limit})"
    elif vote_limit == VoteLimit.UNLIMITED:
        limit_display = "🗳️ Limit: Unlimited"
    else:
        limit_display = f"🗳️ Limit: {vote_limit}"

    # Build Text
    text_lines = ["📊 **Poll Active**"]
    text_lines.append(f"👥 {len(unique_voters)} voters • {total_votes} votes • {limit_display}\n")

    leader_found = False

    # Text Generation: Group ALL games
    groups = group_games_by_complexity(games)

    for level in sorted(groups.keys(), reverse=True):
        group = groups[level]
        sorted_group = sorted(group, key=sort_key)

        # Check if any game in this group has votes OR if category has votes
        group_has_game_votes = any(vote_counts[g.id] > 0 for g in sorted_group)
        cat_vote_count = category_vote_counts.get(level, 0)

        # Display category votes first if any
        if cat_vote_count > 0:
            level_display = level if level > 0 else "Unrated"
            if hide_voters:
                voters_text = f"{cat_vote_count} voters"
            else:
                voters_text = ", ".join(category_voters.get(level, []))
            text_lines.append(f"**{cat_vote_count}** - 🎲 Category {level_display}")
            text_lines.append(f"   └ _{voters_text}_")
            leader_found = True

        if group_has_game_votes:
            for g in sorted_group:
                count = vote_counts[g.id]
                if count > 0:
                    star = "⭐ " if g.id in priority_ids else ""

                    if hide_voters:
                        voters_text = f"{len(voters_by_game[g.id])} voters"
                    else:
                        voters_text = ", ".join(voters_by_game[g.id])

                    text_lines.append(f"**{count}** - {star}{g.name}")
                    text_lines.append(f"   └ _{voters_text}_")
                    leader_found = True

    if not leader_found:
        text_lines.append("_No votes yet! Tap buttons below._")

    text = "\n".join(text_lines)

    # Build Keyboard with Complexity Grouping
    keyboard = []

    # Grouped by Complexity (Descending)
    for level in sorted(groups.keys(), reverse=True):
        group = groups[level]
        sorted_group = sorted(group, key=sort_key)

        # Add Separator/Header with Category Vote action
        # Show category vote count on header
        cat_count = category_vote_counts.get(level, 0)
        if level > 0:
            header_text = f"--- {level} ---" if cat_count == 0 else f"--- {level} ({cat_count}) ---"
        else:
            header_text = "--- Unrated ---" if cat_count == 0 else f"--- Unrated ({cat_count}) ---"
        keyboard.append(
            [InlineKeyboardButton(header_text, callback_data=f"poll_random_vote:{poll_id}:{level}")]
        )

        current_row = []
        for g in sorted_group:
            count = vote_counts[g.id]
            # Label: "⭐ Catan (2)"
            label = ""
            if g.id in priority_ids:
                label += "⭐ "
            label += g.name

            if count > 0:
                label += f" ({count})"

            if len(label) > 30:
                label = label[:27] + "..."
            current_row.append(InlineKeyboardButton(label, callback_data=f"vote:{poll_id}:{g.id}"))

            if len(current_row) == 2:
                keyboard.append(current_row)
                current_row = []
        if current_row:
            keyboard.append(current_row)

    # Add Refresh/Close actions
    row_actions = [
        InlineKeyboardButton("🔄 Refresh", callback_data=f"poll_refresh:{poll_id}"),
        InlineKeyboardButton("🛑 Close", callback_data=f"poll_close:{poll_id}"),
    ]
    keyboard.append(row_actions)

    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )
    except Exception as e:
        # Ignore "Message is not modified" errors (common in rapid updates)
        if "Message is not modified" not in str(e):
            logger.warning(f"Failed to update poll message: {e}")


async def custom_poll_action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle Refresh/Close/Toggle actions for custom poll."""
    query = update.callback_query
    data = query.data
    parts = data.split(":")
    action = parts[0]
    poll_id = parts[1]

    if action == "poll_refresh":
        await query.answer("Refreshing...")
        async with db.AsyncSessionLocal() as session:
            game_poll = await session.get(GameNightPoll, poll_id)
            if game_poll:
                chat_id = game_poll.chat_id
                valid_games, priority_ids = await get_session_valid_games(session, chat_id)
                await render_poll_message(
                    context.bot,
                    chat_id,
                    game_poll.message_id,
                    session,
                    poll_id,
                    valid_games,
                    priority_ids,
                )

    elif action == "poll_toggle_voters":
        chat_id = query.message.chat.id
        async with db.AsyncSessionLocal() as session:
            session_obj = await session.get(Session, chat_id)
            if session_obj:
                session_obj.hide_voters = not session_obj.hide_voters
                await session.commit()

                # Refresh UI
                game_poll = await session.get(GameNightPoll, poll_id)
                if game_poll:
                    valid_games, priority_ids = await get_session_valid_games(session, chat_id)
                    await render_poll_message(
                        context.bot,
                        chat_id,
                        game_poll.message_id,
                        session,
                        poll_id,
                        valid_games,
                        priority_ids,
                    )
        await query.answer("Visibility toggled")

    elif action == "poll_random_vote":
        # Format: poll_random_vote:<poll_id>:<level>
        # Category votes are stored with game_id = -level (negative = category marker)
        # Actual game selection happens at poll close time
        if len(parts) < 3:
            await query.answer("Invalid data")
            return

        level = int(parts[2])
        chat_id = query.message.chat.id
        user_id = query.from_user.id
        user_name = query.from_user.first_name

        # Use negative level as category marker
        category_marker = -level

        async with db.AsyncSessionLocal() as session:
            # Get session for vote limit
            session_obj = await session.get(Session, chat_id)

            # Re-fetch games to ensure validity
            valid_games, priority_ids = await get_session_valid_games(session, chat_id)

            # Use ALL valid games for grouping
            groups = group_games_by_complexity(valid_games)

            target_group = groups.get(level, [])
            if not target_group:
                await query.answer("No games in this group!")
                return

            # Check if user already voted on this category (toggle behavior)
            existing_vote = await session.get(PollVote, (poll_id, user_id, category_marker))

            if existing_vote:
                # Toggle off - remove the category vote
                await session.delete(existing_vote)
                await session.commit()

                await render_poll_message(
                    context.bot,
                    chat_id,
                    query.message.message_id,
                    session,
                    poll_id,
                    valid_games,
                    priority_ids,
                )
                await query.answer(f"Category {level} vote removed")
                return

            # Check vote limit before adding
            if session_obj:
                vote_limit = session_obj.vote_limit

                # Get user's current vote count for this poll
                user_votes_stmt = select(func.count(PollVote.game_id)).where(
                    PollVote.poll_id == poll_id, PollVote.user_id == user_id
                )
                user_vote_count = await session.scalar(user_votes_stmt) or 0

                # Calculate effective limit
                if vote_limit == VoteLimit.AUTO:
                    effective_limit = calculate_auto_vote_limit(len(valid_games))
                elif vote_limit == VoteLimit.UNLIMITED:
                    effective_limit = None
                else:
                    effective_limit = vote_limit

                # Enforce limit
                if effective_limit is not None and user_vote_count >= effective_limit:
                    await query.answer(
                        f"Vote limit reached ({user_vote_count}/{effective_limit}). "
                        "Remove a vote first!",
                        show_alert=True,
                    )
                    return

            # Add category vote (game_id = -level)
            vote = PollVote(
                poll_id=poll_id, user_id=user_id, game_id=category_marker, user_name=user_name
            )
            session.add(vote)
            await session.commit()

            # Refresh UI
            await render_poll_message(
                context.bot,
                chat_id,
                query.message.message_id,
                session,
                poll_id,
                valid_games,
                priority_ids,
            )

            await query.answer(f"🎲 Voted on Category {level}!")

    elif action == "poll_close":
        await query.answer("Closing poll...")
        chat_id = query.message.chat.id

        async with db.AsyncSessionLocal() as session:
            game_poll = await session.get(GameNightPoll, poll_id)
            if not game_poll:
                return

            valid_games, priority_ids = await get_session_valid_games(session, chat_id)

            # Fetch votes
            votes_stmt = select(PollVote).where(PollVote.poll_id == poll_id)
            all_votes = (await session.execute(votes_stmt)).scalars().all()

            # Resolve category votes: pick one random game per category
            # Category votes have negative game_id = -level
            groups = group_games_by_complexity(valid_games)
            category_resolutions = {}  # level -> selected game

            for v in all_votes:
                if v.game_id is not None and v.game_id < 0:
                    level = -v.game_id
                    if level not in category_resolutions:
                        # First vote for this category - pick a random game
                        target_group = groups.get(level, [])
                        if target_group:
                            category_resolutions[level] = random.choice(target_group)

            # Create resolved votes list (convert category votes to game votes)
            class ResolvedVote:
                """Wrapper to provide game_id and user_id for resolved votes."""

                def __init__(self, game_id, user_id):
                    self.game_id = game_id
                    self.user_id = user_id

            resolved_votes = []
            for v in all_votes:
                if v.game_id is not None and v.game_id < 0:
                    # Category vote - resolve to selected game
                    level = -v.game_id
                    if level in category_resolutions:
                        resolved_votes.append(
                            ResolvedVote(game_id=category_resolutions[level].id, user_id=v.user_id)
                        )
                else:
                    # Regular game vote - wrap in ResolvedVote
                    resolved_votes.append(ResolvedVote(game_id=v.game_id, user_id=v.user_id))

            # Apply weights if enabled
            session_obj = await session.get(Session, chat_id)
            is_weighted = session_obj.settings_weighted if session_obj else False

            # Build star_collections for weighted voting
            star_collections = None
            if is_weighted:
                star_collections = {}
                for g in valid_games:
                    if g.id in priority_ids:
                        # Find which users starred this game
                        starred_stmt = select(Collection.user_id).where(
                            Collection.game_id == g.id, Collection.state == GameState.STARRED
                        )
                        starred_users = (await session.execute(starred_stmt)).scalars().all()
                        star_collections[g.id] = list(starred_users)

            # Use shared utility for winner calculation with RESOLVED votes
            winners, scores, modifiers_log = calculate_poll_winner(
                valid_games, resolved_votes, priority_ids, is_weighted, star_collections
            )

            # Final update
            text = "🗳️ **Poll Closed!**\n\n"
            if winners:
                if len(winners) == 1:
                    text += f"🏆 The winner is: **{winners[0]}**! 🎉"
                else:
                    text += "It's a tie between:\n" + "\n".join([f"• {w}" for w in winners])

                # Build Top 5 leaderboard from scores
                sorted_scores = sorted(scores.items(), key=lambda x: x[1], reverse=True)
                top_5 = sorted_scores[:5]

                if len(top_5) > 1:
                    text += "\n\n**Top 5:**"
                    for i, (name, score) in enumerate(top_5):
                        medal = ["🥇", "🥈", "🥉", "", ""][i]
                        text += f"\n{medal} {name}: {score:.1f} pts"
            else:
                text += "No votes cast?"

            # Edit message to remove buttons and show result
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=game_poll.message_id, text=text, parse_mode="Markdown"
            )

            # End the game night session
            session_obj = await session.get(Session, chat_id)
            if session_obj:
                await session.execute(
                    delete(SessionPlayer).where(SessionPlayer.session_id == chat_id)
                )
                await session.execute(delete(User).where(User.is_guest.is_(True)))
                session_obj.is_active = False
                await session.commit()
