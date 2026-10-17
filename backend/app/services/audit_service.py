from __future__ import annotations

import logging
from typing import Optional
from uuid import UUID
from app.core.orm import AsyncSession

from app.repositories.auth_repo import AuthRepository

logger = logging.getLogger(__name__)


async def log_action(
    db: AsyncSession,
    action: str,
    target_type: str,
    target_id: UUID,
    details: Optional[dict] = None,
    user_id: Optional[UUID] = None,
) -> None:
    auth_repo = AuthRepository(db)
    await auth_repo.create_audit_log(
        user_id=user_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        details=details,
    )
    logger.debug(
        "Audit: action=%s target_type=%s target_id=%s user_id=%s",
        action,
        target_type,
        str(target_id),
        str(user_id) if user_id else "SYSTEM",
    )
