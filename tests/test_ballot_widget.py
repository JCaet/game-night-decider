"""Ballot widget: the HTML and state must stay inside Telegram's widget limits."""

import json
import random
import string
import zlib

import pytest
from telegram import Update

from src.bot.widgets import (
    WIDGET_DATA_MAX_BYTES,
    WIDGET_HTML_MAX_ZLIB_BYTES,
    WidgetQueryHandler,
    WidgetTooLarge,
    build_ballot,
    build_ballot_state,
    widget_query_of,
)
from src.core.models import Game


def _games(count: int, name_len: int = 18) -> list[Game]:
    """Games with random-letter names, which compress worse than real titles."""
    rng = random.Random(count)
    return [
        Game(
            id=1000 + i,
            name="".join(rng.choices(string.ascii_letters + " ", k=name_len)).strip() or "x",
            complexity=None if i % 10 == 9 else 1.0 + (i % 40) / 10,
        )
        for i in range(count)
    ]


def _catalog(html: str) -> dict:
    start = html.index("const C=") + len("const C=")
    catalog: dict = json.loads(html[start : html.index(";\n", start)].replace("<\\/", "</"))
    return catalog


def test_ballot_fits_compressed_limit_for_a_large_poll():
    games = _games(100)
    ballot = build_ballot("Game Night", games, {g.id for g in games[::5]}, with_ids=False)

    assert len(zlib.compress(ballot.html.encode("utf-8"), 9)) <= WIDGET_HTML_MAX_ZLIB_BYTES
    assert len(ballot.game_ids) == 100


def test_ballot_over_the_limit_raises_instead_of_sending():
    with pytest.raises(WidgetTooLarge):
        build_ballot("Game Night", _games(400, name_len=30), set())


def test_ballot_orders_games_heaviest_first_and_marks_starred():
    light = Game(id=1, name="Azul", complexity=1.8)
    heavy = Game(id=2, name="Brass", complexity=3.9)
    unrated = Game(id=3, name="Mystery", complexity=None)

    ballot = build_ballot("Night", [light, heavy, unrated], {2})

    assert ballot.levels == (3, 1, 0)
    assert ballot.game_ids == (2, 1, 3)
    # Each game carries its BGG id for the link, and a trailing 1 when starred.
    assert _catalog(ballot.html) == {
        "n": "Night",
        "g": [[3, [["Brass", 2, 1]]], [1, [["Azul", 1]]], [0, [["Mystery", 3]]]],
    }


def test_starred_games_lead_their_complexity_level():
    names = ["Ark Nova", "Brass", "Concordia", "Dune"]
    games = [Game(id=i, name=n, complexity=3.5) for i, n in enumerate(names, start=1)]

    ballot = build_ballot("Night", games, {2, 4})

    # Starred first, each half still by name.
    assert ballot.game_ids == (2, 4, 1, 3)
    assert _catalog(ballot.html)["g"] == [
        [3, [["Brass", 2, 1], ["Dune", 4, 1], ["Ark Nova", 1], ["Concordia", 3]]]
    ]


def test_ballot_name_cannot_close_the_script_element():
    ballot = build_ballot("Night", [Game(id=1, name="</script><b>x", complexity=2.0)], set())

    assert ballot.html.count("</script>") == 1
    assert _catalog(ballot.html)["g"][0][1] == [["</script><b>x", 1]]


def test_catalog_hash_follows_the_game_list():
    games = _games(5)
    same = build_ballot("Night", games, set())

    assert build_ballot("Night", list(reversed(games)), set()).catalog_hash == same.catalog_hash
    assert build_ballot("Night", games[:4], set()).catalog_hash != same.catalog_hash
    assert build_ballot("Night", games, {games[0].id}).catalog_hash != same.catalog_hash


def test_state_names_votes_by_ballot_position():
    light = Game(id=10, name="Azul", complexity=1.8)
    heavy = Game(id=20, name="Brass", complexity=3.9)
    ballot = build_ballot("Night", [light, heavy], set())

    # 99 is a vote for a game that has since left the ballot.
    state = json.loads(build_ballot_state(ballot, {10, 99}, {3, 5}, 4, {10: 2, 20: 40}, {3: 1}))

    assert state["m"] == [1]
    assert state["c"] == [3]
    assert state["l"] == 4
    assert state["t"] == "z2"  # ballot order is Brass, Azul; counts cap at 35
    assert state["k"] == "10"


def test_state_withholds_tallies_when_results_are_hidden():
    ballot = build_ballot("Night", _games(3), set())

    state = json.loads(build_ballot_state(ballot, set(), set(), None, closed="Winner: Azul"))

    assert "t" not in state and "k" not in state
    assert state["l"] == 0
    assert state["x"] == "Winner: Azul"


def test_state_fits_data_limit_in_the_worst_case():
    games = _games(200, name_len=4)
    ballot = build_ballot("Night", games, set())
    ids = [g.id for g in games]

    state = build_ballot_state(
        ballot, ids[:60], ballot.levels, 60, dict.fromkeys(ids, 9), dict.fromkeys(ballot.levels, 9)
    )

    assert len(state.encode("utf-8")) <= WIDGET_DATA_MAX_BYTES


def test_widget_query_is_read_from_api_kwargs():
    query = {"id": "q1", "from": {"id": 7, "is_bot": False, "first_name": "A"}, "data": "v:3"}
    update = Update.de_json({"update_id": 1, "widget_query": query}, None)
    plain = Update.de_json({"update_id": 2}, None)
    handler = WidgetQueryHandler(lambda u, c: None)

    assert widget_query_of(update) == query
    assert handler.check_update(update) is True
    assert handler.check_update(plain) is False
    assert handler.check_update("not an update") is False


def test_ballot_without_ids_keeps_names_bare():
    """Dropping ids is how a big ballot is made to fit; links then search by name."""
    games = [Game(id=10, name="Azul", complexity=1.8), Game(id=20, name="Brass", complexity=3.9)]

    ballot = build_ballot("Night", games, {20}, with_ids=False)

    assert _catalog(ballot.html)["g"] == [[3, [["Brass", 0, 1]]], [1, ["Azul"]]]
    # Positions still resolve to ids on the bot's side.
    assert ballot.game_ids == (20, 10)
    assert len(ballot.html) < len(build_ballot("Night", games, {20}).html)


def test_same_named_games_have_a_stable_order_and_their_own_hash():
    """Positions name games, so two with one name must never swap places."""
    twins = [Game(id=7, name="Catan", complexity=2.3), Game(id=3, name="Catan", complexity=2.3)]

    ballot = build_ballot("Night", twins, set(), with_ids=False)

    assert ballot.game_ids == (3, 7)
    assert build_ballot("Night", twins[::-1], set(), with_ids=False).game_ids == (3, 7)
    other = [Game(id=7, name="Catan", complexity=2.3), Game(id=4, name="Catan", complexity=2.3)]
    assert build_ballot("Night", other, set(), with_ids=False).catalog_hash != ballot.catalog_hash
