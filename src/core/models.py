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
    playing_time: Mapped[int] = mapped_column(Integer)  # in minutes
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


class Collection(Base):
    __tablename__ = "collection"
    __table_args__ = (UniqueConstraint("user_id", "game_id", name="uq_user_game"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.telegram_id"))
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id"))

    is_excluded: Mapped[bool] = mapped_column(Boolean, default=False)
    is_new: Mapped[bool] = mapped_column(Boolean, default=False)  # "New" game flag
    is_priority: Mapped[bool] = mapped_column(Boolean, default=False)  # High priority flag

    # Relationships
    user: Mapped["User"] = relationship(back_populates="collection")
    game: Mapped["Game"] = relationship(lazy="joined")


class Session(Base):
    """Game Night Session (Lobby)"""

    __tablename__ = "sessions"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

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

    poll_id: Mapped[str] = mapped_column(
        ForeignKey("game_night_polls.poll_id"), primary_key=True
    )
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_name: Mapped[str | None] = mapped_column(String, nullable=True)

    # Relationships
    poll: Mapped["GameNightPoll"] = relationship(back_populates="votes")

