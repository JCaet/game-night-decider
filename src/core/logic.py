from src.core.models import Game


def split_games(games: list[Game], max_per_poll: int = 10) -> list[tuple[str, list[Game]]]:
    """
    Split games into logical groups for polling.
    Sorts by complexity and labels by category.

    Returns:
        List of (Label, List[Game]) tuples.
    """
    if not games:
        return []

    # Filter invalid entries
    valid_games = [g for g in games if g.name]

    # Sort by complexity (ascending: light games first)
    valid_games.sort(key=lambda g: (g.complexity or 0, g.name.lower()))

    if len(valid_games) <= max_per_poll:
        return [("Games", valid_games)]

    # Split into chunks of max_per_poll
    chunks = []
    for i in range(0, len(valid_games), max_per_poll):
        chunk = valid_games[i : i + max_per_poll]
        chunks.append(chunk)

    # Label chunks based on complexity category
    def get_category_label(avg_complexity: float) -> str:
        if avg_complexity < 2.0:
            return "Light / Party Games"
        elif avg_complexity < 3.0:
            return "Medium Weight Games"
        else:
            return "Heavy Strategy Games"

    # First pass: assign labels
    labeled = []
    for chunk in chunks:
        avg_complexity = sum((g.complexity or 0) for g in chunk) / len(chunk) if chunk else 0
        label = get_category_label(avg_complexity)
        labeled.append((label, chunk))

    # Second pass: add part numbers for duplicate labels
    label_counts: dict[str, int] = {}
    for label, _ in labeled:
        label_counts[label] = label_counts.get(label, 0) + 1

    # Track which labels need part numbers
    label_indices: dict[str, int] = {}
    result = []
    for label, chunk in labeled:
        if label_counts[label] > 1:
            # Multiple chunks with same label - add part number
            idx = label_indices.get(label, 0) + 1
            label_indices[label] = idx
            final_label = f"{label} ({idx}/{label_counts[label]})"
        else:
            final_label = label
        result.append((final_label, chunk))

    return result
