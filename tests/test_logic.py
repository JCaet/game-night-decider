from dataclasses import dataclass

import pytest
from sqlalchemy import func, select

from src.core import db
from src.core.logic import (
    STAR_BOOST,
    calculate_poll_winner,
    player_count_blocked,
)
from src.core.models import Collection, Game, GameState, User, parse_unplayable_counts


def create_game(name: str, complexity: float) -> Game:
    return Game(
        id=hash(name),
        name=name,
        min_players=1,
        max_players=4,
        playing_time=60,
        complexity=complexity,
    )


# ============================================================================
# calculate_poll_winner tests
# ============================================================================


@dataclass
class MockVote:
    """Mock vote for testing."""

    game_id: int
    user_id: int


def test_calculate_poll_winner_basic():
    """Test basic winner calculation with clear winner."""
    games = [create_game("Winner", 2.0), create_game("Loser", 2.5)]
    # Give games IDs
    games[0].id = 1
    games[1].id = 2

    votes = [
        MockVote(game_id=1, user_id=111),
        MockVote(game_id=1, user_id=222),
        MockVote(game_id=2, user_id=333),
    ]

    winners, scores, modifiers = calculate_poll_winner(
        games, votes, priority_game_ids=set(), is_weighted=False
    )

    assert winners == ["Winner"]
    assert scores["Winner"] == 2.0
    assert scores["Loser"] == 1.0
    assert modifiers == []


def test_calculate_poll_winner_tie():
    """Test tie handling - both games with same votes."""
    games = [create_game("Game1", 2.0), create_game("Game2", 2.5)]
    games[0].id = 1
    games[1].id = 2

    votes = [
        MockVote(game_id=1, user_id=111),
        MockVote(game_id=2, user_id=222),
    ]

    winners, scores, modifiers = calculate_poll_winner(
        games, votes, priority_game_ids=set(), is_weighted=False
    )

    assert len(winners) == 2
    assert "Game1" in winners
    assert "Game2" in winners


def test_calculate_poll_winner_no_votes():
    """Test with no votes."""
    games = [create_game("Game1", 2.0)]
    games[0].id = 1

    winners, scores, modifiers = calculate_poll_winner(
        games, votes=[], priority_game_ids=set(), is_weighted=False
    )

    assert winners == []
    assert scores["Game1"] == 0.0


def test_calculate_poll_winner_with_star_boost():
    """Test weighted voting with star boost breaks tie."""
    games = [create_game("StarredGame", 2.0), create_game("NormalGame", 2.5)]
    games[0].id = 1
    games[1].id = 2

    votes = [
        MockVote(game_id=1, user_id=111),  # User 111 voted for starred game
        MockVote(game_id=2, user_id=222),
    ]

    # User 111 has StarredGame starred
    star_collections = {1: [111]}

    winners, scores, modifiers = calculate_poll_winner(
        games,
        votes,
        priority_game_ids={1},  # Game 1 is starred
        is_weighted=True,
        star_collections=star_collections,
    )

    # StarredGame should win due to boost
    assert winners == ["StarredGame"]
    assert scores["StarredGame"] == 1.0 + STAR_BOOST
    assert scores["NormalGame"] == 1.0
    assert len(modifiers) == 1
    assert "StarredGame" in modifiers[0]


# ---------------------------------------------------------------------------- #
# Community player-count blocklist
# ---------------------------------------------------------------------------- #


def test_parse_unplayable_counts():
    assert parse_unplayable_counts(None) == set()
    assert parse_unplayable_counts("") == set()
    assert parse_unplayable_counts("5") == {5}
    assert parse_unplayable_counts("2,5,7") == {2, 5, 7}
    # Whitespace and bogus tokens are skipped
    assert parse_unplayable_counts("5, ,abc,7") == {5, 7}


def _make_game(
    *,
    game_id: int,
    name: str,
    min_p: int,
    max_p: int,
    blocklist: str | None,
) -> Game:
    return Game(
        id=game_id,
        name=name,
        min_players=min_p,
        max_players=max_p,
        playing_time=60,
        complexity=2.0,
        community_unplayable_counts=blocklist,
    )


@pytest.mark.asyncio
async def test_player_count_blocked_filter_excludes_blocked_count():
    """A game with '5' in community_unplayable_counts should be hidden at player_count=5."""
    async with db.AsyncSessionLocal() as session:
        session.add_all(
            [
                _make_game(
                    game_id=397598,
                    name="Dune Uprising",
                    min_p=1,
                    max_p=6,
                    blocklist="5",
                ),
                _make_game(
                    game_id=13,
                    name="Catan",
                    min_p=3,
                    max_p=4,
                    blocklist="",
                ),
                _make_game(
                    game_id=999,
                    name="Unparsed",
                    min_p=2,
                    max_p=8,
                    blocklist=None,
                ),
            ]
        )
        await session.commit()

        # At 5p: Dune is blocked, Catan's max=4 excludes it, Unparsed (NULL) passes through.
        stmt = select(Game).where(
            Game.min_players <= 5,
            Game.max_players >= 5,
            ~player_count_blocked(Game.community_unplayable_counts, 5),
        )
        names_5p = {g.name for g in (await session.execute(stmt)).scalars().all()}
        assert names_5p == {"Unparsed"}

        # At 4p: Dune passes (4 not blocked), Catan passes, Unparsed passes.
        stmt = select(Game).where(
            Game.min_players <= 4,
            Game.max_players >= 4,
            ~player_count_blocked(Game.community_unplayable_counts, 4),
        )
        names_4p = {g.name for g in (await session.execute(stmt)).scalars().all()}
        assert names_4p == {"Dune Uprising", "Catan", "Unparsed"}


