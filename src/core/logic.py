from src.core.models import Game


def _get_complexity_label(min_c: float, max_c: float) -> str:
    """Generate a descriptive label based on complexity range."""
    # Determine weight category based on average complexity
    avg = (min_c + max_c) / 2
    if avg < 2.0:
        return "Light / Party Games"
    elif avg < 3.0:
        return "Medium Weight Games"
    else:
        return "Heavy Strategy Games"


def _find_best_split(
    games: list[Game], min_group_size: int = 2
) -> int | None:
    """
    Find the best split point in a sorted list of games.

    Uses complexity gap analysis with edge penalties:
    - Calculates gaps between consecutive games
    - Penalizes edge gaps (first/last 2 positions) by -0.2 to prefer groups of 3+
    - Returns the index to split at, or None if no valid split exists

    Args:
        games: List of games sorted by complexity
        min_group_size: Minimum games per group (default 2)

    Returns:
        Index to split at (split before this index), or None if no valid split
    """
    n = len(games)
    if n < min_group_size * 2:
        # Can't split into two valid groups
        return None

    # Calculate gaps between consecutive games
    gaps: list[tuple[int, float]] = []  # (index, adjusted_gap)
    for i in range(n - 1):
        c1 = games[i].complexity or 0
        c2 = games[i + 1].complexity or 0
        raw_gap = c2 - c1

        # Apply edge penalty: first 2 and last 2 positions get -0.2
        # This means splitting here would create a group of 1 or 2 games
        penalty = 0.0
        left_size = i + 1  # Games before split point
        right_size = n - left_size  # Games after split point

        if left_size < 3 or right_size < 3:
            penalty = 0.2

        adjusted_gap = raw_gap - penalty
        gaps.append((i + 1, adjusted_gap))  # Split point is after index i

    # Filter for valid splits (both sides have at least min_group_size)
    valid_gaps = [
        (idx, gap)
        for idx, gap in gaps
        if idx >= min_group_size and (n - idx) >= min_group_size
    ]

    if not valid_gaps:
        return None

    # Find the split with the largest adjusted gap
    best_split = max(valid_gaps, key=lambda x: x[1])
    return best_split[0]


def split_games(
    games: list[Game], max_per_poll: int = 10
) -> list[tuple[str, list[Game]]]:
    """
    Split games into logical groups using dynamic complexity gap analysis.

    Algorithm:
    1. Sort games by complexity (unrated games grouped separately)
    2. Find complexity gaps between consecutive games
    3. Penalize edge gaps (positions that would create groups <3) by -0.2
    4. Split at the largest adjusted gap that doesn't isolate single games
    5. Recursively process groups if they exceed max_per_poll

    Returns:
        List of (Label, List[Game]) tuples. Never creates single-game groups.
    """
    if not games:
        return []

    # Filter invalid entries
    valid_games = [g for g in games if g.name]

    if not valid_games:
        return []

    # Separate unrated games (complexity 0 or None)
    rated_games = [g for g in valid_games if (g.complexity or 0) > 0]
    unrated_games = [g for g in valid_games if (g.complexity or 0) <= 0]

    result: list[tuple[str, list[Game]]] = []

    def process_group(
        group: list[Game], label_prefix: str | None = None
    ) -> list[tuple[str, list[Game]]]:
        """Recursively process a group of games."""
        if not group:
            return []

        # Sort by complexity, then name
        group = sorted(group, key=lambda g: (g.complexity or 0, g.name.lower()))

        # If group fits in one poll, return it
        if len(group) <= max_per_poll:
            if label_prefix:
                label = label_prefix
            elif len(group) == len(valid_games):
                label = "Games"
            else:
                min_c = group[0].complexity or 0
                max_c = group[-1].complexity or 0
                label = _get_complexity_label(min_c, max_c)
            return [(label, group)]

        # Try to find a good split point
        split_idx = _find_best_split(group, min_group_size=2)

        if split_idx is not None:
            # Split the group
            left = group[:split_idx]
            right = group[split_idx:]

            # Recursively process each half
            left_results = process_group(left)
            right_results = process_group(right)
            return left_results + right_results
        else:
            # No valid split found, chunk by max_per_poll
            chunks = [
                group[i : i + max_per_poll]
                for i in range(0, len(group), max_per_poll)
            ]

            # Handle case where last chunk is a single game
            if len(chunks) > 1 and len(chunks[-1]) == 1:
                # Move the single game to the previous chunk
                chunks[-2].append(chunks[-1][0])
                chunks = chunks[:-1]

            results = []
            for idx, chunk in enumerate(chunks):
                min_c = chunk[0].complexity or 0
                max_c = chunk[-1].complexity or 0
                base_label = _get_complexity_label(min_c, max_c)

                if len(chunks) > 1:
                    label = f"{base_label} ({idx + 1}/{len(chunks)})"
                else:
                    label = base_label
                results.append((label, chunk))
            return results

    # Process rated games
    if rated_games:
        result.extend(process_group(rated_games))

    # Process unrated games separately
    if unrated_games:
        unrated_games.sort(key=lambda g: g.name.lower())
        chunks = [
            unrated_games[i : i + max_per_poll]
            for i in range(0, len(unrated_games), max_per_poll)
        ]

        # Handle single-game chunk at the end
        if len(chunks) > 1 and len(chunks[-1]) == 1:
            chunks[-2].append(chunks[-1][0])
            chunks = chunks[:-1]

        for idx, chunk in enumerate(chunks):
            if len(chunks) > 1:
                label = f"Unrated Games ({idx + 1}/{len(chunks)})"
            else:
                label = "Unrated Games"
            result.append((label, chunk))

    return result
