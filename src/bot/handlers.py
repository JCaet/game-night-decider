import contextlib
import functools
import html
import logging
import math
import random
import textwrap
import time
from collections import namedtuple
from collections.abc import Sequence
from typing import cast

import telegram
from sqlalchemy import delete, exists, func, or_, select
from sqlalchemy import update as sa_update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload
from telegram import (
    CallbackQuery,
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatAction, ChatType
from telegram.ext import ContextTypes

from src.bot.widgets import (
    Ballot,
    WidgetTooLarge,
    answer_widget_query,
    build_ballot,
    build_ballot_state,
    closed_ballot_state,
    edit_widget_data,
    send_ballot,
    widget_query_of,
)
from src.core import db
from src.core.bgg import BGGClient
from src.core.logic import group_games_by_complexity, player_count_blocked
from src.core.models import (
    Collection,
    Expansion,
    Game,
    GameNightPoll,
    GameState,
    PollAddedGame,
    PollBallot,
    PollVote,
    Session,
    SessionPlayer,
    User,
    UserExpansion,
    VoteLimit,
    VoteType,
)
from src.core.poll_service import PollService

# Named tuple for resolved votes (after category resolution)
ResolvedVote = namedtuple("ResolvedVote", ["game_id", "user_id"])

logger = logging.getLogger(__name__)


async def _try_pin_message(bot, chat_id: int, message_id: int) -> bool:
    """Attempt to pin a message. Returns True on success, False if bot lacks permissions."""
    try:
        await bot.pin_chat_message(
            chat_id=chat_id, message_id=message_id, disable_notification=True
        )
        return True
    except telegram.error.BadRequest as e:
        logger.info("Cannot pin message in chat %s: %s", chat_id, e)
        return False
    except telegram.error.Forbidden as e:
        logger.info("No permission to pin in chat %s: %s", chat_id, e)
        return False


async def _try_unpin_message(bot, chat_id: int, message_id: int) -> None:
    """Attempt to unpin a message. Silently ignores permission errors."""
    with contextlib.suppress(telegram.error.BadRequest, telegram.error.Forbidden):
        await bot.unpin_chat_message(chat_id=chat_id, message_id=message_id)


_NameEntry = namedtuple("_NameEntry", ["uid", "first", "last", "tg_username", "fallback"])


def _disambiguate(entries: list) -> dict[int, str]:
    """
    Core disambiguation logic. Accepts a list of _NameEntry namedtuples and returns
    {uid: display_name}, guaranteeing uniqueness within the group.

    Ladder (stops at first level that yields a unique name):
      1. First name only
      2. First name + last initial  ("Alex S.")
      3. First name + full last name ("Alex Smith")
      4. First name + @username      ("Alex (@alexs)")
      5. fallback (bgg_username / "User {id}")
    """
    result: dict[int, str] = {}
    for e in entries:
        if not e.first:
            result[e.uid] = e.fallback
            continue

        conflicts = [o for o in entries if o.first == e.first and o.uid != e.uid]
        if not conflicts:
            result[e.uid] = e.first
            continue

        if e.last:
            candidate = f"{e.first} {e.last[0]}."
            conflict_initials = {f"{e.first} {o.last[0]}." for o in conflicts if o.last}
            if candidate not in conflict_initials:
                result[e.uid] = candidate
                continue

            candidate_full = f"{e.first} {e.last}"
            conflict_full = {f"{e.first} {o.last}" for o in conflicts if o.last}
            if candidate_full not in conflict_full:
                result[e.uid] = candidate_full
                continue

        if e.tg_username:
            result[e.uid] = f"{e.first} (@{e.tg_username})"
            continue

        result[e.uid] = e.fallback

    return result


def disambiguate_names(users: list) -> dict[int, str]:
    """Return disambiguated display names for a list of User objects."""
    entries = [
        _NameEntry(
            uid=u.telegram_id,
            first=u.telegram_name,
            last=u.telegram_last_name,
            tg_username=u.telegram_username,
            fallback=u.bgg_username or f"User {u.telegram_id}",
        )
        for u in users
    ]
    return _disambiguate(entries)


def disambiguate_voter_names(votes: list) -> dict[int, str]:
    """
    Return disambiguated display names derived from PollVote snapshot fields.

    Uses only the data captured at vote time so that a new voter joining later
    never retroactively renames an existing voter in the poll display.
    """
    # Deduplicate: one entry per unique voter (keep first occurrence)
    seen: set[int] = set()
    entries = []
    for v in votes:
        if v.user_id not in seen:
            seen.add(v.user_id)
            entries.append(
                _NameEntry(
                    uid=v.user_id,
                    first=v.user_name,
                    last=v.user_last_name,
                    tg_username=v.user_tg_username,
                    fallback=f"User {v.user_id}",
                )
            )
    return _disambiguate(entries)


# Inline-keyboard button label layout for the "Add a game" picker. Telegram
# clients render '\n' inside button text as a soft line break, so a long game
# name can span multiple visual rows inside a single button rather than being
# truncated to '…'.
_BUTTON_LINE_WIDTH = 18
_BUTTON_MAX_LINES = 3
# Names longer than this trigger a one-button-per-row layout for that group,
# giving the wrapper twice the horizontal room and reducing how often it has
# to wrap or fall back to the ellipsis placeholder.
_BUTTON_LONG_THRESHOLD = 18

# Telegram rejects a message over this many characters with "Message_too_long",
# which fails the whole edit and freezes the poll. The tally grows with every
# voted game, so it is clamped before sending.
_MESSAGE_TEXT_LIMIT = 4096

# Start parameter of the deep link behind a poll's Vote button: /start b_<poll_id>
_BALLOT_START_PREFIX = "b_"

# (name length, with BGG ids) tried in turn when a ballot's HTML is over
# Telegram's size limit. Ids go first: without them a game's link falls back to
# a BGG search for its name, which costs the voter less than a cut-off name.
_BALLOT_FITS = ((None, True), (None, False), (24, False), (16, False))

# Buttons cannot collect text, so the ones that need a name post a prompt and
# the answer comes back as a reply to it (prompt_reply). A prompt carries no
# state of its own: what it is asking for is read back out of its text, so
# these strings are both what the player sees and what the reply is matched on.
_GUEST_PROMPT = "reply to this message with the guest's name."
_ADD_GAME_PROMPT = "reply to this message with a game to add to your collection."
# Followed by the guest's name, which runs to the end of the message.
_GUEST_GAME_PROMPT = "reply to this message with a game for the guest: "


def _build_poll_keyboard(bot, poll_id: str, allow_adding: bool) -> list[list[InlineKeyboardButton]]:
    """The poll message's controls. Voting itself happens on each player's ballot."""
    # No refresh button: joins, leaves, votes and collection changes all
    # re-render the poll by themselves.
    vote_url = f"https://t.me/{bot.username}?start={_BALLOT_START_PREFIX}{poll_id}"
    last_row = [InlineKeyboardButton("🛑 Close", callback_data=f"poll_close:{poll_id}")]
    if allow_adding:
        # Puts a game the player already owns on this poll; "Add my game"
        # above it adds a new game to their collection.
        last_row.insert(
            0, InlineKeyboardButton("➕ Add to poll", callback_data=f"poll_add:{poll_id}")
        )
    return [
        [InlineKeyboardButton("🗳 Vote", url=vote_url)],
        [
            InlineKeyboardButton("👤 Guest", callback_data=f"poll_guest:{poll_id}"),
            InlineKeyboardButton("📚 Add my game", callback_data=f"poll_mygame:{poll_id}"),
        ],
        last_row,
    ]


def _wrap_button_label(prefix: str, name: str, suffix: str) -> str:
    """Build an inline-button label that wraps the name across multiple lines.

    The prefix is glued to the first line and the suffix to the last so that
    the star marker and the vote count stay visible regardless of where the
    name wraps. If the wrapped name still doesn't fit within ``_BUTTON_MAX_LINES``
    lines, the final line ends with an ellipsis placeholder.
    """
    lines = textwrap.wrap(
        name,
        width=_BUTTON_LINE_WIDTH,
        max_lines=_BUTTON_MAX_LINES,
        placeholder="…",
        break_long_words=True,
        break_on_hyphens=False,
    ) or [""]
    lines[0] = f"{prefix}{lines[0]}"
    lines[-1] = f"{lines[-1]}{suffix}"
    return "\n".join(lines)


def _clamp_message_text(lines: list[str], limit: int = _MESSAGE_TEXT_LIMIT) -> str:
    """Join lines into a message guaranteed to fit Telegram's text limit.

    The poll tally grows by two lines per voted game, so a big poll can exceed
    the limit; Telegram then rejects the whole edit with "Message_too_long" and
    the keyboard never updates either. Dropping trailing lines keeps the header
    and the top of the tally, which is the part people read.

    Cuts only on line boundaries so Markdown emphasis inside a line is never
    split across the truncation point.
    """
    text = "\n".join(lines)
    if len(text) <= limit:
        return text

    note = "\n_…tally truncated to fit Telegram's message limit._"
    budget = limit - len(note)
    kept: list[str] = []
    used = 0
    for line in lines:
        cost = len(line) + (1 if kept else 0)
        if used + cost > budget:
            break
        kept.append(line)
        used += cost
    return "\n".join(kept) + note


def _build_poll_description(player_count: int, game_count: int, session_obj) -> str:
    """Build a context description string for poll metadata."""
    parts = [f"{player_count} players", f"{game_count} games"]
    if session_obj.settings_weighted:
        parts.append("weighted voting")
    if session_obj.hide_results:
        parts.append("results hidden")
    if session_obj.allow_adding_options:
        parts.append("suggestions enabled")
    return " · ".join(parts)


def build_player_names(players: Sequence[SessionPlayer]) -> list[str]:
    """Build disambiguated display names for a list of SessionPlayer objects."""
    users = [p.user for p in players]
    name_map = disambiguate_names(users)
    names = []
    for p in players:
        name = name_map.get(p.user_id, p.user.bgg_username or f"User {p.user_id}")
        if p.user.is_guest:
            name += " 👤"
        names.append(name)
    return names


def _refreshes_polls(handler):
    """Re-render running polls after a handler that can change a collection.

    A poll's game list is derived from its players' collections, and each
    ballot has that list baked into its HTML. Without this a change would sit
    unseen until the next vote or manual refresh.
    """

    @functools.wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        result = await handler(update, context)
        try:
            await _refresh_affected_polls(update, context)
        except Exception as e:
            logger.warning(f"Failed to refresh polls after a collection change: {e}")
        return result

    return wrapper


async def _refresh_affected_polls(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Schedule a render for every poll the acting user or chat is part of."""
    conditions = []
    if update.effective_chat is not None:
        # Covers guests, whose games are added on their behalf from the group.
        conditions.append(GameNightPoll.chat_id == update.effective_chat.id)
    if update.effective_user is not None:
        conditions.append(
            GameNightPoll.chat_id.in_(
                select(SessionPlayer.session_id).where(
                    SessionPlayer.user_id == update.effective_user.id
                )
            )
        )
    if not conditions:
        return
    async with db.AsyncSessionLocal() as session:
        polls = (
            (await session.execute(select(GameNightPoll).where(or_(*conditions)))).scalars().all()
        )
        for poll in polls:
            if _schedule_poll_render(context, poll.chat_id, poll.message_id, poll.poll_id):
                continue
            valid_games, priority_ids = await get_session_valid_games(session, poll.chat_id)
            await render_poll_message(
                context.bot,
                poll.chat_id,
                poll.message_id,
                session,
                poll.poll_id,
                valid_games,
                priority_ids,
            )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Welcome message, or a voter's ballot when reached from a poll's Vote button."""
    if context.args and context.args[0].startswith(_BALLOT_START_PREFIX):
        await _open_ballot(update, context, context.args[0][len(_BALLOT_START_PREFIX) :])
        return

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
        "_Add me to a group chat for the best experience!_\n\n"
        "Powered by BoardGameGeek"
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
        "• /guestgame `<name> <game>` - Add game to guest's list\n"
        "• /cancel - Cancel the current game night\n\n"
        "**Poll Settings** (via ⚙️ Settings in lobby):\n"
        "• **Weights**: Starred games get +0.5 vote boost\n"
        "• **Anonymous**: Hide voter names in results\n"
        "• **Vote Limit**: Auto / 3 / 5 / 7 / 10 / Unlimited\n"
        "• **Shuffle**: Randomize option order to reduce bias\n"
        "• **Hide Results**: Hide votes until poll closes\n"
        "• **Allow Suggestions**: Let players add games mid-poll\n\n"
        "**Other:**\n"
        "• /help - Show this message\n"
        "• /start - Show welcome message\n\n"
        "Powered by BoardGameGeek"
    )
    await update.message.reply_text(help_text, parse_mode="Markdown")


@_refreshes_polls
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

    # Acknowledge immediately — DB write + BGG fetch take several seconds on
    # Cloud Run + external Postgres, and the user needs to see we're working.
    with contextlib.suppress(Exception):
        await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    mode_text = " (force update)" if force_update else ""
    status_msg = await update.message.reply_text(
        f"⏳ Syncing BGG collection for {bgg_username}{mode_text}, please wait..."
    )

    async with db.AsyncSessionLocal() as session:
        # Check if user exists
        stmt = select(User).where(User.telegram_id == telegram_id)
        result = await session.execute(stmt)
        user = result.scalar_one_or_none()

        if not user:
            user = User(telegram_id=telegram_id, telegram_name=update.effective_user.first_name)
            session.add(user)

        tg_user = update.effective_user
        user.bgg_username = bgg_username
        user.telegram_name = tg_user.first_name
        user.telegram_last_name = tg_user.last_name
        user.telegram_username = tg_user.username
        await session.commit()

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

            # Pick which games need a /thing fetch. Force sync hits every game so
            # algorithm-driven fields (complexity, community_unplayable_counts) get
            # refreshed even when complexity is already present — that's what makes
            # `/setbgg <user> force` the user-facing recovery path after we ship a
            # parser change.
            games_needing_details = []
            for g in games:
                db_game = await session.get(Game, g.id)
                if not db_game:
                    continue
                if force_update or not db_game.complexity or db_game.complexity <= 0:
                    games_needing_details.append(g.id)

            complexity_updated = 0
            if games_needing_details:
                # Update status message instead of sending new one
                with contextlib.suppress(Exception):
                    await status_msg.edit_text(
                        f"⏳ Syncing BGG collection for {bgg_username}\n"
                        f"• Fetching game details: 0/{len(games_needing_details)}..."
                    )

                import asyncio

                total_details = len(games_needing_details)
                progress_every = 20
                for i, game_id in enumerate(games_needing_details):
                    try:
                        details = await bgg.get_game_details(game_id)
                        if details:
                            game_obj = await session.get(Game, game_id)
                            if game_obj:
                                if details.complexity and details.complexity > 0:
                                    game_obj.complexity = details.complexity
                                    complexity_updated += 1
                                # Persist parsed value so algorithm changes propagate.
                                # None means "no poll on /thing"; store "" so we don't
                                # keep retrying. Matches refresh_community_player_counts.
                                game_obj.community_unplayable_counts = (
                                    details.community_unplayable_counts
                                    if details.community_unplayable_counts is not None
                                    else ""
                                )

                        # Commit every 10 games to avoid long transactions
                        if (i + 1) % 10 == 0:
                            await session.commit()

                        # Throttled progress update so the user knows it's alive
                        if (i + 1) % progress_every == 0 and (i + 1) < total_details:
                            with contextlib.suppress(Exception):
                                await status_msg.edit_text(
                                    f"⏳ Syncing BGG collection for {bgg_username}\n"
                                    f"• Fetching game details: {i + 1}/{total_details}..."
                                )

                        await asyncio.sleep(0.5)  # Rate limit
                    except Exception as e:
                        logger.warning(f"Failed to fetch details for game {game_id}: {e}")
                        continue
                await session.commit()

        # Gather collection stats
        total_games = len(games)
        new_count = len(new_game_ids)
        removed_count = len(removed_game_ids)

        # Phase 2: Fetch and sync expansions
        # fetch_expansions returns None on BGG error and [] on legit-empty success.
        # We only prune/recompute when we know the response is authoritative —
        # otherwise a transient BGG outage would silently wipe a user's
        # expansion-driven player counts. See issue #45.
        expansions_processed = 0
        player_count_updates = 0

        try:
            # Update status for expansion sync
            with contextlib.suppress(Exception):
                await status_msg.edit_text(
                    f"⏳ Syncing BGG collection for {bgg_username}\n• Syncing expansions..."
                )

            expansions_data = await bgg.fetch_expansions(bgg_username)

            if expansions_data is not None:
                import asyncio

                async with db.AsyncSessionLocal() as session:
                    owned_expansion_ids: set[int] = set()

                    for exp_data in expansions_data:
                        try:
                            # Get expansion details (base game link, player count)
                            exp_info = await bgg.get_expansion_info(exp_data["id"])
                            await asyncio.sleep(0.3)  # Rate limit

                            if not exp_info or not exp_info.get("base_game_id"):
                                continue

                            base_game_id = exp_info["base_game_id"]

                            # Skip expansions whose base game is unknown to us
                            base_game = await session.get(Game, base_game_id)
                            if not base_game:
                                continue

                            owned_expansion_ids.add(exp_data["id"])
                            new_max = exp_info.get("new_max_players")

                            # Create or update expansion record. Don't overwrite a
                            # known-good new_max_players with None — BGG occasionally
                            # returns 0/missing maxplayers on a re-fetch.
                            existing_exp = await session.get(Expansion, exp_data["id"])
                            if not existing_exp:
                                session.add(
                                    Expansion(
                                        id=exp_data["id"],
                                        name=exp_info["name"],
                                        base_game_id=base_game_id,
                                        new_max_players=new_max,
                                        complexity_delta=None,  # Future use
                                    )
                                )
                            else:
                                existing_exp.name = exp_info["name"]
                                existing_exp.base_game_id = base_game_id
                                if new_max:
                                    existing_exp.new_max_players = new_max

                            # Link expansion to user (idempotent)
                            user_exp_stmt = select(UserExpansion).where(
                                UserExpansion.user_id == telegram_id,
                                UserExpansion.expansion_id == exp_data["id"],
                            )
                            if not (await session.execute(user_exp_stmt)).scalar_one_or_none():
                                session.add(
                                    UserExpansion(
                                        user_id=telegram_id,
                                        expansion_id=exp_data["id"],
                                    )
                                )

                            expansions_processed += 1

                        except Exception as e:
                            logger.warning(f"Failed to process expansion {exp_data.get('id')}: {e}")
                            continue

                    # Prune UserExpansion rows for expansions the user no longer owns.
                    prune_stmt = delete(UserExpansion).where(UserExpansion.user_id == telegram_id)
                    if owned_expansion_ids:
                        prune_stmt = prune_stmt.where(
                            UserExpansion.expansion_id.notin_(owned_expansion_ids)
                        )
                    await session.execute(prune_stmt)
                    await session.flush()

                    # Recompute effective_max_players for every collection entry
                    # without a manual override, based on currently-owned expansions.
                    recompute_stmt = (
                        select(Collection, Game)
                        .join(Game, Game.id == Collection.game_id)
                        .where(
                            Collection.user_id == telegram_id,
                            Collection.is_manual_player_override.is_(False),
                        )
                    )
                    rows = (await session.execute(recompute_stmt)).all()
                    for col, game in rows:
                        max_for_base_stmt = (
                            select(func.max(Expansion.new_max_players))
                            .join(UserExpansion, UserExpansion.expansion_id == Expansion.id)
                            .where(
                                UserExpansion.user_id == telegram_id,
                                Expansion.base_game_id == col.game_id,
                            )
                        )
                        expansion_max = (
                            await session.execute(max_for_base_stmt)
                        ).scalar_one_or_none()

                        if expansion_max and expansion_max > game.max_players:
                            new_eff = expansion_max
                        else:
                            new_eff = None

                        if col.effective_max_players != new_eff:
                            col.effective_max_players = new_eff
                            if new_eff is not None:
                                player_count_updates += 1

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

        final_message = "\n".join(summary_lines) + "\n\nPowered by BoardGameGeek"

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


async def _delete_orphaned_guests(session) -> None:
    """Delete guests no longer seated in any game night, with their games.

    Call it after clearing a chat's players. Guests belong to one chat, but this
    runs from any chat, so it has to leave alone guests still seated in another
    chat's night: deleting every guest broke session_players' foreign key and
    rolled back the whole cleanup. The guests' own rows go first, since the
    database refuses to drop a user that collection still points at.
    """
    unseated = ~exists().where(SessionPlayer.user_id == User.telegram_id)
    orphans = select(User.telegram_id).where(User.is_guest.is_(True), unseated)
    await session.execute(delete(Collection).where(Collection.user_id.in_(orphans)))
    await session.execute(delete(UserExpansion).where(UserExpansion.user_id.in_(orphans)))
    await session.execute(delete(User).where(User.is_guest.is_(True), unseated))


async def _close_existing_polls(session, context, chat_id, reason="Poll Closed"):
    """Close all active polls for a chat: edit their messages and delete DB records."""
    existing_polls_stmt = select(GameNightPoll).where(GameNightPoll.chat_id == chat_id)
    existing_polls = (await session.execute(existing_polls_stmt)).scalars().all()

    for p in existing_polls:
        with contextlib.suppress(Exception):
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=p.message_id,
                text=f"🛑 **{reason}**",
                parse_mode="Markdown",
                reply_markup=None,
            )
        await _try_unpin_message(context.bot, chat_id, p.message_id)
        # Before the delete: the ballot rows go with the poll.
        await _retire_ballots(context.bot, session, p.poll_id, reason)
        await session.delete(p)


