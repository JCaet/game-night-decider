from typing import Any

from sqlalchemy import func

from src.core.models import Game


def player_count_blocked(column: Any, n: int) -> Any:
    """
    SQLAlchemy expression: True iff `n` is present in the CSV-encoded
    Game.community_unplayable_counts column. Uses LIKE patterns anchored on
    commas so "1" doesn't match inside "15".
    """
    needle = str(n)
    col = func.coalesce(column, "")
    return (
        (col == needle)
        | col.like(f"{needle},%")
        | col.like(f"%,{needle}")
        | col.like(f"%,{needle},%")
    )


def group_games_by_complexity(games: list[Game]) -> dict[int, list[Game]]:
    """
    Group games by their integer complexity level (floor).

    Args:
        games: List of games to group.

    Returns:
        Dictionary mapping complexity level (1-5) to list of games.
        Games with complexity < 1.0 are grouped under 1.
        Games with complexity >= 5.0 are grouped under 5.
        Games with complexity None are grouped under 0 (Unrated).
    """
    groups: dict[int, list[Game]] = {0: [], 1: [], 2: [], 3: [], 4: [], 5: []}

    for game in games:
        if game.complexity is None or game.complexity <= 0:
            groups[0].append(game)
            continue

        level = int(game.complexity)
        # Clamp to 1-5 range (though BGG is max 5)
        if level < 1:
            level = 1
        elif level > 5:
            level = 5

        groups[level].append(game)

    # Sort games within each group by name
    for level in groups:
        groups[level].sort(key=lambda g: g.name)

    # Remove empty groups
    return {k: v for k, v in groups.items() if v}


# ---------------------------------------------------------------------------- #
# Poll Winner Calculation
# ---------------------------------------------------------------------------- #

# Star boost constant for weighted voting
STAR_BOOST = 0.5


def calculate_poll_winner(
    games: list,
    votes: list,
    priority_game_ids: set,
    is_weighted: bool = False,
    star_collections: dict | None = None,
) -> tuple[list[str], dict[str, float], list[str]]:
    """
    Calculate the winner(s) of a poll based on votes and optional weighting.

    Args:
        games: List of Game objects that were in the poll
        votes: List of vote records with game_id attribute
        priority_game_ids: Set of game IDs that are starred by any player
        is_weighted: Whether to apply star boost to voting
        star_collections: Dict mapping game_id -> list of user_ids who starred it
                         (needed for weighted voting calculation)

    Returns:
        Tuple of:
        - winners: List of game names with max score
        - scores: Dict mapping game_name -> final score
        - modifiers_log: List of modifier descriptions (e.g., "Catan: +0.5 (⭐)")
    """
    scores = {}
    modifiers_log = []

    for game in games:
        # Count votes for this game
        game_votes = [v for v in votes if v.game_id == game.id]
        base_votes = len(game_votes)
        score = float(base_votes)

        # Apply Star Boost if weighted voting is enabled
        if is_weighted and game.id in priority_game_ids and star_collections:
            # Count how many voters have this game starred
            voter_ids = {v.user_id for v in game_votes}
            starred_by = star_collections.get(game.id, [])
            priority_voters = len(set(starred_by) & voter_ids)

            if priority_voters > 0:
                boost = priority_voters * STAR_BOOST
                score += boost
                modifiers_log.append(f"{game.name}: +{boost} (⭐)")

        scores[game.name] = score

    # Determine winner(s)
    winners = []
    if scores:
        max_score = max(scores.values())
        if max_score > 0:
            winners = [name for name, s in scores.items() if s == max_score]

    return winners, scores, modifiers_log
