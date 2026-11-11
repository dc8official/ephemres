"""Session rules per account: idle timeout choices, the admin absolute cap, the session limit,
and the reasons a session can end.

A session ends after a period without real interaction (clicks, key presses, navigation; see
POST /auth/activity), the period being set per account. Admin accounts also end 12 hours after
sign-in whatever the activity, because an admin session left open on a shared screen is the
most damaging one to lose. Viewer sessions have no fixed cap, only a long backstop so rows of
abandoned browsers are still cleaned up.

The choices are fixed lists rather than free numbers so the Users form, the API and the
migration backfill all agree on what a valid value is (decided in the v3.3.6 build plan).
"""
from __future__ import annotations

from datetime import timedelta
from typing import Tuple

ADMIN = "ADMIN"
VIEWER = "VIEWER"

# Minutes without interaction before a session ends.
ADMIN_IDLE_CHOICES: Tuple[int, ...] = (15, 30, 60)
VIEWER_IDLE_CHOICES: Tuple[int, ...] = (30, 60, 120, 240)
ADMIN_IDLE_DEFAULT = 30
VIEWER_IDLE_DEFAULT = 120

# Admin: ends this long after sign-in even with activity. Viewer: no cap, only a backstop
# for cleaning up rows (an idle viewer session is dead long before this).
ADMIN_ABSOLUTE_LIFETIME = timedelta(hours=12)
VIEWER_BACKSTOP_LIFETIME = timedelta(days=7)

# The token and cookie outlive the session row by this much, so a request that arrives just
# after the absolute end still reaches the server, which can then say why the session ended
# (absolute_timeout). Without it the browser drops the cookie first and the user only sees
# the sign-in page. The session row, not the token, decides whether a request is accepted.
TOKEN_GRACE = timedelta(hours=1)

# Concurrent sessions per account; same bounds as security.max_active_sessions_per_user.
MAX_SESSIONS_MIN = 1
MAX_SESSIONS_MAX = 10

# Why a session ended, sent to the browser in the X-Session-End-Reason header of a 401.
IDLE_TIMEOUT = "idle_timeout"
ABSOLUTE_TIMEOUT = "absolute_timeout"
SIGNED_IN_ELSEWHERE = "signed_in_elsewhere"
SIGNED_OUT = "signed_out"
PASSWORD_CHANGED = "password_changed"
ENDED_BY_ADMIN = "ended_by_admin"
END_REASONS = (
    IDLE_TIMEOUT,
    ABSOLUTE_TIMEOUT,
    SIGNED_IN_ELSEWHERE,
    SIGNED_OUT,
    PASSWORD_CHANGED,
    ENDED_BY_ADMIN,
)

# An ended_sessions row only has to outlive the token it explains (backstop plus grace).
ENDED_SESSION_RETENTION = VIEWER_BACKSTOP_LIFETIME + timedelta(days=1)


def _is_admin(role_name: object) -> bool:
    return str(role_name or "").upper() == ADMIN


def idle_choices(role_name: object) -> Tuple[int, ...]:
    return ADMIN_IDLE_CHOICES if _is_admin(role_name) else VIEWER_IDLE_CHOICES


def default_idle_minutes(role_name: object) -> int:
    return ADMIN_IDLE_DEFAULT if _is_admin(role_name) else VIEWER_IDLE_DEFAULT


def clamp_idle_minutes(role_name: object, minutes: object) -> int:
    """The largest allowed choice not above `minutes`, or the smallest choice.

    Used when a role changes (an admin's 15 minutes becomes a viewer's 30; a viewer's 4 hours
    becomes an admin's 1 hour) and for the migration and restore backfills, so a value that
    was valid before never leaves an account with a timeout its role does not offer.
    """
    choices = idle_choices(role_name)
    try:
        value = int(minutes)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default_idle_minutes(role_name)
    fitting = [c for c in choices if c <= value]
    return max(fitting) if fitting else min(choices)


def session_lifetime(role_name: object) -> timedelta:
    """How long the session row lives at most: the admin cap, or the viewer backstop."""
    return ADMIN_ABSOLUTE_LIFETIME if _is_admin(role_name) else VIEWER_BACKSTOP_LIFETIME


def token_lifetime(role_name: object) -> timedelta:
    """JWT exp and cookie max_age: the session's absolute end plus the grace (see TOKEN_GRACE)."""
    return session_lifetime(role_name) + TOKEN_GRACE


def default_max_sessions() -> int:
    """New accounts start with security.max_active_sessions_per_user from the config file."""
    try:
        from app.config import settings

        value = int(getattr(settings.security, "max_active_sessions_per_user", 2))
    except Exception:
        value = 2
    return min(max(value, MAX_SESSIONS_MIN), MAX_SESSIONS_MAX)
