from dataclasses import dataclass

import pytest
from sqlalchemy import func, select

from src.core import db
from src.core.logic import (
    STAR_BOOST,
    _find_best_split,
    _get_complexity_label,
    calculate_poll_winner,
    player_count_blocked,
    split_games,
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


def test_split_games_small_list():
    """Small lists (<=12) should return a single 'Games' group."""
    games = [
        create_game("Catan", 2.3),
        create_game("Carcassonne", 1.9),
    ]
    result = split_games(games, max_per_poll=10)
    assert len(result) == 1
    assert result[0][0] == "Games"
    assert len(result[0][1]) == 2


def test_split_games_default_limit_is_12():
    """Default max_per_poll is 12 (API 9.1+)."""
    # 11 games should fit in a single group with the new default
    games = [create_game(f"Game {i}", 2.0 + i * 0.05) for i in range(11)]
    result = split_games(games)
    assert len(result) == 1
    assert len(result[0][1]) == 11

    # 13 games should split
    games = [create_game(f"Game {i}", 2.0 + i * 0.05) for i in range(13)]
    result = split_games(games)
    total = sum(len(chunk) for _, chunk in result)
    assert total == 13
    assert len(result) >= 2


def test_split_games_gap_based_split():
    """Games should be split at the largest complexity gap."""
    # Create games with a clear gap between light and heavy
    # 5 Light games (1.0 - 1.4)
    light = [create_game(f"Light {i}", 1.0 + i * 0.1) for i in range(5)]
    # 6 Heavy games (3.0 - 3.5)
    heavy = [create_game(f"Heavy {i}", 3.0 + i * 0.1) for i in range(6)]

    games = light + heavy
    # Total 11 games -> should split at the gap between 1.4 and 3.0

    result = split_games(games, max_per_poll=10)

    assert len(result) == 2

    # First chunk should be light
    label1, chunk1 = result[0]
    assert label1 == "Light / Party Games"
    assert len(chunk1) == 5
    assert all(g.name.startswith("Light") for g in chunk1)

    # Second chunk should be heavy
    label2, chunk2 = result[1]
    assert label2 == "Heavy Strategy Games"
    assert len(chunk2) == 6
    assert all(g.name.startswith("Heavy") for g in chunk2)


def test_split_games_avoids_single_game():
    """Never create a single-game group - merge with neighbors instead."""
    # 10 light games + 1 heavy game
    # The heavy game should NOT be isolated
    light = [create_game(f"Light {i}", 1.0 + i * 0.05) for i in range(10)]
    heavy = [create_game("Heavy 1", 3.5)]

    games = light + heavy
    result = split_games(games, max_per_poll=10)

    # Check that no group has only 1 game
    for _label, chunk in result:
        assert len(chunk) >= 2, f"Single-game group found: {_label}"


def test_split_games_edge_penalty():
    """Edge gaps should be penalized to prefer groups of 3+."""
    # 6 games with uniform gaps of 0.3, but the first gap would create a group of 1
    # and the last gap would also create a group of 1
    # We want the algorithm to prefer the middle gaps
    games = [
        create_game("A", 1.0),
        create_game("B", 1.3),  # Gap 0.3
        create_game("C", 1.6),  # Gap 0.3
        create_game("D", 2.0),  # Gap 0.4 - largest but at position that creates 3+3
        create_game("E", 2.3),  # Gap 0.3
        create_game("F", 2.6),  # Gap 0.3
    ]
    # Need more than 10 games to trigger splitting
    # Let's add more games
    more_games = [create_game(f"G{i}", 2.8 + i * 0.1) for i in range(6)]
    games = games + more_games

    result = split_games(games, max_per_poll=10)

    # All groups should have at least 2 games
    for _label, chunk in result:
        assert len(chunk) >= 2, f"Single-game group found: {_label}"


def test_split_games_category_overflow():
    """Categories with >10 games should split into multiple parts."""
    # 15 light games (complexity 1.0-1.5)
    games = [create_game(f"Light {i}", 1.0 + i * 0.03) for i in range(15)]

    result = split_games(games, max_per_poll=10)

    # Should be split into multiple chunks
    assert len(result) >= 2
    # All games should be included
    total_games = sum(len(chunk) for _, chunk in result)
    assert total_games == 15
    # No single-game groups
    for _label, chunk in result:
        assert len(chunk) >= 2, f"Single-game group found: {_label}"


def test_split_games_unrated():
    """Games with complexity 0.0 should go to 'Unrated Games'."""
    # 5 rated games
    rated = [create_game(f"Rated {i}", 2.5) for i in range(5)]
    # 8 unrated games (complexity 0.0)
    unrated = [create_game(f"Unrated {i}", 0.0) for i in range(8)]

    games = rated + unrated
    result = split_games(games, max_per_poll=10)

    assert len(result) == 2

    # First should be rated games
    label1, chunk1 = result[0]
    assert label1 == "Medium Weight Games"
    assert len(chunk1) == 5

    # Second should be unrated
    label2, chunk2 = result[1]
    assert label2 == "Unrated Games"
    assert len(chunk2) == 8


def test_split_games_all_categories():
    """Test with games across all categories - small list returns single group."""
    light = [create_game("Light 1", 1.5)]
    medium = [create_game("Medium 1", 2.5)]
    heavy = [create_game("Heavy 1", 3.5)]
    unrated = [create_game("Unrated 1", 0.0)]

    games = light + medium + heavy + unrated
    result = split_games(games, max_per_poll=10)

    # Small list returns single "Games" group (rated only, unrated separate)
    # Actually: 3 rated + 1 unrated
    # With 4 total games <= 10, rated get "Games" label if they're the only rated
    # But unrated is separate so we get 2 groups
    assert len(result) == 2
    labels = [r[0] for r in result]
    # Should have both rated and unrated groups
    assert "Unrated Games" in labels


def test_split_games_empty():
    """Empty list should return empty result."""
    result = split_games([], max_per_poll=10)
    assert result == []


def test_find_best_split_basic():
    """Test _find_best_split helper function."""
    games = [
        create_game("A", 1.0),
        create_game("B", 1.2),
        create_game("C", 1.4),
        create_game("D", 3.0),  # Big gap here
        create_game("E", 3.2),
        create_game("F", 3.4),
    ]

    split_idx = _find_best_split(games, min_group_size=2)
    # Should split at index 3 (before game D)
    assert split_idx == 3


def test_find_best_split_too_small():
    """_find_best_split should return None for groups that can't be split."""
    games = [
        create_game("A", 1.0),
        create_game("B", 2.0),
        create_game("C", 3.0),
    ]
    # With min_group_size=2, can't split 3 games into two groups of 2
    split_idx = _find_best_split(games, min_group_size=2)
    assert split_idx is None


def test_get_complexity_label():
    """Test _get_complexity_label helper function."""
    assert _get_complexity_label(1.0, 1.5) == "Light / Party Games"
    assert _get_complexity_label(2.0, 2.8) == "Medium Weight Games"
    assert _get_complexity_label(3.0, 4.0) == "Heavy Strategy Games"
    # Edge cases - based on average
    assert _get_complexity_label(1.5, 2.5) == "Medium Weight Games"  # avg 2.0


def test_split_games_all_same_complexity():
    """Games with identical complexity should still be grouped properly."""
    # 12 games all with complexity 2.5
    games = [create_game(f"Game {i}", 2.5) for i in range(12)]

    result = split_games(games, max_per_poll=10)

    # Should split into chunks since > 10 games
    assert len(result) >= 1
    # All games should be included
    total = sum(len(chunk) for _, chunk in result)
    assert total == 12
    # No single-game groups
    for _label, chunk in result:
        assert len(chunk) >= 2


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
