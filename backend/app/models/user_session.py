"""Ephemres Network Monitoring Platform: User Session Data Model.

One row per signed-in session, used by PostgresSessionStore. A session is valid while its row
exists, expires_at is in the future, the account's idle timeout has not passed since
last_activity_at, and (admin only) it is under 12 hours old: services/session_policy.py.
expires_at is the absolute end (12 hours for admin, a 7-day backstop for viewers). The per-user
session limit deletes the oldest rows.

EndedSession remembers why a session ended, for the 401 of that browser's next request.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4

from app.core.orm import DateTime, ForeignKey, Integer, String, func
from app.core.orm import UUID as PG_UUID
from app.core.orm import Mapped, mapped_column, relationship

from app.database import Base


class UserSession(Base):
    """Native Declarative ORM Model (app.core.orm.DeclarativeBase) representing
    active authenticated web sessions in the user_sessions table.
    """

    __tablename__ = "user_sessions"

    # Unique session record identifier (UUIDv4)
    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )

    # Reference to the authenticated user account
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Unique JWT ID (JTI) claim associated with this session token
    jti: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        unique=True,
        index=True,
    )

    # Session creation timestamp (UTC)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    # Last real interaction (clicks, keys, navigation via POST /auth/activity). Ordinary API
    # calls and the live-event stream do not move it, so a dashboard left polling still idles out.
    last_activity_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    # Where the session signed in from (migration 0022), shown in the Users tab's session list
    # so an admin can tell devices apart. Display only; empty for sessions older than v3.3.6.
    client_ip: Mapped[Optional[str]] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # Absolute expiration timestamp of this session (evaluated on request)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )

    # Relational link to the associated User account
    user: Mapped["User"] = relationship("User", lazy="joined")  # noqa: F821


class EndedSession(Base):
    """Why a session ended (migration 0022), kept for a few days so the browser that held it is
    told "signed in elsewhere" or "idle" rather than a bare 401. Never backed up or restored."""

    __tablename__ = "ended_sessions"

    # The ended session's token id; one row per session.
    jti: Mapped[str] = mapped_column(String(64), primary_key=True)

    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    # One of services/session_policy.END_REASONS.
    reason: Mapped[str] = mapped_column(String(32), nullable=False)

    # The idle timeout that ended it, for "signed out after N minutes without activity".
    idle_minutes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    ended_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )
