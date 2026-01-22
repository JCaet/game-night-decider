from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# ---------------------------------------------------------------------------- #
# SQLAlchemy Models
# ---------------------------------------------------------------------------- #


class Base(AsyncAttrs, DeclarativeBase):
    pass


class Game(Base):
    __tablename__ = "games"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)  # BGG API ID
    name: Mapped[str] = mapped_column(String, index=True)
    min_players: Mapped[int] = mapped_column(Integer)
    max_players: Mapped[int] = mapped_column(Integer)
    playing_time: Mapped[int] = mapped_column(Integer)  # in minutes (typical)
    min_playing_time: Mapped[int | None] = mapped_column(Integer, nullable=True)  # in minutes
    max_playing_time: Mapped[int | None] = mapped_column(Integer, nullable=True)  # in minutes
    complexity: Mapped[float] = mapped_column(Float)  # BGG Weight (1-5)
    thumbnail: Mapped[str | None] = mapped_column(String, nullable=True)

    # Relationships
    # user_collections: Mapped[List["Collection"]] = relationship(back_populates="game")


class User(Base):
    __tablename__ = "users"

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    telegram_name: Mapped[str | None] = mapped_column(
        String, nullable=True
    )  # Display name from Telegram
    bgg_username: Mapped[str | None] = mapped_column(String, unique=True, nullable=True)

    # Guest fields
    is_guest: Mapped[bool] = mapped_column(Boolean, default=False)
    added_by_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # Relationships
    collection: Mapped[list["Collection"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class GameState:
    """Game collection states for the 3-state cycle."""

    INCLUDED = 0  # ⬜ Normal game, available for polls
    STARRED = 1  # 🌟 Priority game, gets boost in weighted voting
    EXCLUDED = 2  # ❌ Excluded from polls


class PollType:
    """Poll types for the session."""

    CUSTOM = 0  # Single interactive message with buttons
    NATIVE = 1  # Standard Telegram polls (split if >10 games)


class VoteLimit:
    """Vote limit options for polls."""

    AUTO = -1  # Dynamic: max(3, ceil(log2(game_count)))
    UNLIMITED = 0  # Current behavior: one vote per game
    # Static values (3, 5, 7, 10) are stored directly as integers


class Collection(Base):
    __tablename__ = "collection"
    __table_args__ = (UniqueConstraint("user_id", "game_id", name="uq_user_game"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.telegram_id"))
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id"))

    # Game state: 0=included, 1=starred, 2=excluded
    state: Mapped[int] = mapped_column(Integer, default=GameState.INCLUDED)

    # Effective values (base game + owned expansions)
    # Null means use base game values (no expansion modifiers)
    effective_max_players: Mapped[int | None] = mapped_column(Integer, nullable=True)
    effective_complexity: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Relationships
    user: Mapped["User"] = relationship(back_populates="collection")
    game: Mapped["Game"] = relationship(lazy="joined")


class Session(Base):
    """Game Night Session (Lobby)"""

    __tablename__ = "sessions"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    settings_weighted: Mapped[bool] = mapped_column(Boolean, default=True)
    poll_type: Mapped[int] = mapped_column(Integer, default=PollType.CUSTOM)
    message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    hide_voters: Mapped[bool] = mapped_column(Boolean, default=False)
    vote_limit: Mapped[int] = mapped_column(Integer, default=-1)  # VoteLimit.AUTO

    # Relationships
    players: Mapped[list["SessionPlayer"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )


class SessionPlayer(Base):
    """Users joined in a specific session"""

    __tablename__ = "session_players"
    __table_args__ = (UniqueConstraint("session_id", "user_id", name="uq_session_user"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.chat_id"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.telegram_id"))

    # Relationships
    session: Mapped["Session"] = relationship(back_populates="players")
    user: Mapped["User"] = relationship(lazy="joined")


class GameNightPoll(Base):
    """Track active polls for a session"""

    __tablename__ = "game_night_polls"

    poll_id: Mapped[str] = mapped_column(String, primary_key=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey("sessions.chat_id"))
    message_id: Mapped[int] = mapped_column(Integer)

    # Relationships
    votes: Mapped[list["PollVote"]] = relationship(
        back_populates="poll", cascade="all, delete-orphan"
    )


class PollVote(Base):
    """Track who voted in a poll"""

    __tablename__ = "poll_votes"

    poll_id: Mapped[str] = mapped_column(ForeignKey("game_night_polls.poll_id"), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # game_id is nullable: NULL for native polls, set for custom polls
    game_id: Mapped[int | None] = mapped_column(
        BigInteger, primary_key=True, nullable=True, default=None
    )
    user_name: Mapped[str | None] = mapped_column(String, nullable=True)

    # Relationships
    poll: Mapped["GameNightPoll"] = relationship(back_populates="votes")


class Expansion(Base):
    """Track expansions and their modifiers to base games."""

    __tablename__ = "expansions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)  # BGG expansion ID
    name: Mapped[str] = mapped_column(String, index=True)
    base_game_id: Mapped[int] = mapped_column(ForeignKey("games.id"), index=True)

    # Player count modifier (new max when expansion is used)
    new_max_players: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Complexity modifier (delta to add to base complexity)
    complexity_delta: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Relationships
    base_game: Mapped["Game"] = relationship()


class UserExpansion(Base):
    """Track user ownership of expansions."""

    __tablename__ = "user_expansions"
    __table_args__ = (UniqueConstraint("user_id", "expansion_id", name="uq_user_expansion"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.telegram_id"))
    expansion_id: Mapped[int] = mapped_column(ForeignKey("expansions.id"))

    # Relationships
    user: Mapped["User"] = relationship()
    expansion: Mapped["Expansion"] = relationship()
