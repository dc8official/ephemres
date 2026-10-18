import os
import sys
import asyncio
import logging
from typing import Optional
from app.core.orm import text
from app.database import AsyncSessionLocal
from app.services.auth_service import hash_password, generate_readable_password

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("seed_admin")

async def seed_admin(password: Optional[str] = None):
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

if __name__ == "__main__":
    target_pass = os.environ.get("DEFAULT_ADMIN_PASSWORD") or os.environ.get("ADMIN_PASSWORD")
    success = asyncio.run(seed_admin(target_pass))
    if not success:
        sys.exit(1)