async def start_night(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start a lobby."""
    chat_id = update.effective_chat.id

    with contextlib.suppress(Exception):
        await context.bot.send_chat_action(chat_id, ChatAction.TYPING)

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
                names = build_player_names(players)

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

        # Close any leftover polls from previous session
        await _close_existing_polls(
            session, context, chat_id, reason="Poll Closed (New Game Night)"
        )

        # Clear players
        await session.execute(delete(SessionPlayer).where(SessionPlayer.session_id == chat_id))

        # Clean up orphaned guests from previous session
        await _delete_orphaned_guests(session)

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


async def _prune_stale_votes(session, context, chat_id):
    """Delete votes for games no longer in the valid set and notify the chat."""
    active_poll_stmt = select(GameNightPoll).where(GameNightPoll.chat_id == chat_id)
    active_poll = (await session.execute(active_poll_stmt)).scalars().first()
    if not active_poll:
        return

    # Safety: skip pruning if no players in session (cleanup transition)
    players_stmt = select(SessionPlayer).where(SessionPlayer.session_id == chat_id)
    players = (await session.execute(players_stmt)).scalars().all()
    if not players:
        return

    valid_games, priority_ids = await get_session_valid_games(session, chat_id)
    valid_game_ids = {g.id for g in valid_games}

    # Find stale game votes (voted game no longer in valid set)
    stale_game_votes_stmt = select(PollVote).where(
        PollVote.poll_id == active_poll.poll_id,
        PollVote.vote_type == VoteType.GAME,
        PollVote.game_id.notin_(valid_game_ids)
        if valid_game_ids
        else PollVote.vote_type == VoteType.GAME,
    )
    stale_game_votes = (await session.execute(stale_game_votes_stmt)).scalars().all()

    # Find stale category votes (complexity level has no games left)
    from src.core.logic import group_games_by_complexity

    active_levels = set(group_games_by_complexity(valid_games).keys()) if valid_games else set()
    stale_cat_votes_stmt = select(PollVote).where(
        PollVote.poll_id == active_poll.poll_id,
        PollVote.vote_type == VoteType.CATEGORY,
        PollVote.category_level.notin_(active_levels)
        if active_levels
        else PollVote.vote_type == VoteType.CATEGORY,
    )
    stale_cat_votes = (await session.execute(stale_cat_votes_stmt)).scalars().all()

    total_suspended = len(stale_game_votes) + len(stale_cat_votes)
    if total_suspended > 0:
        with contextlib.suppress(Exception):
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"⚠️ Game list updated ({len(valid_games)} games). "
                    f"{total_suspended} vote(s) suspended "
                    f"(will resume if games become eligible again)."
                ),
            )


async def _auto_refresh_poll(session, context, chat_id):
    """Refresh the poll message if a poll is active."""
    active_poll_stmt = select(GameNightPoll).where(GameNightPoll.chat_id == chat_id)
    active_poll = (await session.execute(active_poll_stmt)).scalars().first()
    if not active_poll:
        return

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
        logger.warning(f"Failed to auto-refresh poll: {e}")


async def join_lobby_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle join button."""
    query = update.callback_query

    user = query.from_user
    chat_id = query.message.chat.id
    message_id = query.message.message_id

    async with db.AsyncSessionLocal() as session:
        # Validate Session Message ID
        session_obj = await session.get(Session, chat_id)
        if session_obj and session_obj.message_id and session_obj.message_id != message_id:
            with contextlib.suppress(telegram.error.BadRequest):
                await query.answer(
                    "This session is expired. Please use the active Game Night message.",
                    show_alert=True,
                )
            return

        # Answer callback query early to avoid "query is too old" errors
        with contextlib.suppress(telegram.error.BadRequest):
            await query.answer()

        stmt = select(User).where(User.telegram_id == user.id)
        result = await session.execute(stmt)
        db_user = result.scalar_one_or_none()

        if not db_user:
            # Create user if not exists
            db_user = User(
                telegram_id=user.id,
                telegram_name=user.first_name,
                telegram_last_name=user.last_name,
                telegram_username=user.username,
            )
            session.add(db_user)
            await session.commit()
        elif (
            db_user.telegram_name != user.first_name
            or db_user.telegram_last_name != user.last_name
            or db_user.telegram_username != user.username
        ):
            # Update name fields if anything changed
            db_user.telegram_name = user.first_name
            db_user.telegram_last_name = user.last_name
            db_user.telegram_username = user.username
            session.add(db_user)
            await session.commit()

        # Check if already joined. Same session as the upsert above: the join
        # is a separate transaction, but it reuses one pooled connection
        # instead of checking out a second one.
        player_stmt = select(SessionPlayer).where(
            SessionPlayer.session_id == chat_id, SessionPlayer.user_id == user.id
        )
        existing = (await session.execute(player_stmt)).scalar_one_or_none()

        joined = existing is None
        if joined:
            player = SessionPlayer(session_id=chat_id, user_id=user.id)
            session.add(player)
            await session.commit()

        # Update message -- get all players
        players_stmt = (
            select(SessionPlayer)
            .where(SessionPlayer.session_id == chat_id)
            .options(selectinload(SessionPlayer.user))
        )
        players = (await session.execute(players_stmt)).scalars().all()

        names = build_player_names(players)

    # Telegram I/O happens with no connection held: sending the notification
    # inside the session pinned a cross-region connection for the round trip.
    if joined:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"👋 **{user.first_name}** joined the game night!",
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

    await query.edit_message_text(
        f"🎲 **Game Night Started!**\n\n**Joined ({len(names)}):**\n"
        + "\n".join([f"- {n}" for n in names]),
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )

    # Prune stale votes and auto-refresh custom poll if active
    async with db.AsyncSessionLocal() as session:
        await _prune_stale_votes(session, context, chat_id)
        await _auto_refresh_poll(session, context, chat_id)


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
            with contextlib.suppress(telegram.error.BadRequest):
                await query.answer("This session is expired.", show_alert=True)
            return

        # Answer callback query early to avoid "query is too old" errors
        with contextlib.suppress(telegram.error.BadRequest):
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
        names = build_player_names(players)

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

    # Prune stale votes and auto-refresh custom poll if active
    async with db.AsyncSessionLocal() as session:
        await _prune_stale_votes(session, context, chat_id)
        await _auto_refresh_poll(session, context, chat_id)


async def create_poll(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start a poll for the lobby."""
    chat_id = update.effective_chat.id

    with contextlib.suppress(Exception):
        await context.bot.send_chat_action(chat_id, ChatAction.TYPING)

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

        # A chat has one poll at a time, however it was started.
        await _close_existing_polls(
            session, context, chat_id, reason="Poll Closed (New poll started)"
        )
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
                # Drop counts the BGG community marks as not playable (e.g. Dune Uprising 5p)
                ~player_count_blocked(Game.community_unplayable_counts, player_count),
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

    async with db.AsyncSessionLocal() as session:
        await create_custom_poll(update, context, session, list(valid_games), priority_game_ids)


@_refreshes_polls
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

    with contextlib.suppress(Exception):
        await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)

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
            g_id = game.id
            g_min = game.min_players
            g_max = game.max_players
            g_comp = game.complexity
            g_time = game.playing_time

        await update.message.reply_text(
            f"✅ Added '{g_name}' to your collection!\n\n"
            f"📊 **Details from BGG:**\n"
            f"• Players: {g_min}-{g_max}\n"
            f"• Complexity: {g_comp:.2f}/5\n"
            f"• Play time: {g_time} min\n\n"
            f"[View on BoardGameGeek](https://boardgamegeek.com/boardgame/{g_id})\n"
            "Powered by BoardGameGeek",
            parse_mode="Markdown",
            disable_web_page_preview=True,
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
            await _delete_orphaned_guests(session)

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

    await _add_guest(update, context, " ".join(context.args))


async def _send_prompt(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user, body: str) -> None:
    """Ask one player for a piece of text, to be given as a reply. `body` is HTML."""
    # Selective force-reply opens the reply box only for the person mentioned.
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"{user.mention_html()}, {body}",
        parse_mode="HTML",
        reply_markup=ForceReply(selective=True),
    )


async def prompt_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Act on a reply to one of the poll buttons' prompts.

    The reply's text becomes the arguments of the command the button stands in
    for, so a button and its command cannot drift apart.
    """
    message = update.message
    prompt = message.reply_to_message if message else None
    if prompt is None or prompt.from_user is None or prompt.from_user.id != context.bot.id:
        return
    asked = prompt.text or ""
    answer = (message.text or "").strip()
    if not answer:
        return
    chat_id = update.effective_chat.id
    user = update.effective_user

    if asked.endswith(_GUEST_PROMPT):
        if await _add_guest(update, context, answer):
            # A guest with no games changes nothing but the head count, so go
            # straight on to asking for one.
            await _send_prompt(context, chat_id, user, _GUEST_GAME_PROMPT + html.escape(answer))
    elif _GUEST_GAME_PROMPT in asked:
        guest_name = asked.split(_GUEST_GAME_PROMPT, 1)[1]
        context.args = [*guest_name.split(), *answer.split()]
        await guest_game(update, context)
        # Guests usually bring more than one; an unanswered prompt costs nothing.
        await _send_prompt(context, chat_id, user, _GUEST_GAME_PROMPT + html.escape(guest_name))
    elif asked.endswith(_ADD_GAME_PROMPT):
        context.args = answer.split()
        await add_game(update, context)
    else:
        return

    # The prompt has done its job; leaving it would invite a second reply.
    with contextlib.suppress(Exception):
        await prompt.delete()


async def _add_guest(update: Update, context: ContextTypes.DEFAULT_TYPE, guest_name: str) -> bool:
    """Create a guest and seat them in the chat's game night. False if there is none."""
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
            return False

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

    # One more player changes which games fit, so a running poll must catch up.
    try:
        await _refresh_affected_polls(update, context)
    except Exception as e:
        logger.warning(f"Failed to refresh polls after adding a guest: {e}")
    return True


@_refreshes_polls
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
    with contextlib.suppress(telegram.error.BadRequest):
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

        names = build_player_names(players)

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
    with contextlib.suppress(telegram.error.BadRequest):
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
            await _close_existing_polls(
                session, context, chat_id, reason="Poll Closed (Game Night Restarted)"
            )

            # Clear players
            await session.execute(delete(SessionPlayer).where(SessionPlayer.session_id == chat_id))

            # Clean up orphaned guests
            await _delete_orphaned_guests(session)

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
    with contextlib.suppress(telegram.error.BadRequest):
        await query.answer()

    chat_id = query.message.chat.id
    message_id = query.message.message_id

    async with db.AsyncSessionLocal() as session:
        session_obj = await session.get(Session, chat_id)
        if session_obj:
            if session_obj.message_id and session_obj.message_id != message_id:
                with contextlib.suppress(telegram.error.BadRequest):
                    await query.answer("This session is expired.", show_alert=True)
                return

            if await _has_active_poll(session, chat_id):
                with contextlib.suppress(telegram.error.BadRequest):
                    await query.answer(POLL_RUNNING_ALERT, show_alert=True)
                return

            session_obj.settings_weighted = not session_obj.settings_weighted
            await session.commit()

            keyboard = _build_settings_keyboard(session_obj)

            await query.edit_message_text(
                POLL_SETTINGS_TEXT,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown",
            )


async def start_poll_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle poll button - create poll from callback."""
    query = update.callback_query
    with contextlib.suppress(telegram.error.BadRequest):
        await query.answer()

    chat_id = query.message.chat.id
    message_id = query.message.message_id

    async with db.AsyncSessionLocal() as session:
        # Validate Session Message ID
        session_obj = await session.get(Session, chat_id)
        if session_obj and session_obj.message_id and session_obj.message_id != message_id:
            with contextlib.suppress(telegram.error.BadRequest):
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
        await _close_existing_polls(
            session, context, chat_id, reason="Poll Closed (New poll started)"
        )
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
                ~player_count_blocked(Game.community_unplayable_counts, player_count),
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

    async with db.AsyncSessionLocal() as session:
        await create_custom_poll(update, context, session, list(valid_games), priority_game_ids)


async def cancel_night_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle cancel button - end the game night."""
    query = update.callback_query
    with contextlib.suppress(telegram.error.BadRequest):
        await query.answer()

    chat_id = query.message.chat.id
    message_id = query.message.message_id

    async with db.AsyncSessionLocal() as session:
        db_session = await session.get(Session, chat_id)

        # Validate Session Message ID
        if db_session and db_session.message_id and db_session.message_id != message_id:
            with contextlib.suppress(telegram.error.BadRequest):
                await query.answer("This session is expired.", show_alert=True)
            return

        if db_session:
            # Close any active polls
            await _close_existing_polls(
                session, context, chat_id, reason="Poll Closed (Game Night Cancelled)"
            )

            # Clear players
            await session.execute(delete(SessionPlayer).where(SessionPlayer.session_id == chat_id))

            # Clean up orphaned guests
            await _delete_orphaned_guests(session)

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

        # Close any active polls
        await _close_existing_polls(
            session, context, chat_id, reason="Poll Closed (Game Night Cancelled)"
        )

        # Clear players
        await session.execute(delete(SessionPlayer).where(SessionPlayer.session_id == chat_id))

        # Clean up orphaned guests
        await _delete_orphaned_guests(session)

        # Mark session as inactive
        db_session.is_active = False
        await session.commit()

    await update.message.reply_text(
        "🎲 **Game Night Cancelled.**\n\nUse /gamenight to start a new one!",
        parse_mode="Markdown",
    )


# ---------------------------------------------------------------------------- #
# Collection Management UI
# ---------------------------------------------------------------------------- #
GAMES_PER_PAGE = 8


MANAGE_HEADER = (
    "📚 **Your Collection** ({count} games)\n"
    "Tap a game to cycle its state:\n"
    "⬜ Included → 🌟 Starred → ❌ Excluded\n"
    "Tap ⚙️ to set custom max players."
)


async def _load_manage_cache(user_id: int) -> list[tuple[Collection, Game]]:
    """Fetch the (Collection, Game) list for a user, ordered by game name."""
    async with db.AsyncSessionLocal() as session:
        stmt = (
            select(Collection, Game)
            .join(Game)
            .where(Collection.user_id == user_id)
            .order_by(Game.name)
        )
        results = (await session.execute(stmt)).all()
    return [(col, game) for col, game in results]


async def _get_manage_cache(
    context: ContextTypes.DEFAULT_TYPE, user_id: int
) -> list[tuple[Collection, Game]]:
    """Return the cached collection, reloading from DB if missing (e.g., cold start)."""
    cache = context.user_data.get("manage_cache")
    if cache is None:
        cache = await _load_manage_cache(user_id)
        context.user_data["manage_cache"] = cache
    return cast(list[tuple[Collection, Game]], cache)


def _find_cache_entry(
    cache: list[tuple[Collection, Game]], game_id: int
) -> tuple[Collection, Game] | None:
    for col, game in cache:
        if game.id == game_id:
            return col, game
    return None


async def manage_collection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show user's collection with toggle buttons for availability (sent via DM)."""
    user_id = update.effective_user.id
    user_name = update.effective_user.first_name
    chat_type = update.effective_chat.type

    with contextlib.suppress(Exception):
        await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)

    cache = await _load_manage_cache(user_id)

    if not cache:
        context.user_data.pop("manage_cache", None)
        await update.message.reply_text(
            "Your collection is empty!\n\n"
            "Use /setbgg <username> to sync from BGG, or /addgame <name> to add games."
        )
        return

    # Clean up previous manage message if one exists
    old_manage_msg_id = context.user_data.pop("manage_message_id", None)
    if old_manage_msg_id:
        with contextlib.suppress(Exception):
            await context.bot.delete_message(
                chat_id=user_id,
                message_id=old_manage_msg_id,
            )

    # Cache the (col, game) pairs so button callbacks can rebuild the keyboard
    # without re-fetching the whole collection on every click.
    # expire_on_commit=False means detached ORM objects are safe to read/mutate.
    context.user_data["manage_cache"] = cache

    keyboard, _total_pages = _build_manage_keyboard(cache, page=0)
    collection_message = MANAGE_HEADER.format(count=len(cache))

    # If in a group chat, send via DM and post playful message in group
    if chat_type in ("group", "supergroup"):
        try:
            msg = await context.bot.send_message(
                chat_id=user_id,
                text=collection_message,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown",
            )
            context.user_data["manage_message_id"] = msg.message_id
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
        msg = await update.message.reply_text(
            collection_message,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )
        context.user_data["manage_message_id"] = msg.message_id


async def _render_detail_view(
    query: CallbackQuery,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    game_id: int,
) -> None:
    """Render the per-game detail view with player count stepper."""
    cache = await _get_manage_cache(context, user_id)
    entry = _find_cache_entry(cache, game_id)
    if not entry:
        await query.edit_message_text("Game not found in your collection.")
        return
    col, game = entry
    keyboard = _build_detail_keyboard(col, game)
    override_text = (
        f"Override: **{col.effective_max_players}** players (manual)"
        if col.is_manual_player_override and col.effective_max_players
        else "No override set"
    )
    await query.edit_message_text(
        f"⚙️ **{game.name}**\n"
        f"Players: {game.min_players}–{game.max_players} "
        f"([BGG default](https://boardgamegeek.com/boardgame/{game.id}))\n"
        f"{override_text}\n\n"
        "Set a custom max player count below:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )


@_refreshes_polls
async def manage_collection_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle manage collection button clicks.

    Uses an in-memory cache (context.user_data["manage_cache"]) so each click
    needs only one single-row UPDATE — no re-fetching the whole collection.
    """
    query = update.callback_query
    with contextlib.suppress(telegram.error.BadRequest):
        await query.answer()

    data = query.data  # "manage:<action>[:<arg>[:<arg>]]"
    parts = data.split(":")
    action = parts[1]
    user_id = query.from_user.id

    # Reject button presses on stale/outdated manage messages
    current_manage_msg = context.user_data.get("manage_message_id")
    if current_manage_msg and current_manage_msg != query.message.message_id:
        with contextlib.suppress(telegram.error.BadRequest):
            await query.answer(
                "This collection view is outdated. Use /manage for the current one.",
                show_alert=True,
            )
        return

    # --- No-op (e.g., page indicator tap) ---
    if action == "noop":
        return

    # --- Close the collection manager ---
    if action == "close":
        context.user_data.pop("manage_message_id", None)
        context.user_data.pop("manage_cache", None)
        with contextlib.suppress(Exception):
            await query.delete_message()
        return

    cache = await _get_manage_cache(context, user_id)

    # --- Set max players override ---
    if action == "setmax":
        game_id = int(parts[2])
        new_max = int(parts[3])
        entry = _find_cache_entry(cache, game_id)
        if entry:
            col, _ = entry
            col.effective_max_players = new_max
            col.is_manual_player_override = True
        async with db.AsyncSessionLocal() as session:
            await session.execute(
                sa_update(Collection)
                .where(Collection.user_id == user_id, Collection.game_id == game_id)
                .values(effective_max_players=new_max, is_manual_player_override=True)
            )
            await session.commit()
        await _render_detail_view(query, context, user_id, game_id)
        return

    # --- Clear max players override ---
    if action == "clearmax":
        game_id = int(parts[2])
        entry = _find_cache_entry(cache, game_id)
        if entry:
            col, _ = entry
            col.effective_max_players = None
            col.is_manual_player_override = False
        async with db.AsyncSessionLocal() as session:
            await session.execute(
                sa_update(Collection)
                .where(Collection.user_id == user_id, Collection.game_id == game_id)
                .values(effective_max_players=None, is_manual_player_override=False)
            )
            await session.commit()
        await _render_detail_view(query, context, user_id, game_id)
        return

    # --- Detail view: show per-game settings ---
    if action == "detail":
        game_id = int(parts[2])
        await _render_detail_view(query, context, user_id, game_id)
        return

    # --- Toggle state in cache + single-row UPDATE ---
    page = 0
    if action == "toggle":
        game_id = int(parts[2])
        entry = _find_cache_entry(cache, game_id)
        if entry:
            col, _ = entry
            # 3-state cycle: 0 (included) -> 1 (starred) -> 2 (excluded) -> 0
            col.state = (col.state + 1) % 3
            new_state = col.state
            async with db.AsyncSessionLocal() as session:
                await session.execute(
                    sa_update(Collection)
                    .where(Collection.user_id == user_id, Collection.game_id == game_id)
                    .values(state=new_state)
                )
                await session.commit()
            # Stay on the page containing the toggled game
            for idx, (_c, g) in enumerate(cache):
                if g.id == game_id:
                    page = idx // GAMES_PER_PAGE
                    break
    elif action == "page":
        page = int(parts[2])

    if not cache:
        await query.edit_message_text("Your collection is now empty!")
        return

    keyboard, _total_pages = _build_manage_keyboard(cache, page)

    await query.edit_message_text(
        MANAGE_HEADER.format(count=len(cache)),
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


def _build_detail_keyboard(col: Collection, game: Game) -> list[list[InlineKeyboardButton]]:
    """Build keyboard for per-game detail view with +/- player count stepper."""
    keyboard: list[list[InlineKeyboardButton]] = []
    current = col.effective_max_players or game.max_players
    gid = game.id

    # +/- stepper row
    row: list[InlineKeyboardButton] = []
    if current > game.min_players:
        row.append(InlineKeyboardButton("➖", callback_data=f"manage:setmax:{gid}:{current - 1}"))
    row.append(InlineKeyboardButton(f"  {current} players  ", callback_data="manage:noop"))
    row.append(InlineKeyboardButton("➕", callback_data=f"manage:setmax:{gid}:{current + 1}"))
    keyboard.append(row)

    # Clear override (only if one exists)
    if col.is_manual_player_override and col.effective_max_players:
        keyboard.append(
            [
                InlineKeyboardButton(
                    "🔄 Reset to BGG default",
                    callback_data=f"manage:clearmax:{game.id}",
                )
            ]
        )

    # Back to collection list
    keyboard.append([InlineKeyboardButton("◀️ Back to collection", callback_data="manage:page:0")])

    return keyboard


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
        # Show manual player override indicator
        override_label = ""
        if col.is_manual_player_override and col.effective_max_players:
            override_label = f" [{col.effective_max_players}p]"
        # Truncate long names (account for override label width)
        max_name_len = 25 - len(override_label)
        name = game.name[:max_name_len] + "…" if len(game.name) > max_name_len + 1 else game.name
        keyboard.append(
            [
                InlineKeyboardButton(
                    f"{status} {name}{override_label}",
                    callback_data=f"manage:toggle:{game.id}",
                ),
                InlineKeyboardButton("⚙️", callback_data=f"manage:detail:{game.id}"),
            ]
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

    # Done button to close the manager
    keyboard.append([InlineKeyboardButton("✅ Done", callback_data="manage:close")])

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


def _build_settings_keyboard(
    session_obj, vote_limit_text: str | None = None
) -> list[list[InlineKeyboardButton]]:
    """
    Build the poll settings inline keyboard.

    Args:
        session_obj: The Session ORM object with poll settings.
        vote_limit_text: Optional pre-computed vote limit display text.

    Returns:
        List of button rows for InlineKeyboardMarkup.
    """
    weight_icon = "✅" if session_obj.settings_weighted else "❌"
    hide_icon = "✅" if session_obj.hide_voters else "❌"
    shuffle_icon = "✅" if session_obj.shuffle_options else "❌"
    hide_results_icon = "✅" if session_obj.hide_results else "❌"
    allow_adding_icon = "✅" if session_obj.allow_adding_options else "❌"
    limit_text = vote_limit_text or get_vote_limit_display(session_obj.vote_limit)

    keyboard: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(f"Weights: {weight_icon}", callback_data="toggle_weights")],
        [
            InlineKeyboardButton(
                f"Anonymous Voting: {hide_icon}", callback_data="toggle_hide_voters"
            )
        ],
        [InlineKeyboardButton(f"Vote Limit: {limit_text}", callback_data="cycle_vote_limit")],
        [InlineKeyboardButton(f"Shuffle Options: {shuffle_icon}", callback_data="toggle_shuffle")],
        [
            InlineKeyboardButton(
                f"Hide Results: {hide_results_icon}", callback_data="toggle_hide_results"
            )
        ],
        [
            InlineKeyboardButton(
                f"Allow Suggestions: {allow_adding_icon}",
                callback_data="toggle_allow_adding",
            )
        ],
        [InlineKeyboardButton("🔙 Back to Lobby", callback_data="resume_night")],
    ]
    return keyboard


POLL_SETTINGS_TEXT = (
    "**Poll Settings**\n\n"
    "• **Weights**: Starred games get +0.5 votes.\n"
    "• **Vote Limit**: Max votes per player (Auto scales with game count).\n"
    "• **Shuffle**: Randomize option order to reduce positional bias.\n"
    "• **Hide Results**: Hide votes until the poll is closed.\n"
    "• **Allow Suggestions**: Let players add games to the poll."
)

POLL_RUNNING_ALERT = "Poll is running — close it first to change settings."


async def _has_active_poll(session, chat_id: int) -> bool:
    """Return True if a GameNightPoll row exists for this chat."""
    stmt = select(GameNightPoll).where(GameNightPoll.chat_id == chat_id)
    return (await session.execute(stmt)).scalars().first() is not None


async def poll_settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show poll settings."""
    query = update.callback_query
    with contextlib.suppress(telegram.error.BadRequest):
        await query.answer()

    chat_id = query.message.chat.id
    message_id = query.message.message_id

    async with db.AsyncSessionLocal() as session:
        session_obj = await session.get(Session, chat_id)

        if session_obj and session_obj.message_id and session_obj.message_id != message_id:
            with contextlib.suppress(telegram.error.BadRequest):
                await query.answer("This session is expired.", show_alert=True)
            return

        if not session_obj:
            return

        if await _has_active_poll(session, chat_id):
            with contextlib.suppress(telegram.error.BadRequest):
                await query.answer(POLL_RUNNING_ALERT, show_alert=True)
            return

        keyboard = _build_settings_keyboard(session_obj)

    await query.edit_message_text(
        POLL_SETTINGS_TEXT,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


async def toggle_hide_voters_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle anonymous voting setting."""
    query = update.callback_query
    with contextlib.suppress(telegram.error.BadRequest):
        await query.answer()

    chat_id = query.message.chat.id
    message_id = query.message.message_id

    async with db.AsyncSessionLocal() as session:
        session_obj = await session.get(Session, chat_id)

        if session_obj:
            if session_obj.message_id and session_obj.message_id != message_id:
                with contextlib.suppress(telegram.error.BadRequest):
                    await query.answer("This session is expired.", show_alert=True)
                return

            if await _has_active_poll(session, chat_id):
                with contextlib.suppress(telegram.error.BadRequest):
                    await query.answer(POLL_RUNNING_ALERT, show_alert=True)
                return

            session_obj.hide_voters = not session_obj.hide_voters
            await session.commit()

            keyboard = _build_settings_keyboard(session_obj)

            await query.edit_message_text(
                POLL_SETTINGS_TEXT,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown",
            )


async def cycle_vote_limit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cycle through vote limit options."""
    query = update.callback_query
    with contextlib.suppress(telegram.error.BadRequest):
        await query.answer()

    chat_id = query.message.chat.id
    message_id = query.message.message_id

    async with db.AsyncSessionLocal() as session:
        session_obj = await session.get(Session, chat_id)

        if session_obj:
            if session_obj.message_id and session_obj.message_id != message_id:
                with contextlib.suppress(telegram.error.BadRequest):
                    await query.answer("This session is expired.", show_alert=True)
                return

            if await _has_active_poll(session, chat_id):
                with contextlib.suppress(telegram.error.BadRequest):
                    await query.answer(POLL_RUNNING_ALERT, show_alert=True)
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

            keyboard = _build_settings_keyboard(session_obj)

            await query.edit_message_text(
                POLL_SETTINGS_TEXT,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown",
            )


async def _toggle_session_bool(update, field_name: str):
    """Generic toggle for boolean session settings."""
    query = update.callback_query
    with contextlib.suppress(telegram.error.BadRequest):
        await query.answer()

    chat_id = query.message.chat.id
    message_id = query.message.message_id

    async with db.AsyncSessionLocal() as session:
        session_obj = await session.get(Session, chat_id)

        if session_obj:
            if session_obj.message_id and session_obj.message_id != message_id:
                with contextlib.suppress(telegram.error.BadRequest):
                    await query.answer("This session is expired.", show_alert=True)
                return

            if await _has_active_poll(session, chat_id):
                with contextlib.suppress(telegram.error.BadRequest):
                    await query.answer(POLL_RUNNING_ALERT, show_alert=True)
                return

            setattr(session_obj, field_name, not getattr(session_obj, field_name))
            await session.commit()

            keyboard = _build_settings_keyboard(session_obj)

            await query.edit_message_text(
                POLL_SETTINGS_TEXT,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown",
            )


async def toggle_shuffle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle shuffle options setting."""
    await _toggle_session_bool(update, "shuffle_options")


async def toggle_hide_results_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle hide results until close setting."""
    await _toggle_session_bool(update, "hide_results")


async def toggle_allow_adding_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle allow adding options setting."""
    await _toggle_session_bool(update, "allow_adding_options")


async def create_custom_poll(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    session,
    valid_games: list,
    priority_game_ids: set,
):
    """Post the poll message: a live tally, with voting on each player's ballot."""
    chat_id = update.effective_chat.id if update.message else update.callback_query.message.chat.id
    chat_title = getattr(update.effective_chat, "title", None)

    # Generate unique poll ID
    import time

    poll_id = f"poll_{chat_id}_{int(time.time())}"

    # Send initial placeholder
    message = await context.bot.send_message(
        chat_id=chat_id, text="📊 **Initializing Poll...**", parse_mode="Markdown"
    )

    # The shuffle seed is fixed for the poll's life, so every ballot — and every
    # replacement of one — lists the games in the same shuffled order.
    db_poll = GameNightPoll(
        poll_id=poll_id,
        chat_id=chat_id,
        message_id=message.message_id,
        shuffle_seed=random.randint(0, 2**31 - 1),
        chat_title=chat_title if isinstance(chat_title, str) else None,
    )
    session.add(db_poll)
    await session.commit()

    # Render initial state (0 votes)
    await render_poll_message(
        context.bot, chat_id, message.message_id, session, poll_id, valid_games, priority_game_ids
    )

    # Pin poll message for visibility
    pinned = await _try_pin_message(context.bot, chat_id, message.message_id)
    if not pinned:
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "💡 *Tip:* Promote this bot to admin with"
                " 'Pin Messages' permission to auto-pin polls."
            ),
            parse_mode="Markdown",
        )


# ---------------------------------------------------------------------------- #
# Ballots
# ---------------------------------------------------------------------------- #
#
# Voting happens on a ballot: a Bot API 10.4 HTML widget in each player's
# private chat with the bot, opened from the poll's Vote button. The poll
# message in the group only shows the tally.
#
# This replaced a vote keyboard on the poll message itself. That message is
# shared, so it could hold no per-user state (paging it moved everyone's page,
# #90 / #92), and Telegram caps an edited keyboard at 100 buttons, which a
# large combined collection overflows. A ballot is per user and pages locally.
# Private chats are the only place production allows widgets today.
#
# A ballot's HTML — and so its game list — is fixed once sent, and it names
# games by position. Two things keep that honest when the list changes: every
# render replaces ballots whose list is stale (_sync_ballots), and every vote
# checks the ballot's catalog_hash before trusting a position.


def _build_poll_ballot(game_poll, games, priority_ids, session_obj) -> Ballot | None:
    """Render a poll's ballot, trimming it step by step if that is what it takes to fit."""
    seed = None
    if session_obj and session_obj.shuffle_options:
        seed = game_poll.shuffle_seed or 0
    title = game_poll.chat_title or "Game Night"
    for name_limit, with_ids in _BALLOT_FITS:
        try:
            return build_ballot(title, games, priority_ids, name_limit, seed, with_ids)
        except WidgetTooLarge as e:
            too_large = e
    logger.warning("Ballot for poll %s does not fit a widget: %s", game_poll.poll_id, too_large)
    return None


def _ballot_state(
    ballot: Ballot, votes, user_id: int, member_ids, session_obj, error: str | None = None
) -> str:
    """One user's ballot state from all of a poll's votes.

    `votes` includes those of players who have left: they are kept for a
    rejoin, shown to their owner, and left out of the tallies.
    """
    vote_counts: dict[int, int] | None = None
    category_counts: dict[int, int] | None = None
    if not (session_obj and session_obj.hide_results):
        vote_counts, category_counts = {}, {}
        for v in votes:
            if v.user_id not in member_ids:
                continue
            if v.vote_type == VoteType.CATEGORY:
                category_counts[v.category_level] = category_counts.get(v.category_level, 0) + 1
            elif v.game_id is not None:
                vote_counts[v.game_id] = vote_counts.get(v.game_id, 0) + 1

    mine = [v for v in votes if v.user_id == user_id]
    vote_limit = session_obj.vote_limit if session_obj else VoteLimit.UNLIMITED
    return build_ballot_state(
        ballot,
        [v.game_id for v in mine if v.vote_type == VoteType.GAME],
        [v.category_level for v in mine if v.vote_type == VoteType.CATEGORY],
        PollService.calculate_effective_limit(vote_limit, len(ballot.game_ids)),
        vote_counts,
        category_counts,
        error=error,
        paused=user_id not in member_ids,
    )


async def _replace_ballot(bot, row: PollBallot, ballot: Ballot, state: str) -> None:
    """Send a fresh ballot and remove the message it supersedes."""
    old_message_id = row.message_id
    row.message_id = await send_ballot(bot, row.user_id, ballot, state, disable_notification=True)
    row.catalog_hash = ballot.catalog_hash
    with contextlib.suppress(Exception):
        await bot.delete_message(chat_id=row.user_id, message_id=old_message_id)


async def _sync_ballots(
    bot, session, game_poll, games, priority_ids, votes, member_ids, session_obj
) -> None:
    """Bring every open ballot of a poll up to date.

    A ballot built from the current game list gets the new tallies pushed to
    it; one built from an older list is replaced.
    """
    rows = (
        (await session.execute(select(PollBallot).where(PollBallot.poll_id == game_poll.poll_id)))
        .scalars()
        .all()
    )
    # No games means the lobby is mid-teardown; leave the ballots as they are.
    if not rows or not games:
        return
    ballot = _build_poll_ballot(game_poll, games, priority_ids, session_obj)
    if ballot is None:
        return

    replaced = False
    for row in rows:
        try:
            state = _ballot_state(ballot, votes, row.user_id, member_ids, session_obj)
            if row.catalog_hash != ballot.catalog_hash:
                await _replace_ballot(bot, row, ballot, state)
                replaced = True
            else:
                await edit_widget_data(bot, row.user_id, row.message_id, state)
        except Exception as e:
            # One unreachable voter (bot blocked, ballot deleted) must not
            # stop the others from being updated.
            logger.info(
                "Could not update the ballot of user %s for poll %s: %s",
                row.user_id,
                game_poll.poll_id,
                e,
            )
    if replaced:
        await session.commit()


async def _retire_ballots(bot, session, poll_id: str, text: str) -> None:
    """Mark every ballot of a poll as closed. Call before the poll row is deleted."""
    rows = (
        (await session.execute(select(PollBallot).where(PollBallot.poll_id == poll_id)))
        .scalars()
        .all()
    )
    state = closed_ballot_state(text)
    for row in rows:
        with contextlib.suppress(Exception):
            await edit_widget_data(bot, row.user_id, row.message_id, state)


async def _open_ballot(update: Update, context: ContextTypes.DEFAULT_TYPE, poll_id: str) -> None:
    """Give the caller their ballot for a poll.

    Tapping Vote again while holding a current ballot must not resend it: the
    old one vanishing and a new one arriving reads as a glitch. It is refreshed
    in place instead, and only replaced when its game list is out of date or
    the voter deleted it.
    """
    user = update.effective_user
    message = update.message
    if update.effective_chat.type != ChatType.PRIVATE:
        await message.reply_text("Tap 🗳 Vote on the poll to open your ballot in our private chat.")
        return

    async with db.AsyncSessionLocal() as session:
        game_poll = await session.get(GameNightPoll, poll_id)
        if game_poll is None:
            await message.reply_text("That poll is closed.")
            return
        chat_id = game_poll.chat_id

        member_ids = await get_active_member_ids(session, chat_id)
        if user.id not in member_ids:
            await message.reply_text(
                "You're not in this game night yet. Tap Join in the group, then 🗳 Vote again."
            )
            return

        valid_games, priority_ids = await get_session_valid_games(session, chat_id)
        games = await _merge_added_games(session, poll_id, valid_games)
        session_obj = await session.get(Session, chat_id)
        ballot = _build_poll_ballot(game_poll, games, priority_ids, session_obj) if games else None
        if ballot is None:
            await message.reply_text("I couldn't build a ballot for this poll's games.")
            return

        votes = await PollService.get_votes_for_poll(session, poll_id)
        state = _ballot_state(ballot, votes, user.id, member_ids, session_obj)
        row = await session.scalar(
            select(PollBallot).where(PollBallot.poll_id == poll_id, PollBallot.user_id == user.id)
        )
        try:
            kept = False
            if row is not None and row.catalog_hash == ballot.catalog_hash:
                # Fails only if the voter deleted the message; then send anew.
                with contextlib.suppress(telegram.error.TelegramError):
                    await edit_widget_data(context.bot, user.id, row.message_id, state)
                    kept = True
            if kept:
                pass
            elif row is None:
                message_id = await send_ballot(context.bot, user.id, ballot, state)
                session.add(
                    PollBallot(
                        poll_id=poll_id,
                        user_id=user.id,
                        message_id=message_id,
                        catalog_hash=ballot.catalog_hash,
                    )
                )
            else:
                await _replace_ballot(context.bot, row, ballot, state)
            await session.commit()
        except telegram.error.TelegramError as e:
            logger.warning(
                "Could not send a ballot for poll %s to user %s: %s", poll_id, user.id, e
            )
            await message.reply_text("I couldn't open your ballot. Update Telegram and try again.")
            return
        except IntegrityError:
            # Two taps raced; the other one's ballot is the one on record.
            await session.rollback()

    # The deep link arrives as a "/start b_..." message from the voter. Removing
    # it leaves the ballot as the last thing in the chat.
    with contextlib.suppress(Exception):
        await message.delete()


def _parse_ballot_request(data: object, ballot: Ballot) -> tuple[VoteType, int] | None:
    """Decode a ballot's request: "v:<position>" for a game, "c:<level>" for a category."""
    kind, _, arg = str(data or "").partition(":")
    try:
        value = int(arg)
    except ValueError:
        return None
    if kind == "v" and 0 <= value < len(ballot.game_ids):
        return VoteType.GAME, ballot.game_ids[value]
    if kind == "c" and value in ballot.levels:
        return VoteType.CATEGORY, value
    return None


async def widget_query_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle a tap on a ballot: toggle the vote and answer with the fresh state."""
    query = widget_query_of(update)
    if query is None:
        return
    started = time.monotonic()
    bot = context.bot
    sender = query.get("from") or {}
    user_id = sender.get("id")
    message_id = query.get("message_id")

    async def answer(state: str) -> None:
        # Telegram allows 15 seconds. A late or failed answer makes the widget
        # undo the tap on screen even though the vote is recorded, so it is
        # logged with how long the handler took: that is the first thing to
        # look at when voters report ticks that do not stick.
        elapsed_ms = (time.monotonic() - started) * 1000
        try:
            await answer_widget_query(bot, query["id"], state)
        except Exception as e:
            logger.warning(
                "Could not answer the widget query of user %s after %.0f ms: %s",
                user_id,
                elapsed_ms,
                e,
            )
        else:
            logger.info("Answered the widget query of user %s in %.0f ms", user_id, elapsed_ms)

    async with db.AsyncSessionLocal() as session:
        row = None
        if user_id is not None and message_id is not None:
            row = await session.scalar(
                select(PollBallot).where(
                    PollBallot.user_id == user_id, PollBallot.message_id == message_id
                )
            )
        game_poll = await session.get(GameNightPoll, row.poll_id) if row else None
        if row is None or game_poll is None:
            # The poll is over and took its ballot rows with it. Stamp the
            # ballot closed so it stops inviting taps.
            state = closed_ballot_state("This poll is closed.")
            await answer(state)
            if user_id is not None and message_id is not None:
                with contextlib.suppress(Exception):
                    await edit_widget_data(bot, user_id, message_id, state)
            return

        chat_id = game_poll.chat_id
        poll_id = game_poll.poll_id
        poll_message_id = game_poll.message_id
        valid_games, priority_ids = await get_session_valid_games(session, chat_id)
        games = await _merge_added_games(session, poll_id, valid_games)
        session_obj = await session.get(Session, chat_id)
        member_ids = await get_active_member_ids(session, chat_id)
        ballot = _build_poll_ballot(game_poll, games, priority_ids, session_obj) if games else None
        if ballot is None:
            await answer(closed_ballot_state("There are no games to vote on right now."))
            return
        votes = await PollService.get_votes_for_poll(session, poll_id)

        if ballot.catalog_hash != row.catalog_hash:
            # This ballot lists an older set of games, so its positions no
            # longer mean what the voter saw. Record nothing; hand over a
            # current ballot instead.
            await answer(closed_ballot_state("The game list changed. Use the new ballot below."))
            try:
                state = _ballot_state(ballot, votes, user_id, member_ids, session_obj)
                await _replace_ballot(bot, row, ballot, state)
                await session.commit()
            except Exception as e:
                logger.warning("Could not replace a stale ballot for poll %s: %s", poll_id, e)
            return

        request = _parse_ballot_request(query.get("data"), ballot)
        if request is None:
            await answer(
                _ballot_state(ballot, votes, user_id, member_ids, session_obj, "Invalid vote.")
            )
            return
        if user_id not in member_ids:
            # Left the lobby since opening the ballot: the state says so.
            await answer(_ballot_state(ballot, votes, user_id, member_ids, session_obj))
            return

        vote_type, target_id = request
        result = await PollService.cast_vote(
            session=session,
            poll_id=poll_id,
            user_id=user_id,
            target_id=target_id,
            vote_type=vote_type,
            user_name=sender.get("first_name"),
            vote_limit=session_obj.vote_limit if session_obj else VoteLimit.UNLIMITED,
            game_count=len(games),
            user_last_name=sender.get("last_name"),
            user_tg_username=sender.get("username"),
            valid_game_ids=set(ballot.game_ids),
            valid_category_levels=set(ballot.levels),
        )

        # cast_vote commits internally; re-read so the answer reflects it.
        votes = await PollService.get_votes_for_poll(session, poll_id)
        await answer(
            _ballot_state(
                ballot,
                votes,
                user_id,
                member_ids,
                session_obj,
                None if result.success else result.message,
            )
        )

        if result.success:
            # An answer reaches only the widget that asked and is not stored.
            # Storing the state now, rather than leaving it to the debounced
            # render, means a widget whose answer went missing still finds the
            # vote when it next reads its data.
            with contextlib.suppress(Exception):
                await edit_widget_data(
                    bot,
                    user_id,
                    message_id,
                    _ballot_state(ballot, votes, user_id, member_ids, session_obj),
                )

        # The render updates the group tally and pushes the new state to every
        # ballot. Debounced, so a burst of votes collapses into one round of edits.
        if result.success and not _schedule_poll_render(context, chat_id, poll_message_id, poll_id):
            await render_poll_message(
                bot,
                chat_id,
                poll_message_id,
                session,
                poll_id,
                games,
                priority_ids,
                already_merged=True,
            )


async def get_active_member_ids(session, chat_id) -> set[int]:
    """Return user_ids of players currently in the lobby.

    Used to suspend votes cast by users who have since left the session: the
    rows stay in the DB so they auto-resume if the user rejoins, but they are
    excluded from tallies and winner calculation while the voter is absent.
    """
    stmt = select(SessionPlayer.user_id).where(SessionPlayer.session_id == chat_id)
    return set((await session.execute(stmt)).scalars().all())


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
            ~player_count_blocked(Game.community_unplayable_counts, player_count),
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


async def _merge_added_games(session, poll_id: str, games: list) -> list:
    """Merge user-added games (PollAddedGame) into the games list."""
    added_stmt = select(PollAddedGame).where(PollAddedGame.poll_id == poll_id)
    added_records = (await session.execute(added_stmt)).scalars().all()
    existing_ids = {g.id for g in games}
    merged = list(games)
    for rec in added_records:
        if rec.game.id not in existing_ids:
            merged.append(rec.game)
            existing_ids.add(rec.game.id)
    return merged


async def render_poll_message(
    bot, chat_id, message_id, session, poll_id, games, priority_ids, already_merged=False
):
    """Update the poll message, and every open ballot, with current vote state.

    Set already_merged=True when the caller has already folded user-added games
    into `games` (via _merge_added_games), to skip a redundant query.
    """
    # Include user-added games (from allow_adding_options)
    if not already_merged:
        games = await _merge_added_games(session, poll_id, games)

    # Fetch all votes for this poll, then drop votes from users who have left
    # the lobby. Rows stay in the DB (so a rejoin auto-resumes them) but are
    # excluded from every tally below.
    votes_stmt = select(PollVote).where(PollVote.poll_id == poll_id)
    raw_votes = (await session.execute(votes_stmt)).scalars().all()
    active_member_ids = await get_active_member_ids(session, chat_id)
    all_votes = [v for v in raw_votes if v.user_id in active_member_ids]

    # Aggregate votes - separate game votes from category votes
    vote_counts = {g.id: 0 for g in games}
    voters_by_game = {g.id: [] for g in games}

    # Category votes (game_id is negative = -level)
    category_vote_counts = {}  # level -> count
    category_voters = {}  # level -> [user_names]

    total_votes = 0
    unique_voters = set()

    # Build disambiguated names from vote snapshots — isolates existing voters
    # from lobby changes (a new joiner can't rename someone who already voted)
    voter_name_map = disambiguate_voter_names(all_votes)

    # Pre-compute active complexity levels so suspended category votes are excluded
    # from totals (mirrors the same guard used for game votes via `vote_counts` keys).
    active_levels = set(group_games_by_complexity(games).keys()) if games else set()

    for v in all_votes:
        voter_display = voter_name_map.get(v.user_id, v.user_name or f"User {v.user_id}")
        if v.vote_type == VoteType.CATEGORY:
            level = v.category_level
            if level not in active_levels:
                continue  # suspended — complexity level has no valid games right now
            category_vote_counts[level] = category_vote_counts.get(level, 0) + 1
            if level not in category_voters:
                category_voters[level] = []
            category_voters[level].append(voter_display)
            total_votes += 1
            unique_voters.add(v.user_id)
        elif v.vote_type == VoteType.GAME:
            game_id = v.game_id
            if game_id in vote_counts:
                vote_counts[game_id] += 1
                voters_by_game[game_id].append(voter_display)
                total_votes += 1
                unique_voters.add(v.user_id)

    # Session settings
    session_obj = await session.get(Session, chat_id)
    hide_voters = session_obj.hide_voters if session_obj else False
    hide_results = session_obj.hide_results if session_obj else False
    allow_adding = session_obj.allow_adding_options if session_obj else False

    game_poll = await session.get(GameNightPoll, poll_id)

    # Get vote limit info for display
    vote_limit = session_obj.vote_limit if session_obj else VoteLimit.UNLIMITED
    if vote_limit == VoteLimit.AUTO:
        effective_limit = calculate_auto_vote_limit(len(games))
        limit_display = f"🗳️ Limit: Auto ({effective_limit})"
    elif vote_limit == VoteLimit.UNLIMITED:
        limit_display = "🗳️ Limit: Unlimited"
    else:
        limit_display = f"🗳️ Limit: {vote_limit}"

    # Build context description
    players_stmt = select(func.count(SessionPlayer.user_id)).where(
        SessionPlayer.session_id == chat_id
    )
    player_count = await session.scalar(players_stmt) or 0
    description = _build_poll_description(player_count, len(games), session_obj)

    # Build Text
    text_lines = ["📊 **Poll Active**"]
    text_lines.append(f"_{description}_")
    text_lines.append(f"👥 {len(unique_voters)} voters • {total_votes} votes • {limit_display}\n")

    leader_found = False

    if hide_results:
        # When results are hidden, only show a generic status
        text_lines.append("_Results will be revealed when the poll closes._")
        leader_found = True
    else:
        # One list, most-voted first, so the leaders read off the top. Each
        # entry is (votes, name to break ties by, label, voters).
        entries: list[tuple[int, str, str, list[str]]] = []
        for level, count in category_vote_counts.items():
            what = f"complexity {level}" if level > 0 else "unrated"
            entries.append(
                (count, f"~{level}", f"🎲 Random {what} game", category_voters.get(level, []))
            )
        for g in games:
            if vote_counts[g.id] > 0:
                star = "⭐ " if g.id in priority_ids else ""
                entries.append((vote_counts[g.id], g.name, f"{star}{g.name}", voters_by_game[g.id]))

        for count, _, label, voters in sorted(entries, key=lambda e: (-e[0], e[1])):
            voters_text = f"{count} voters" if hide_voters else ", ".join(voters)
            text_lines.append(f"**{count}** - {label}")
            text_lines.append(f"   └ _{voters_text}_")
            leader_found = True

    if not leader_found:
        text_lines.append("_No votes yet! Tap 🗳 Vote to open your ballot._")

    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=_clamp_message_text(text_lines),
            reply_markup=InlineKeyboardMarkup(_build_poll_keyboard(bot, poll_id, allow_adding)),
            parse_mode="Markdown",
        )
    except Exception as e:
        # "Message is not modified" is routine: a refresh with nothing new.
        if "Message is not modified" not in str(e):
            logger.warning(f"Failed to update poll message: {e}")

    if game_poll is not None:
        await _sync_ballots(
            bot, session, game_poll, games, priority_ids, raw_votes, active_member_ids, session_obj
        )


# Debounce window for coalescing vote-driven re-renders. A burst of votes within
# this window collapses into a single edit_message_text call, which avoids
# Telegram's per-message edit rate limit (the cause of the laggy top-of-poll
# tally) and cuts redundant DB renders.
_POLL_RENDER_DEBOUNCE_SECONDS = 0.5


async def _render_poll_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Job-queue callback that renders a poll from fresh state.

    Runs after the debounce window so it reflects every vote cast during the
    burst, not just the one that scheduled it.
    """
    data = cast(dict, context.job.data)
    poll_id = data["poll_id"]
    chat_id = data["chat_id"]
    message_id = data["message_id"]
    async with db.AsyncSessionLocal() as session:
        # The poll may have been closed during the debounce window — closing
        # deletes the record and rewrites the message to show results. Skip the
        # render so we don't clobber the results message with a stale tally.
        if await session.get(GameNightPoll, poll_id) is None:
            return
        valid_games, priority_ids = await get_session_valid_games(session, chat_id)
        await render_poll_message(
            context.bot, chat_id, message_id, session, poll_id, valid_games, priority_ids
        )


def _schedule_poll_render(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int, poll_id: str
) -> bool:
    """Throttle a poll re-render: ensure exactly one render is pending for this
    poll, firing _POLL_RENDER_DEBOUNCE_SECONDS after the *first* vote of a burst.
    Returns False if no job queue is available (caller should then render inline
    as a fallback).

    This is a leading-window throttle, NOT a trailing-edge debounce: if a render
    is already pending we leave it in place rather than cancelling and pushing it
    out. A pure debounce starves under sustained voting — every vote within the
    window reschedules the job, so it never fires and the tally freezes. The job
    reads fresh state when it runs, so one render per window reflects every vote
    cast in that window; a vote arriving after the job fires schedules the next
    one, guaranteeing a trailing render once the burst ends."""
    job_queue = context.job_queue
    if job_queue is None:
        return False
    name = f"render:{poll_id}"
    if job_queue.get_jobs_by_name(name):
        # A render is already pending for this poll; it will pick up this vote.
        return True
    job_queue.run_once(
        _render_poll_job,
        when=_POLL_RENDER_DEBOUNCE_SECONDS,
        name=name,
        data={"chat_id": chat_id, "message_id": message_id, "poll_id": poll_id},
    )
    return True


def _cancel_poll_render(context: ContextTypes.DEFAULT_TYPE, poll_id: str) -> None:
    """Cancel any pending debounced render for a poll (e.g. on close)."""
    job_queue = context.job_queue
    if job_queue is None:
        return
    for job in job_queue.get_jobs_by_name(f"render:{poll_id}"):
        job.schedule_removal()


# ---------------------------------------------------------------------------- #
# Poll Action Handlers (extracted from custom_poll_action_callback)
# ---------------------------------------------------------------------------- #

# DESIGN NOTE: Category Voting Convention
# ----------------------------------------
# Category votes use a negative game_id as a semantic marker:
#   game_id = -level (e.g., -4 means "vote for complexity level 4 category")
# At poll close time, category votes are resolved to a random game within
# that complexity group. This allows voting on groups of games without
# pre-selecting which specific game will be played.


async def _handle_poll_guest(query, context: ContextTypes.DEFAULT_TYPE, poll_id: str) -> None:
    """Ask who the guest is. The name comes back as a reply: see prompt_reply."""
    with contextlib.suppress(telegram.error.BadRequest):
        await query.answer()
    await _send_prompt(context, query.message.chat.id, query.from_user, _GUEST_PROMPT)


async def _handle_poll_my_game(query, context: ContextTypes.DEFAULT_TYPE, poll_id: str) -> None:
    """Ask which game to add to the tapper's own collection, as /addgame would."""
    with contextlib.suppress(telegram.error.BadRequest):
        await query.answer()
    await _send_prompt(context, query.message.chat.id, query.from_user, _ADD_GAME_PROMPT)


async def _handle_poll_close(query, context: ContextTypes.DEFAULT_TYPE, poll_id: str) -> None:
    """Close the poll, resolve category votes, calculate winner, and end session."""
    await query.answer("Closing poll...")
    chat_id = query.message.chat.id

    # Drop any pending debounced render so it can't fire after close and
    # overwrite the results message with a stale tally.
    _cancel_poll_render(context, poll_id)

    async with db.AsyncSessionLocal() as session:
        game_poll = await session.get(GameNightPoll, poll_id)
        if not game_poll:
            return

        valid_games, priority_ids = await get_session_valid_games(session, chat_id)
        valid_games = await _merge_added_games(session, poll_id, valid_games)

        # Use PollService for centralized winner calculation
        winners, scores, modifiers_log = await PollService.close_poll(
            session, poll_id, chat_id, valid_games, priority_ids
        )

        # Build result message
        text = "🗳️ **Poll Closed!**\n\n"
        if winners:
            if len(winners) == 1:
                text += f"🏆 The winner is: **{winners[0]}**! 🎉"
            else:
                text += "It's a tie between:\n" + "\n".join([f"• {w}" for w in winners])

            # Build Top 5 leaderboard from scores. Only include games that
            # actually scored — otherwise an under-voted poll pads the board
            # with 0-pt entries (issue #54).
            voted_scores = sorted(
                ((n, s) for n, s in scores.items() if s > 0),
                key=lambda x: x[1],
                reverse=True,
            )
            top_5 = voted_scores[:5]

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

        # Unpin the poll message
        await _try_unpin_message(context.bot, chat_id, game_poll.message_id)

        closing = f"Poll closed. Winner: {', '.join(winners)}" if winners else "Poll closed."
        await _retire_ballots(context.bot, session, poll_id, closing)

        # Delete the poll record (cascade deletes all PollVote rows)
        # The result message above remains visible in chat.
        await session.delete(game_poll)

        # End the game night session
        session_obj = await session.get(Session, chat_id)
        if session_obj:
            await session.execute(delete(SessionPlayer).where(SessionPlayer.session_id == chat_id))
            await _delete_orphaned_guests(session)
            session_obj.is_active = False
            await session.commit()


async def _handle_poll_add(query, context: ContextTypes.DEFAULT_TYPE, poll_id: str) -> None:
    """Show a picker of games the user can add to the poll."""
    chat_id = query.message.chat.id
    user_id = query.from_user.id

    async with db.AsyncSessionLocal() as session:
        game_poll = await session.get(GameNightPoll, poll_id)
        if not game_poll:
            with contextlib.suppress(telegram.error.BadRequest):
                await query.answer("Poll not found")
            return

        # Guard: verify setting is enabled
        session_obj = await session.get(Session, chat_id)
        if not session_obj or not session_obj.allow_adding_options:
            with contextlib.suppress(telegram.error.BadRequest):
                await query.answer("Adding games is not enabled for this poll.", show_alert=True)
            return

        # Get current valid games + already-added games
        valid_games, _ = await get_session_valid_games(session, chat_id)
        valid_ids = {g.id for g in valid_games}

        added_stmt = select(PollAddedGame.game_id).where(PollAddedGame.poll_id == poll_id)
        already_added_ids = set((await session.execute(added_stmt)).scalars().all())
        valid_ids |= already_added_ids

        # Get user's collection games NOT already in the poll
        candidate_query = (
            select(Game)
            .join(Collection)
            .where(
                Collection.user_id == user_id,
                Collection.state != GameState.EXCLUDED,
                Game.id.notin_(valid_ids) if valid_ids else Game.id.isnot(None),
            )
            .distinct()
            .order_by(Game.name)
            .limit(24)
        )
        candidates = (await session.execute(candidate_query)).scalars().all()

    if not candidates:
        with contextlib.suppress(telegram.error.BadRequest):
            await query.answer("No additional games to add!", show_alert=True)
        return

    # Build picker keyboard: wrap long names across multiple lines and drop
    # to one-per-row when needed.
    keyboard = []
    columns = 1 if any(len(g.name) > _BUTTON_LONG_THRESHOLD for g in candidates) else 2
    current_row = []
    for g in candidates:
        label = _wrap_button_label("", g.name, "")
        current_row.append(
            InlineKeyboardButton(label, callback_data=f"poll_add_select:{poll_id}:{g.id}")
        )
        if len(current_row) == columns:
            keyboard.append(current_row)
            current_row = []
    if current_row:
        keyboard.append(current_row)
    keyboard.append([InlineKeyboardButton("🔙 Cancel", callback_data=f"poll_add_cancel:{poll_id}")])

    with contextlib.suppress(telegram.error.BadRequest):
        await query.answer()

    await context.bot.send_message(
        chat_id=chat_id,
        text="**Add a game to the poll:**\n_Select from your collection._",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


async def poll_add_select_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle selecting a game to add to the poll."""
    query = update.callback_query
    parts = query.data.split(":")
    if len(parts) < 2:
        with contextlib.suppress(telegram.error.BadRequest):
            await query.answer("Invalid data")
        return

    action = parts[0]
    poll_id = parts[1]

    if action == "poll_add_cancel":
        # Just delete the picker message
        with contextlib.suppress(telegram.error.BadRequest):
            await query.answer()
            await query.message.delete()  # type: ignore[attr-defined]
        return

    if len(parts) < 3:
        with contextlib.suppress(telegram.error.BadRequest):
            await query.answer("Invalid data")
        return

    game_id = int(parts[2])
    user_id = query.from_user.id

    async with db.AsyncSessionLocal() as session:
        game_poll = await session.get(GameNightPoll, poll_id)
        if not game_poll:
            with contextlib.suppress(telegram.error.BadRequest):
                await query.answer("Poll not found")
            return

        chat_id = game_poll.chat_id

        # Check if already added
        existing = (
            await session.execute(
                select(PollAddedGame).where(
                    PollAddedGame.poll_id == poll_id, PollAddedGame.game_id == game_id
                )
            )
        ).scalar_one_or_none()

        if existing:
            with contextlib.suppress(telegram.error.BadRequest):
                await query.answer("Already added!")
            return

        # Add the game
        added = PollAddedGame(poll_id=poll_id, game_id=game_id, added_by_user_id=user_id)
        session.add(added)
        await session.commit()

        # Get game name for feedback
        game = await session.get(Game, game_id)
        game_name = game.name if game else f"Game {game_id}"

        with contextlib.suppress(telegram.error.BadRequest):
            await query.answer(f"Added {game_name}!")

        # Delete the picker message
        with contextlib.suppress(telegram.error.BadRequest):
            await query.message.delete()  # type: ignore[attr-defined]

        # Refresh the poll
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


async def custom_poll_action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Dispatcher for the poll message's buttons: Guest, Add my game, Add to poll, Close.

    Callback data formats:
    - poll_guest:<poll_id>
    - poll_mygame:<poll_id>
    - poll_close:<poll_id>
    - poll_add:<poll_id>
    """
    query = update.callback_query
    data = query.data or ""
    parts = data.split(":")
    action = parts[0]

    if len(parts) < 2:
        with contextlib.suppress(telegram.error.BadRequest):
            await query.answer("Invalid data")
        return
    poll_id = parts[1]

    if action == "poll_guest":
        await _handle_poll_guest(query, context, poll_id)

    elif action == "poll_mygame":
        await _handle_poll_my_game(query, context, poll_id)

    elif action == "poll_close":
        await _handle_poll_close(query, context, poll_id)

    elif action == "poll_add":
        await _handle_poll_add(query, context, poll_id)
