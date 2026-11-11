"""Bot API 10.4 HTML widgets: a raw-call shim and the poll ballot builder.

python-telegram-bot 22.x stops at Bot API 10.0, so widgets are reached the way
this bot already reaches other newer features: raw requests out, and
``Update.api_kwargs`` in. Everything that knows the wire format lives here, so
it is one module to delete once PTB ships Bot API 10.4.

Limits are from https://core.telegram.org/bots/widgets. A widget's HTML cannot
be edited after sending -- only its data -- so the game list is baked into the
HTML and everything that changes (votes, tallies, closing) travels as state.
"""

import hashlib
import json
import random
import re
import time
import zlib
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

from telegram import Bot, Update
from telegram.ext import BaseHandler

from src.core.logic import group_games_by_complexity
from src.core.models import Game

WIDGET_HTML_MAX_BYTES = 32768
# Measured the way Telegram says it measures: zlib at the maximum level.
WIDGET_HTML_MAX_ZLIB_BYTES = 4096
# chat_data, public_data and the answer to a query share this ceiling.
WIDGET_DATA_MAX_BYTES = 1024
# Telegram clips anything taller; nothing scrolls inside a widget.
WIDGET_MAX_HEIGHT = 600

# Update types are opt-in for anything newer than the library knows about.
WIDGET_QUERY_UPDATE = "widget_query"

_BALLOT_TEMPLATE = Path(__file__).parent / "widget" / "ballot.html"
_CATALOG_SLOT = '/*CATALOG*/{n:"Preview",g:[]}'


class WidgetTooLarge(ValueError):
    """The widget HTML or its data is over one of Telegram's size limits."""


# ---------------------------------------------------------------------------- #
# Raw Bot API calls
# ---------------------------------------------------------------------------- #


async def send_widget(
    bot: Bot,
    chat_id: int,
    html: str,
    *,
    chat_data: str | None = None,
    caption: str | None = None,
    height: int | None = None,
    disable_notification: bool = False,
) -> dict[str, Any]:
    """Send an HTML widget that may query the bot. Returns the raw Message."""
    api_kwargs: dict[str, Any] = {"chat_id": chat_id, "html": html, "allow_queries": True}
    if chat_data is not None:
        api_kwargs["chat_data"] = chat_data
    if caption is not None:
        api_kwargs["caption"] = caption
    if height is not None:
        api_kwargs["height"] = height
    if disable_notification:
        api_kwargs["disable_notification"] = True
    result: dict[str, Any] = await bot.do_api_request("sendWidget", api_kwargs=api_kwargs)
    return result


async def edit_widget_data(bot: Bot, chat_id: int, message_id: int, chat_data: str) -> None:
    """Replace a widget message's chat_data; clients are pushed the change."""
    await bot.do_api_request(
        "editMessageWidgetData",
        api_kwargs={"chat_id": chat_id, "message_id": message_id, "chat_data": chat_data},
    )


async def answer_widget_query(bot: Bot, widget_query_id: str, data: str | None = None) -> None:
    """Answer a widget query. Telegram allows 15 seconds.

    The answer reaches only the widget that asked and is not stored, so state
    that should survive a reopen must also go through edit_widget_data.
    """
    api_kwargs: dict[str, Any] = {"widget_query_id": widget_query_id}
    if data is not None:
        api_kwargs["data"] = data
    await bot.do_api_request("answerWidgetQuery", api_kwargs=api_kwargs)


def widget_query_of(update: object) -> dict[str, Any] | None:
    """The raw WidgetQuery carried by an update, if it has one."""
    if not isinstance(update, Update):
        return None
    query = update.api_kwargs.get(WIDGET_QUERY_UPDATE)
    return query if isinstance(query, dict) else None


class WidgetQueryHandler(BaseHandler):
    """Routes updates that carry a ``widget_query``.

    PTB has no field for it, so the payload sits in ``Update.api_kwargs``; the
    callback reads it with widget_query_of. Matching on the payload (rather than
    a TypeHandler on Update) keeps this from swallowing every other update.
    """

    def check_update(self, update: object) -> bool:
        return widget_query_of(update) is not None


# ---------------------------------------------------------------------------- #
# Ballot
# ---------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Ballot:
    """A rendered ballot and the game order everything about it is indexed by.

    The widget names a game by its position, and game_ids turns that position
    back into an id. Ids appear in the HTML only to build each game's BGG link,
    and are the first thing dropped when a ballot is too big: at ~4 compressed
    bytes each they cost a quarter of the room a game takes.
    """

    html: str
    # Changes whenever the baked-in game list does; a ballot whose hash is stale
    # has to be re-sent, since its HTML cannot be edited.
    catalog_hash: str
    game_ids: tuple[int, ...]
    levels: tuple[int, ...]


def _catalog_entry(name: str, game_id: int, starred: bool) -> str | list[str | int]:
    """A game on the ballot: a bare name, or [name, BGG id, starred] cut short.

    game_id is 0 on a ballot that carries no ids.
    """
    if starred:
        return [name, game_id, 1]
    return [name, game_id] if game_id else name


@cache
def _ballot_shell() -> str:
    """The ballot template without comments or indentation.

    Line breaks are kept, so this cannot change what the script means.
    """
    source = _BALLOT_TEMPLATE.read_text(encoding="utf-8")
    # The slot is itself written as a comment, so it is set aside first.
    source = source.replace(_CATALOG_SLOT, "@@CATALOG@@")
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    lines = (line.strip() for line in source.splitlines())
    return "\n".join(line for line in lines if line).replace("@@CATALOG@@", _CATALOG_SLOT)


