import os
import sys
import asyncio
import logging
from typing import Optional, Any
from app.core.orm import text
from app.core.restore import default_reset_report_path, write_password_reset_report
from app.database import AsyncSessionLocal
from app.services.auth_service import hash_password, generate_readable_password

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("reset_legacy_passwords")


async def reset_legacy_passwords(session: Optional[Any] = None, report_path: Optional[str] = None) -> int:
    """Finds users with non-$scrypt$ password hashes, resets them to temporary passwords,
    sets must_change_password=TRUE, and records them to a secure report file (mode 0o600).
    Returns the count of users reset.
    """
    if report_path is None:
        report_path = default_reset_report_path()

    async def _execute_reset(db: Any) -> int:
        res = await db.execute(
            text("SELECT id, username FROM users WHERE password_hash NOT LIKE '$scrypt$%'")
        )
        rows = res.fetchall()
        if not rows:
            print("No legacy password hashes found.")
            return 0

        resets = []
        for row in rows:
            uid = getattr(row, "id", None)
            uname = getattr(row, "username", None)
            if uid is None and hasattr(row, "__getitem__"):
                uid = row["id"]
                uname = row["username"]

            temp_pwd = generate_readable_password()
            new_hash = hash_password(temp_pwd)

            await db.execute(
                text("""
                    UPDATE users
                    SET password_hash = :h,
                        must_change_password = TRUE,
                        updated_at = NOW()
                    WHERE id = CAST(:id AS uuid)
                """),
                {"h": new_hash, "id": str(uid)},
            )
            resets.append((uname, temp_pwd))

        await db.commit()

        written_path = write_password_reset_report(report_path, resets)

        count = len(resets)
        print(f"{count} users had legacy password hashes and were assigned temporary passwords. Report: {written_path}")
        return count

    if session is not None:
        return await _execute_reset(session)
    else:
        async with AsyncSessionLocal() as db:
            return await _execute_reset(db)


def main() -> None:
    try:
        asyncio.run(reset_legacy_passwords())
        sys.exit(0)
    except Exception as exc:
        logger.error("Failed to reset legacy passwords: %s", exc, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
