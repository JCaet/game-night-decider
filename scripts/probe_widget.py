"""Prove the poll ballot widget against the live Bot API before building on it.

Bot API 10.4 widgets are new and python-telegram-bot 22.x predates them, so
three things are assumptions until measured:

* how many games fit -- the HTML must compress (zlib level 9) to 4096 bytes;
* that a ``widget_query`` update reaches PTB at all, in ``Update.api_kwargs``;
* that ``editMessageWidgetData`` is pushed to an open widget, and that the
  ballot is not clipped at its height on real clients.

The first needs no network:

    uv run python scripts/probe_widget.py measure

The rest needs a private chat, the only place production allows widgets:

    TELEGRAM_BOT_TOKEN=... uv run python scripts/probe_widget.py live <your_user_id> [games]

Use a throwaway bot, not the deployed one: this reads updates with getUpdates,
which Telegram refuses while a webhook is set. Start the bot from your account
first. Tap around the ballot; every query is printed raw, answered, and stored.
With no taps for a while a fake vote is pushed, which should appear in the open
ballot by itself. Ctrl+C to stop; the ballot message is left in the chat so it
can be checked on other clients.
"""

import asyncio
import contextlib
import json
import os
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from telegram import Bot  # noqa: E402
from telegram.error import TelegramError  # noqa: E402

from src.bot.widgets import (  # noqa: E402
    WIDGET_HTML_MAX_ZLIB_BYTES,
    WIDGET_QUERY_UPDATE,
    Ballot,
    WidgetTooLarge,
    answer_widget_query,
    build_ballot,
    build_ballot_state,
    edit_widget_data,
    send_ballot,
    widget_query_of,
)
from src.core.models import Game  # noqa: E402

# Real titles, because the limit is on compressed size and invented names
# compress far better than a real collection does.
NAMES = (
    """
Gloomhaven|Brass: Birmingham|Pandemic Legacy: Season 1|Ark Nova|Terraforming Mars
Twilight Imperium: Fourth Edition|Dune: Imperium|Gaia Project|Star Wars: Rebellion
Spirit Island|War of the Ring: Second Edition|Twilight Struggle|Great Western Trail|Scythe
The Castles of Burgundy|Through the Ages: A New Story of Civilization|7 Wonders Duel
Brass: Lancashire|Nemesis|Concordia|A Feast for Odin|Terra Mystica|Wingspan
Clank! Legacy: Acquisitions Incorporated|Root|Everdell|Orléans|Arkham Horror: The Card Game
Mage Knight Board Game|Lost Ruins of Arnak|Barrage|Food Chain Magnate
Viticulture Essential Edition|Puerto Rico|Underwater Cities|Agricola|Blood Rage
Mansions of Madness: Second Edition|Kanban EV|Pax Pamir: Second Edition|Too Many Bones
The Crew: Mission Deep Sea|Cascadia|Anachrony|On Mars|Crokinole
Marvel Champions: The Card Game|Le Havre|Power Grid|Tzolk'in: The Mayan Calendar
The Gallerist|Azul|Lisboa|Maracaibo|Sleeping Gods|Race for the Galaxy
Caverna: The Cave Farmers|Eclipse: Second Dawn for the Galaxy|Teotihuacan: City of Gods
Trickerion: Legends of Illusion|Kingdom Death: Monster|Paladins of the West Kingdom
Mechs vs. Minions|Five Tribes|Heat: Pedal to the Metal|The Quacks of Quedlinburg
Clans of Caledonia|Grand Austria Hotel|Android: Netrunner|El Grande|Pandemic|7 Wonders
Dominion|Catan|Carcassonne|Ticket to Ride: Europe|Codenames|Splendor|Kingdomino|Patchwork
Jaipur|Sushi Go Party!|Love Letter|The Resistance: Avalon|Decrypto|Just One|Wavelength|Dixit
Mysterium|Betrayal at House on the Hill|Cosmic Encounter|Small World|King of Tokyo|Bohnanza
Coup|Hanabi|The Mind|Skull|Camel Up|Azul: Summer Pavilion|Sagrada|Santorini|Hive|Onitama
Targi|Lost Cities|Star Realms|Res Arcana|Quacks & Co.|Century: Spice Road|Istanbul|Stone Age
Lords of Waterdeep|Champions of Midgard|Raiders of the North Sea
Architects of the West Kingdom|Tapestry|Charterstone|Between Two Cities
Isle of Skye: From Chieftain to King|Kemet|Cyclades|Inis|Rising Sun|Chaos in the Old World
Forbidden Stars|Battlestar Galactica: The Board Game|Dead of Winter|Zombicide: Black Plague
Robinson Crusoe: Adventures on the Cursed Island|This War of Mine: The Board Game
Eldritch Horror|Arkham Horror (Third Edition)|T.I.M.E Stories
Detective: A Modern Crime Board Game|Sherlock Holmes Consulting Detective
Chronicles of Crime|Exit: The Game – The Abandoned Cabin|Photosynthesis|Parks|Meadow|Calico
Flamecraft|Planet Unknown|Earth|Wyrmspan|Sky Team|Harmonies|Forest Shuffle|Darwin's Journey
Voidfall|Hegemony: Lead Your Class to Victory|Frosthaven|Oathsworn: Into the Deepwood|Revive
Carnegie|Praga Caput Regni|Bitoku|Golem|Messina 1347|Tiletum|Woodcraft|Nucleum
Kutná Hora: The City of Silver
""".strip()
    .replace("\n", "|")
    .split("|")
)


