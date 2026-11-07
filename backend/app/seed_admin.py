import os
import sys
import asyncio
import logging
from typing import Optional
from app.core.orm import text
from app.database import AsyncSessionLocal
from app.services.auth_service import hash_password, generate_readable_password, verify_password

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("seed_admin")

async def seed_admin(password: Optional[str] = None, if_missing: bool = False):
    """Creates the 'admin' account, or resets it to `password` (DEFAULT_ADMIN_PASSWORD).

    if_missing: only create a missing admin and never touch an existing one. Upgrades use it:
    the install-time default must not replace an admin password, and a legacy admin hash is
    replaced by app.reset_legacy_passwords with a random temporary password instead.
    """
    if not password:
        password = os.environ.get("DEFAULT_ADMIN_PASSWORD") or os.environ.get("ADMIN_PASSWORD")

    def _get_password_and_hash() -> str:
        nonlocal password
        if not password:
            password = generate_readable_password()
            print(f"[seed] Initial admin password generated: {password}")
        return hash_password(password)

    async with AsyncSessionLocal() as db:
        role_res = await db.execute(
            text("SELECT id FROM roles WHERE role_name = 'ADMIN' LIMIT 1")
        )
        role_row = role_res.fetchone()
        if not role_row:
            logger.error("Role 'ADMIN' does not exist in database. Ensure native database migrations (app.core.migrator) have run.")
            return False

        role_id = role_row.id

        existing = await db.execute(
            text("SELECT id, password_hash, must_change_password FROM users WHERE username = 'admin' LIMIT 1")
        )
        existing_admin = existing.fetchone()

        force_reset = os.environ.get("FORCE_RESET_ADMIN", "").lower() in ("true", "1")

        if existing_admin is not None and if_missing:
            print("[seed] Admin user 'admin' already exists — skipping.")
            return True

        if existing_admin is not None:
            pwd_hash = getattr(existing_admin, "password_hash", None)
            if pwd_hash is None and hasattr(existing_admin, "__getitem__"):
                pwd_hash = existing_admin["password_hash"]

            if not str(pwd_hash or "").startswith("$scrypt$"):
                await db.execute(
                    text("""
                        UPDATE users
                        SET password_hash = :p,
                            must_change_password = TRUE,
                            is_active = TRUE,
                            updated_at = NOW()
                        WHERE username = 'admin'
                    """),
                    {"p": _get_password_and_hash()}
                )
                await db.commit()
                print(
                    "[seed] Upgraded legacy password hash for admin 'admin' "
                    "to $scrypt$ (must_change_password=True)."
                )
                return True
            else:
                mcp = getattr(existing_admin, "must_change_password", False)
                if mcp is None and hasattr(existing_admin, "__getitem__"):
                    mcp = existing_admin["must_change_password"]

                if force_reset or mcp:
                    await db.execute(
                        text("""
                            UPDATE users
                            SET password_hash = :p,
                                must_change_password = TRUE,
                                is_active = TRUE,
                                updated_at = NOW()
                            WHERE username = 'admin'
                        """),
                        {"p": _get_password_and_hash()}
                    )
                    await db.commit()
                    logger.info("Successfully updated initial admin user account (must_change_password=TRUE).")
                else:
                    print("[seed] Admin user 'admin' already exists — skipping.")
                return True
        else:
            await db.execute(
                text("""
                    INSERT INTO users (
                        username, password_hash, role_id, is_active, must_change_password
                    ) VALUES (
                        'admin', :p, CAST(:r AS uuid), TRUE, TRUE
                    )
                """),
                {"p": _get_password_and_hash(), "r": str(role_id)}
            )
            logger.info("Successfully created default admin user account (username: admin).")

        await db.commit()
        return True

DEFAULT_ADMIN_RESET_REPORT = "/opt/ephemres/admin_password_reset.txt"


async def rotate_default_admin_password(candidates, report_path: Optional[str] = None) -> bool:
    """Gives 'admin' a random temporary password if it still signs in with an installer default.

    Earlier installers seeded admin/admin. The API now refuses that account until its
    password is changed, but anyone who reaches the sign-in page first could still change it
    and keep the server. Upgrades call this to close that window on servers where nobody ever
    changed it. The new password goes to a root-only report (never to stdout or logs).
    Returns True when the password was rotated.
    """
    from app.core.restore import write_password_reset_report

    candidates = [c for c in dict.fromkeys(candidates) if c]
    async with AsyncSessionLocal() as db:
        res = await db.execute(
            text("SELECT id, password_hash FROM users WHERE username = 'admin' LIMIT 1")
        )
        row = res.fetchone()
        if row is None:
            return False
        current_hash = str(getattr(row, "password_hash", "") or "")
        if not current_hash.startswith("$scrypt$"):
            return False  # legacy hashes are handled by app.reset_legacy_passwords
        if not any(verify_password(c, current_hash) for c in candidates):
            print("[seed] Admin password is not an installer default — unchanged.")
            return False

        temp_password = generate_readable_password()
        await db.execute(
            text("""
                UPDATE users
                SET password_hash = :p,
                    must_change_password = TRUE,
                    updated_at = NOW()
                WHERE username = 'admin'
            """),
            {"p": hash_password(temp_password)},
        )
        await db.commit()

    path = write_password_reset_report(
        report_path or os.environ.get("EPHEMRES_ADMIN_RESET_REPORT", DEFAULT_ADMIN_RESET_REPORT),
        [("admin", temp_password)],
    )
    # Its sessions were made with the default password: sign them all out.
    async with AsyncSessionLocal() as db:
        await db.execute(
            text("DELETE FROM user_sessions WHERE user_id = (SELECT id FROM users WHERE username = 'admin')")
        )
        await db.commit()
    print(f"[seed] Admin still had an installer default password; a random one was written to {path}")
    return True


if __name__ == "__main__":
    if "--rotate-default" in sys.argv[1:]:
        candidates = ["admin", os.environ.get("DEFAULT_ADMIN_PASSWORD", "")]
        asyncio.run(rotate_default_admin_password(candidates))
        sys.exit(0)
    target_pass = os.environ.get("DEFAULT_ADMIN_PASSWORD") or os.environ.get("ADMIN_PASSWORD")
    success = asyncio.run(seed_admin(target_pass, if_missing="--if-missing" in sys.argv[1:]))
    if not success:
        sys.exit(1)