@pytest.mark.asyncio
async def test_player_count_blocked_csv_substring_safety():
    """'1' must not match inside '15'; commas anchor the search."""
    async with db.AsyncSessionLocal() as session:
        # Hypothetical 18-player Werewolves variant — not real, just a substring stress-test
        session.add(
            _make_game(
                game_id=42,
                name="Multi-block",
                min_p=1,
                max_p=20,
                blocklist="3,15,17",
            )
        )
        await session.commit()

        for n, expected_blocked in [
            (1, False),
            (3, True),
            (5, False),
            (7, False),  # not the "17" suffix
            (15, True),
            (17, True),
            (20, False),
        ]:
            stmt = select(Game.id).where(player_count_blocked(Game.community_unplayable_counts, n))
            hits = (await session.execute(stmt)).scalars().all()
            assert (len(hits) == 1) == expected_blocked, (
                f"player_count={n} expected_blocked={expected_blocked}, hits={hits}"
            )


@pytest.mark.asyncio
async def test_manage_override_interacts_correctly_with_community_blocklist():
    """Mirror the create_poll filter: Collection.effective_max_players (set via
    /manage) extends the playable range upward; the community blocklist still
    drops counts within the official range that the parser flagged."""
    user_id = 111
    async with db.AsyncSessionLocal() as session:
        session.add(User(telegram_id=user_id, telegram_name="Tester"))

        # Dune Uprising-shaped: official 1-6, community blocks 5p (a non-max count).
        session.add(
            _make_game(
                game_id=397598,
                name="Dune Uprising",
                min_p=1,
                max_p=6,
                blocklist="5",
            )
        )
        # Deep Regrets-shaped: official 1-5, community blocks NOTHING under the
        # new "max is sacred" rule (issue #55), so blocklist is empty.
        session.add(
            _make_game(
                game_id=397931,
                name="Deep Regrets",
                min_p=1,
                max_p=5,
                blocklist="",
            )
        )
        await session.flush()

        # User has both in their collection. Dune gets a manual /manage override
        # bumping its effective max to 8 (homebrew variant). Deep Regrets is left
        # untouched — default state, no override.
        session.add(
            Collection(
                user_id=user_id,
                game_id=397598,
                state=GameState.INCLUDED,
                effective_max_players=8,
                is_manual_player_override=True,
            )
        )
        session.add(
            Collection(
                user_id=user_id,
                game_id=397931,
                state=GameState.INCLUDED,
            )
        )
        await session.commit()

        # Mirror the exact filter shape from handlers.py:create_poll.
        def _query(player_count: int):
            return (
                select(Game.name)
                .join(Collection, Collection.game_id == Game.id)
                .where(
                    Collection.user_id == user_id,
                    Collection.state != GameState.EXCLUDED,
                    Game.min_players <= player_count,
                    func.coalesce(Collection.effective_max_players, Game.max_players)
                    >= player_count,
                    ~player_count_blocked(Game.community_unplayable_counts, player_count),
                )
            )

        # 5p: Deep Regrets shows (max not blocked under new rule); Dune is dropped
        # by the community blocklist even though the manual override extends to 8.
        names_5p = set((await session.execute(_query(5))).scalars().all())
        assert names_5p == {"Deep Regrets"}, (
            "Deep Regrets must appear at its official max of 5p; "
            "Dune's 5p block must still apply despite the /manage override extending max to 8."
        )

        # 6p: Deep Regrets out of range; Dune passes (6p not blocked, max=6 anyway).
        names_6p = set((await session.execute(_query(6))).scalars().all())
        assert names_6p == {"Dune Uprising"}

        # 8p: only Dune, only via manual override. Confirms /manage extension works.
        names_8p = set((await session.execute(_query(8))).scalars().all())
        assert names_8p == {"Dune Uprising"}

        # 9p: nothing — manual override capped at 8.
        names_9p = set((await session.execute(_query(9))).scalars().all())
        assert names_9p == set()

        # User then EXCLUDES Dune via /manage (state cycle → EXCLUDED).
        await session.execute(
            Collection.__table__.update()
            .where(Collection.user_id == user_id, Collection.game_id == 397598)
            .values(state=GameState.EXCLUDED)
        )
        await session.commit()

        names_6p_after = set((await session.execute(_query(6))).scalars().all())
        assert names_6p_after == set(), "EXCLUDED state from /manage must hide the game."
