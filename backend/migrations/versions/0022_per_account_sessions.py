"""Per-account idle timeout and session limit, session activity, and why sessions ended

Revision ID: 0022
Revises: 0021
Create Date: 2026-11-11 22:00:00.000000

Until now a session lasted a fixed time from sign-in (security.session_timeout_minutes or the
session_timeout setting), so a user could be signed out mid-click, and one config value limited
the sessions of every account. From v3.3.6 (services/session_policy.py):

- users.session_idle_minutes: minutes without interaction before a session ends. Admin
  accounts start at 30. Viewer accounts keep what the installation used before (the
  session_timeout setting, else the config file value, else 120), fitted to the viewer
  choices (30, 60, 120, 240), so nobody's sign-in gets shorter than it was by surprise.
- users.max_sessions: concurrent sessions per account, from security.max_active_sessions_per_user.
- user_sessions.last_activity_at: last real interaction (POST /auth/activity), not the last
  request: dashboard polling and the live-event stream must not keep a session alive.
- user_sessions.client_ip / user_agent: where the session signed in from, so the Users tab's
  session list can tell an admin which device is which before they end one. Sessions open
  during the upgrade have neither.
- ended_sessions: one short-lived row per session that ended, keyed by its token id, so the
  next request from that browser can be told why (signed in elsewhere, idle, ...) instead of a
  bare 401. Transient like user_sessions: never backed up or restored, rows pruned after 8 days.
"""
from typing import Sequence, Union
from app.core.migrator import op

revision: str = "0022"
down_revision: Union[str, None] = "0021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _config_values() -> tuple[int, int]:
    """The config file's session timeout and session limit, or the shipped defaults.

    upgrade() can't read the database (app/core/migrator.py), but it can read the config file;
    the app_settings override for the timeout is looked up in the SQL itself.
    """
    try:
        from app.config import settings

        timeout = int(getattr(settings.security, "session_timeout_minutes", 120))
        limit = int(getattr(settings.security, "max_active_sessions_per_user", 2))
    except Exception:
        timeout, limit = 120, 2
    return timeout, min(max(limit, 1), 10)


def upgrade() -> None:
    config_timeout, config_limit = _config_values()

    # 30 suits both roles (an admin choice and the shortest viewer choice), so a row inserted
    # without the column, by a restore of an older backup, is still valid; the restore then
    # sets the role's own default (core/restore.py).
    op.execute(
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS session_idle_minutes INTEGER NOT NULL DEFAULT 30"
    )
    op.execute(
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS max_sessions INTEGER NOT NULL DEFAULT 2"
    )

    # Viewers: the previous fixed timeout, fitted down to the nearest viewer choice (at least 30).
    op.execute(
        f"""
        UPDATE users SET session_idle_minutes = (
            SELECT CASE
                WHEN v >= 240 THEN 240
                WHEN v >= 120 THEN 120
                WHEN v >= 60 THEN 60
                ELSE 30
            END
            FROM (
                SELECT COALESCE(
                    (SELECT trim(setting_value)::integer FROM app_settings
                     WHERE setting_key = 'session_timeout'
                       AND trim(setting_value) ~ '^[0-9]{{1,5}}$'
                     LIMIT 1),
                    {int(config_timeout)}
                ) AS v
            ) AS previous
        )
        WHERE role_id IS NULL
           OR role_id NOT IN (SELECT id FROM roles WHERE role_name = 'ADMIN')
        """
    )
    op.execute(f"UPDATE users SET max_sessions = {int(config_limit)}")

    op.execute(
        "ALTER TABLE users ADD CONSTRAINT ck_users_session_idle_minutes "
        "CHECK (session_idle_minutes BETWEEN 15 AND 240)"
    )
    op.execute(
        "ALTER TABLE users ADD CONSTRAINT ck_users_max_sessions "
        "CHECK (max_sessions BETWEEN 1 AND 10)"
    )

    # Sessions open during the upgrade count as active now, so nobody is signed out by it.
    op.execute(
        "ALTER TABLE user_sessions ADD COLUMN IF NOT EXISTS last_activity_at "
        "TIMESTAMPTZ NOT NULL DEFAULT NOW()"
    )

    # Both short: the address as seen through trusted proxies, and the browser string cut to
    # 255 characters (only shown to admins, never used for decisions).
    op.execute("ALTER TABLE user_sessions ADD COLUMN IF NOT EXISTS client_ip VARCHAR(45)")
    op.execute("ALTER TABLE user_sessions ADD COLUMN IF NOT EXISTS user_agent VARCHAR(255)")

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ended_sessions (
            jti           VARCHAR(64) PRIMARY KEY,
            user_id       UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            reason        VARCHAR(32) NOT NULL,
            idle_minutes  INTEGER,
            ended_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_ended_sessions_ended_at ON ended_sessions (ended_at)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ended_sessions")
    op.execute("ALTER TABLE user_sessions DROP COLUMN IF EXISTS user_agent")
    op.execute("ALTER TABLE user_sessions DROP COLUMN IF EXISTS client_ip")
    op.execute("ALTER TABLE user_sessions DROP COLUMN IF EXISTS last_activity_at")
    op.execute("ALTER TABLE users DROP CONSTRAINT IF EXISTS ck_users_max_sessions")
    op.execute("ALTER TABLE users DROP CONSTRAINT IF EXISTS ck_users_session_idle_minutes")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS max_sessions")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS session_idle_minutes")