def sample_games(count: int) -> list[Game]:
    """count games spread over every complexity level, a tenth of them unrated."""
    games = []
    for i in range(count):
        name = NAMES[i % len(NAMES)] + ("" if i < len(NAMES) else f" {i // len(NAMES) + 1}")
        complexity = None if i % 10 == 9 else 1.0 + (i * 7 % 40) / 10
        games.append(Game(id=100000 + i * 37, name=name, complexity=complexity))
    return games


def measure() -> None:
    """Print how close each ballot size comes to the compressed-HTML limit."""
    print(
        f"{'games':>6} {'name cap':>9} {'raw':>7} {'zlib-9':>7}  limit {WIDGET_HTML_MAX_ZLIB_BYTES}"
    )
    for count in (0, 25, 50, 75, 100, 125, 151, 200):
        for name_limit in (None, 24, 16):
            games = sample_games(count)
            starred = {g.id for g in games[::6]}
            try:
                ballot = build_ballot("Friday Game Night", games, starred, name_limit)
            except WidgetTooLarge as exc:
                print(f"{count:>6} {name_limit or '-':>9}  too large: {exc}")
                continue
            raw = ballot.html.encode("utf-8")
            print(
                f"{count:>6} {name_limit or '-':>9} {len(raw):>7} {len(zlib.compress(raw, 9)):>7}"
            )
    # Worst case for the state: every game has votes and the user voted a lot.
    games = sample_games(151)
    ballot = build_ballot("x", games[:25], set())
    full = build_ballot_state(
        Ballot(ballot.html, ballot.catalog_hash, tuple(g.id for g in games), ballot.levels),
        [g.id for g in games[:20]],
        ballot.levels,
        20,
        {g.id: 9 for g in games},
        dict.fromkeys(ballot.levels, 9),
    )
    print(f"state for 151 games, 20 own votes: {len(full.encode('utf-8'))} bytes (limit 1024)")


async def live(chat_id: int, count: int) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        sys.exit("TELEGRAM_BOT_TOKEN is not set")

    games = sample_games(count)
    starred = {g.id for g in games[::6]}
    ballot = build_ballot("Widget probe", games, starred)
    mine: set[int] = set()
    levels: set[int] = set()
    counts: dict[int, int] = {}
    limit = 5

    def state(error: str | None = None) -> str:
        return build_ballot_state(ballot, mine, levels, limit, counts, {}, error=error)

    bot = Bot(token)
    async with bot:
        me = await bot.get_me()
        print(f"probing as @{me.username} in chat {chat_id} with {count} games")
        try:
            message_id = await send_ballot(bot, chat_id, ballot, state())
        except TelegramError as exc:
            sys.exit(f"sendWidget failed: {exc}")
        print(f"ballot sent as message {message_id}; tap it. Waiting for {WIDGET_QUERY_UPDATE}...")

        offset = None
        idle = 0
        while True:
            updates = await bot.get_updates(
                offset=offset, timeout=10, allowed_updates=["message", WIDGET_QUERY_UPDATE]
            )
            if not updates:
                idle += 1
                if idle % 2 == 0:
                    # Nobody tapped: someone else "votes", to show a live push.
                    target = ballot.game_ids[idle % len(ballot.game_ids)]
                    counts[target] = counts.get(target, 0) + 1
                    await edit_widget_data(bot, chat_id, message_id, state())
                    print(f"pushed a vote for game {target}; the open ballot should show it")
                continue
            idle = 0
            for update in updates:
                offset = update.update_id + 1
                query = widget_query_of(update)
                if query is None:
                    print(f"other update: api_kwargs={dict(update.api_kwargs)}")
                    continue
                print("widget_query:", json.dumps(query, ensure_ascii=False))
                kind, _, arg = str(query.get("data", "")).partition(":")
                chosen = levels if kind == "c" else mine
                error = None
                with contextlib.suppress(ValueError, IndexError):
                    # A game is named by its position on the ballot, a level by itself.
                    key = int(arg) if kind == "c" else ballot.game_ids[int(arg)]
                    if key in chosen:
                        chosen.discard(key)
                        counts[key] = max(0, counts.get(key, 0) - 1)
                    elif len(mine) + len(levels) >= limit:
                        error = "Vote limit reached. Remove a vote first."
                    else:
                        chosen.add(key)
                        counts[key] = counts.get(key, 0) + 1
                fresh = state(error)
                await answer_widget_query(bot, query["id"], fresh)
                # The answer is not stored; without this a reopened ballot
                # would come back showing the votes it was sent with.
                await edit_widget_data(bot, chat_id, message_id, state())
                print(f"  answered and stored: {fresh}")


if __name__ == "__main__":
    # Unbuffered, so queries show up as they arrive even when output is piped.
    sys.stdout.reconfigure(line_buffering=True)  # type: ignore[union-attr]
    if len(sys.argv) >= 2 and sys.argv[1] == "measure":
        measure()
    elif len(sys.argv) >= 3 and sys.argv[1] == "live":
        with contextlib.suppress(KeyboardInterrupt):
            asyncio.run(live(int(sys.argv[2]), int(sys.argv[3]) if len(sys.argv) > 3 else 100))
    else:
        sys.exit(__doc__)
