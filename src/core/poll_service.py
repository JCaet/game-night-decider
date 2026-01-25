"""
Poll Service - Centralized poll business logic.

This module encapsulates all poll-related operations, separating business
rules from Telegram-specific handling. This enables:
- Unit testing without Telegram mocks
- Cleaner handler code
- Reusable poll logic
"""

import random
from collections import namedtuple
from dataclasses import dataclass

from sqlalchemy import func, select

from src.core.logic import calculate_poll_winner, group_games_by_complexity
from src.core.models import (
    Collection,
    GameState,
    PollVote,
    Session,
    VoteLimit,
    VoteType,
)

# Named tuple for resolved votes (after category resolution)
ResolvedVote = namedtuple("ResolvedVote", ["game_id", "user_id"])


@dataclass
class VoteResult:
    """Result of a vote operation."""

    success: bool
    message: str
    is_removal: bool = False


class PollService:
    """
    Centralized service for poll operations.

    Encapsulates:
    - Vote casting with limit enforcement
    - Category vote handling
    - Winner calculation
    - Poll closing logic
    """

    @staticmethod
    def calculate_effective_limit(vote_limit: int, game_count: int) -> int | None:
        """
        Calculate the effective vote limit for a session.

        Args:
            vote_limit: The configured vote limit (VoteLimit constant or int)
            game_count: Number of games in the poll

        Returns:
            Effective limit as int, or None for unlimited
        """
        if vote_limit == VoteLimit.AUTO:
            # max(3, ceil(log2(game_count)))
            import math

            if game_count <= 0:
                return 3
            return max(3, math.ceil(math.log2(game_count)))
        elif vote_limit == VoteLimit.UNLIMITED:
            return None
        else:
            return vote_limit

    @staticmethod
    async def cast_vote(
        session,
        poll_id: str,
        user_id: int,
        target_id: int,
        vote_type: VoteType,
        user_name: str,
        vote_limit: int,
        game_count: int,
    ) -> VoteResult:
        """
        Cast or toggle a vote with limit enforcement.

        Args:
            session: SQLAlchemy async session
            poll_id: The poll ID
            user_id: User's Telegram ID
            target_id: Game ID (if GAME) or Complexity Level (if CATEGORY)
            vote_type: Type of vote (GAME or CATEGORY)
            user_name: User's display name
            vote_limit: Configured vote limit
            game_count: Total games in poll for auto-limit calculation

        Returns:
            VoteResult with success status and message
        """
        # Build query specific to vote type
        stmt = select(PollVote).where(
            PollVote.poll_id == poll_id,
            PollVote.user_id == user_id,
            PollVote.vote_type == vote_type,
        )

        if vote_type == VoteType.GAME:
            stmt = stmt.where(PollVote.game_id == target_id)
        else:
            stmt = stmt.where(PollVote.category_level == target_id)

        # Conditional locking only for Postgres (SQLite generic fallback)
        if hasattr(session.bind, "dialect") and "postgresql" in session.bind.dialect.name:
            stmt = stmt.with_for_update()

        existing_vote = (await session.execute(stmt)).scalar_one_or_none()

        if existing_vote:
            # Toggle off - remove the vote
            await session.delete(existing_vote)
            await session.commit()
            return VoteResult(success=True, message="Vote removed", is_removal=True)

        # Check vote limit before adding
        effective_limit = PollService.calculate_effective_limit(vote_limit, game_count)

        if effective_limit is not None:
            # Get user's current vote count for this poll
            # We count total votes (games + categories)
            user_votes_stmt = select(func.count(PollVote.id)).where(
                PollVote.poll_id == poll_id,
                PollVote.user_id == user_id,
            )
            user_vote_count = await session.scalar(user_votes_stmt) or 0

            if user_vote_count >= effective_limit:
                return VoteResult(
                    success=False,
                    message=(
                        f"Vote limit reached ({user_vote_count}/{effective_limit}). "
                        "Remove a vote first!"
                    ),
                )

        # Add new vote
        vote = PollVote(
            poll_id=poll_id,
            user_id=user_id,
            vote_type=vote_type,
            user_name=user_name,
        )

        if vote_type == VoteType.GAME:
            vote.game_id = target_id
        else:
            vote.category_level = target_id

        session.add(vote)
        await session.commit()

        # Generate appropriate message
        if vote_type == VoteType.CATEGORY:
            return VoteResult(success=True, message=f"🎲 Voted on Category {target_id}!")
        else:
            return VoteResult(success=True, message="Vote recorded")

    @staticmethod
    def resolve_category_votes(all_votes: list, valid_games: list) -> list[ResolvedVote]:
        """
        Convert category votes to actual game votes.

        Category votes are resolved to a random game from that
        complexity category. Regular game votes are passed through unchanged.

        Args:
            all_votes: List of PollVote records
            valid_games: List of Game objects in the poll

        Returns:
            List of ResolvedVote namedtuples with game_id and user_id
        """
        # Group games by complexity level
        groups = group_games_by_complexity(valid_games)

        # Resolve each category to one random game (shared across all voters)
        category_resolutions = {}  # level -> selected game

        for v in all_votes:
            if v.vote_type == VoteType.CATEGORY:
                level = v.category_level
                if level not in category_resolutions:
                    target_group = groups.get(level, [])
                    if target_group:
                        category_resolutions[level] = random.choice(target_group)

        # Build resolved votes list
        resolved_votes = []
        for v in all_votes:
            if v.vote_type == VoteType.CATEGORY:
                level = v.category_level
                if level in category_resolutions:
                    resolved_votes.append(
                        ResolvedVote(
                            game_id=category_resolutions[level].id,
                            user_id=v.user_id,
                        )
                    )
            elif v.vote_type == VoteType.GAME:
                resolved_votes.append(
                    ResolvedVote(game_id=v.game_id, user_id=v.user_id)
                )

        return resolved_votes

    @staticmethod
    async def get_votes_for_poll(session, poll_id: str) -> list:
        """Fetch all votes for a poll."""
        stmt = select(PollVote).where(PollVote.poll_id == poll_id)
        result = await session.execute(stmt)
        return result.scalars().all()

    @staticmethod
    async def build_star_collections(
        session, valid_games: list, priority_ids: set
    ) -> dict[int, list[int]]:
        """
        Build mapping of game_id -> list of user_ids who starred it.

        Used for weighted voting calculation.
        """
        star_collections = {}
        for g in valid_games:
            if g.id in priority_ids:
                stmt = select(Collection.user_id).where(
                    Collection.game_id == g.id,
                    Collection.state == GameState.STARRED,
                )
                starred_users = (await session.execute(stmt)).scalars().all()
                star_collections[g.id] = list(starred_users)
        return star_collections

    @staticmethod
    async def close_poll(
        session,
        poll_id: str,
        chat_id: int,
        valid_games: list,
        priority_ids: set,
    ) -> tuple[list[str], dict[str, float], list[str]]:
        """
        Close a poll and calculate the winner.

        Args:
            session: SQLAlchemy async session
            poll_id: Poll ID
            chat_id: Chat ID for session lookup
            valid_games: List of games in the poll
            priority_ids: Set of starred game IDs

        Returns:
            Tuple of (winner_names, scores_dict, modifiers_log)
        """
        # Fetch all votes
        all_votes = await PollService.get_votes_for_poll(session, poll_id)

        # Resolve category votes to actual games
        resolved_votes = PollService.resolve_category_votes(all_votes, valid_games)

        # Check if weighted voting is enabled
        session_obj = await session.get(Session, chat_id)
        is_weighted = session_obj.settings_weighted if session_obj else False

        # Build star collections for weighted voting
        star_collections = None
        if is_weighted:
            star_collections = await PollService.build_star_collections(
                session, valid_games, priority_ids
            )

        # Calculate winner
        winners, scores, modifiers_log = calculate_poll_winner(
            valid_games,
            resolved_votes,
            priority_ids,
            is_weighted,
            star_collections,
        )

        return winners, scores, modifiers_log  # type: ignore
