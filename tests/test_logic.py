from src.core.logic import split_games
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
    games = [
        create_game("Catan", 2.3),
        create_game("Carcassonne", 1.9),
    ]
    result = split_games(games, max_per_poll=10)
    assert len(result) == 1
    assert result[0][0] == "All Games"
    assert len(result[0][1]) == 2


def test_split_games_large_list_with_gap():
    # 5 Light games (1.0 - 1.5)
    light = [create_game(f"Light {i}", 1.0 + i * 0.1) for i in range(5)]
    # 6 Heavy games (4.0 - 4.5)
    heavy = [create_game(f"Heavy {i}", 4.0 + i * 0.1) for i in range(6)]

    games = light + heavy
    # Total 11 games, max 10 -> Should split.

    result = split_games(games, max_per_poll=10)

    assert len(result) == 2

    # First chunk should be light
    label1, chunk1 = result[0]
    assert "Light" in label1
    assert len(chunk1) == 5
    assert chunk1[0].name.startswith("Light")

    # Second chunk should be heavy
    label2, chunk2 = result[1]
    assert "Heavy" in label2
    assert len(chunk2) == 6
    assert chunk2[0].name.startswith("Heavy")


def test_split_games_uniform_distribution():
    # 15 games, 1.0 to 2.4 (0.1 increments)
    games = [create_game(f"Game {i}", 1.0 + i * 0.1) for i in range(15)]

    result = split_games(games, max_per_poll=10)

    assert len(result) == 2
    # Should split roughly in half (7 and 8) or where the largest gap is (constant gap, so first one usually?)
    # With constant gap, any split is valid, but logic takes largest gap.
    # Since gaps are equal, `gaps.sort(reverse=True)` might preserve order or reverse.
    # Python sort is stable.

    total_games = sum(len(c) for l, c in result)
    assert total_games == 15
