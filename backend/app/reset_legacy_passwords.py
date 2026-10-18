import os
import sys
import asyncio
import logging
from typing import Optional, Any
from app.core.orm import text
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
        report_path = os.environ.get("EPHEMRES_RESET_REPORT", "/opt/ephemres/legacy_password_resets.txt")

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

        dir_path = os.path.dirname(report_path)
        if dir_path:
            os.makedirs(dir_path, exist_ok=True)

        fd = os.open(report_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.chmod(report_path, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                for username, temp_password in resets:
                    f.write(f"{username}\t{temp_password}\n")
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            raise

        count = len(resets)
        print(f"{count} users had legacy password hashes and were assigned temporary passwords. Report: {report_path}")
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