def build_ballot(
    title: str,
    games: Sequence[Game],
    priority_ids: Collection[int],
    name_limit: int | None = None,
    shuffle_seed: int | None = None,
    with_ids: bool = True,
) -> Ballot:
    """Render the ballot widget for a poll's games.

    Games are grouped by complexity, heaviest first, matching the order the
    poll has always listed them in; within a level the starred games lead, then
    the rest by name. With a shuffle_seed each level is shuffled instead, the
    same way for every ballot of the poll.

    Two things buy room on a ballot that is too big: with_ids=False leaves out
    the BGG ids (each game then links to a BGG search for its name), and
    name_limit shortens long names.

    Raises:
        WidgetTooLarge: the HTML is over the raw or the compressed limit.
    """
    groups = group_games_by_complexity(list(games))
    levels = tuple(sorted(groups, reverse=True))
    for level, group in groups.items():
        if shuffle_seed is None:
            # The id settles ties between same-named games: positions must not
            # depend on the order the database happened to return them in.
            group.sort(key=lambda g: (g.id not in priority_ids, g.name, g.id))
        else:
            # Seeded per level, so one level changing leaves the others alone.
            group.sort(key=lambda g: g.id)
            random.Random(shuffle_seed ^ level).shuffle(group)

    def short(name: str) -> str:
        if name_limit is None or len(name) <= name_limit:
            return name
        return name[: name_limit - 1].rstrip() + "…"

    catalog = {
        "n": title,
        "g": [
            [
                level,
                [
                    # Manually entered games have made-up negative ids, which
                    # are no use for a BGG link.
                    _catalog_entry(
                        short(g.name), g.id if with_ids and g.id > 0 else 0, g.id in priority_ids
                    )
                    for g in groups[level]
                ],
            ]
            for level in levels
        ],
    }
    # "</" is escaped so a game name can never close the script element.
    payload = json.dumps(catalog, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    html = _ballot_shell().replace(_CATALOG_SLOT, payload)

    raw = html.encode("utf-8")
    packed = len(zlib.compress(raw, 9))
    if len(raw) > WIDGET_HTML_MAX_BYTES or packed > WIDGET_HTML_MAX_ZLIB_BYTES:
        raise WidgetTooLarge(
            f"ballot is {len(raw)} bytes, {packed} compressed "
            f"(limits {WIDGET_HTML_MAX_BYTES} / {WIDGET_HTML_MAX_ZLIB_BYTES})"
        )

    game_ids = tuple(g.id for level in levels for g in groups[level])
    # The ids are hashed even when the HTML leaves them out, so swapping one
    # game for another of the same name still counts as a different ballot.
    fingerprint = f"{payload}{game_ids}".encode()
    return Ballot(
        html=html,
        catalog_hash=hashlib.sha256(fingerprint).hexdigest()[:16],
        game_ids=game_ids,
        levels=levels,
    )


def _base36_digit(count: int) -> str:
    """One character per tally; anything past 35 votes reads as 35."""
    return "0123456789abcdefghijklmnopqrstuvwxyz"[max(0, min(count, 35))]


def build_ballot_state(
    ballot: Ballot,
    my_game_ids: Collection[int],
    my_levels: Collection[int],
    limit: int | None,
    vote_counts: Mapping[int, int] | None = None,
    category_counts: Mapping[int, int] | None = None,
    closed: str | None = None,
    error: str | None = None,
    paused: bool = False,
) -> str:
    """Serialize one user's view of the poll as widget data.

    Keys are single letters because the whole thing must fit in 1024 bytes:
    r revision, m my game votes as ballot positions, c my category votes,
    l vote limit (0 = none),
    t / k tallies as one base36 digit per game / per level in ballot order,
    x closing text, e a one-off refusal message, p set while the viewer is out
    of the lobby (their votes are kept but not counted, so voting is off).

    Pass vote_counts=None to withhold tallies (hide_results). Votes for games
    that are no longer on the ballot are left out, as they are from the limit.

    Raises:
        WidgetTooLarge: the state is over the data limit.
    """
    mine = set(my_game_ids)
    state: dict[str, Any] = {
        # Widgets drop any state older than the one they hold, so pushes and
        # answers that arrive out of order cannot roll a ballot back.
        "r": time.time_ns() // 1_000_000,
        "m": [x for x, g in enumerate(ballot.game_ids) if g in mine],
        "c": sorted(lv for lv in my_levels if lv in ballot.levels),
        "l": limit or 0,
    }
    if vote_counts is not None:
        state["t"] = "".join(_base36_digit(vote_counts.get(g, 0)) for g in ballot.game_ids)
        counts = category_counts or {}
        state["k"] = "".join(_base36_digit(counts.get(lv, 0)) for lv in ballot.levels)
    if closed is not None:
        state["x"] = closed
    if error is not None:
        state["e"] = error
    if paused:
        state["p"] = 1

    data = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
    if len(data.encode("utf-8")) > WIDGET_DATA_MAX_BYTES:
        raise WidgetTooLarge(f"ballot state is {len(data.encode('utf-8'))} bytes")
    return data


def closed_ballot_state(text: str) -> str:
    """State for a ballot whose poll is over; needs no knowledge of its games."""
    state = {"r": time.time_ns() // 1_000_000, "m": [], "c": [], "l": 0, "x": text}
    return json.dumps(state, ensure_ascii=False, separators=(",", ":"))


async def send_ballot(
    bot: Bot, chat_id: int, ballot: Ballot, state: str, *, disable_notification: bool = False
) -> int:
    """Send a ballot to a private chat and return its message id.

    No height is passed: the bubble then takes the height of the ballot's first
    render, which the template keeps constant across tabs and pages.
    """
    message = await send_widget(
        bot,
        chat_id,
        ballot.html,
        chat_data=state,
        caption="If you can't see the ballot above, update Telegram.",
        disable_notification=disable_notification,
    )
    return int(message["message_id"])
