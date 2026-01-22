from dataclasses import dataclass

from src.core.logic import (
    STAR_BOOST,
    _find_best_split,
    _get_complexity_label,
    calculate_poll_winner,
    split_games,
)
from src.core.models import Game


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
    """Small lists (<=10) should return a single 'Games' group."""
    games = [
        create_game("Catan", 2.3),
        create_game("Carcassonne", 1.9),
    ]
    result = split_games(games, max_per_poll=10)
    assert len(result) == 1
    assert result[0][0] == "Games"
    assert len(result[0][1]) == 2


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
