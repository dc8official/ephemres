from __future__ import annotations

import asyncio
import json
import logging
from math import ceil
from typing import Any, Dict, List, Optional
from uuid import UUID

from app.core.asgi import APIRouter, Depends, HTTPException, Query, status
from app.core.orm import func, select
from app.core.orm import AsyncSession

from app.database import get_db
from app.models.alert_channel import AlertChannel
from app.models.alert_delivery_log import AlertDeliveryLog
from app.routers.auth import require_admin
from app.schemas import APIResponse, PaginationMeta
from app.schemas.alerts import (
    AlertChannelCreate,
    AlertChannelResponse,
    AlertChannelUpdate,
    AlertDeliveryLogResponse,
    AlertTestRequest,
)
from app.services.alert_dispatcher import alert_dispatcher
from app.core.crypto import decrypt_secret, encrypt_secret, mask_secret
from app.core.http_client import validate_outbound_url, validate_outbound_url_async

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/alerts", tags=["alerts"])


def _mask_channel_config(channel_type: str, config: Dict[str, Any]) -> Dict[str, Any]:
    masked = dict(config)
    if "webhook_url" in masked and masked["webhook_url"]:
        masked["webhook_url"] = mask_secret(str(masked["webhook_url"]))
    if "password" in masked and masked["password"]:
        masked["password"] = mask_secret(str(masked["password"]))
    if "headers" in masked and isinstance(masked["headers"], dict):
        masked_headers = {}
        for k, v in masked["headers"].items():
            if any(s in k.lower() for s in ("auth", "token", "key", "secret")):
                masked_headers[k] = mask_secret(str(v))
            else:
                masked_headers[k] = v
        masked["headers"] = masked_headers
    return masked


def _decrypt_config_dict(raw_config: str) -> Dict[str, Any]:
    try:
        decrypted = decrypt_secret(raw_config)
    except Exception:
        return {}
    if isinstance(decrypted, dict):
        return decrypted
    try:
        return json.loads(decrypted)
    except Exception:
        return {}


def _merge_channel_config(existing_cfg: Dict[str, Any], new_cfg: Dict[str, Any]) -> Dict[str, Any]:
    merged_cfg = dict(existing_cfg)
    for k, v in new_cfg.items():
        existing_val = merged_cfg.get(k)
        if k == "headers" and isinstance(v, dict):
            existing_headers = existing_val if isinstance(existing_val, dict) else {}
            merged_headers = dict(existing_headers)
            for hk, hv in v.items():
                if hv is None:
                    merged_headers.pop(hk, None)
                elif isinstance(hv, str) and ("••••••••" in hv or "••••••••••••" in hv or "•" in hv):
                    if hk in existing_headers:
                        merged_headers[hk] = existing_headers[hk]
                    else:
                        merged_headers[hk] = hv
                else:
                    merged_headers[hk] = hv
            merged_cfg[k] = merged_headers
            continue
        # If user sent masked bullets back, keep existing decrypted secret!
        if isinstance(v, str) and ("••••••••" in v or "••••••••••••" in v or "•" in v):
            if k in existing_cfg:
                continue
        merged_cfg[k] = v
    return merged_cfg


@router.get("/channels", response_model=APIResponse)
async def list_alert_channels(
    current_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(AlertChannel).order_by(AlertChannel.created_at.asc())
    res = await db.execute(stmt)
    channels = res.scalars().all()

    result = []
    for ch in channels:
        raw_cfg = _decrypt_config_dict(ch.config)
        masked_cfg = _mask_channel_config(ch.channel_type, raw_cfg)
        result.append(
            AlertChannelResponse(
                id=ch.id,
                name=ch.name,
                channel_type=ch.channel_type,
                is_enabled=ch.is_enabled,
                config=masked_cfg,
                endpoint_ids=ch.endpoint_ids or [],
                subnet_filters=ch.subnet_filters or [],
                severity_filters=ch.severity_filters or [],
                created_at=ch.created_at,
                updated_at=ch.updated_at,
            )
        )
    return APIResponse.success(data=result)


@router.post("/channels", response_model=APIResponse, status_code=status.HTTP_201_CREATED)
async def create_alert_channel(
    payload: AlertChannelCreate,
    current_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    ctype = payload.channel_type.upper()
    if ctype in ("TEAMS", "DISCORD", "SLACK", "GENERIC_WEBHOOK"):
        url = payload.config.get("webhook_url")
        if not url:
            raise HTTPException(status_code=400, detail="Webhook URL is required for webhook channels.")
        try:
            await validate_outbound_url_async(url)
        except ValueError as err:
            raise HTTPException(status_code=400, detail=str(err))
    elif ctype == "EMAIL_SMTP":
        smtp_host = payload.config.get("smtp_host")
        if not smtp_host:
            raise HTTPException(status_code=400, detail="smtp_host is required for EMAIL_SMTP channel.")
        try:
            await validate_outbound_url_async(f"http://{smtp_host}", allow_private=True, allow_loopback=True)
        except ValueError as err:
            raise HTTPException(status_code=400, detail=str(err))

    # Encrypt config payload
    encrypted_config = encrypt_secret(json.dumps(payload.config))

    channel = AlertChannel(
        name=payload.name,
        channel_type=ctype,
        is_enabled=payload.is_enabled,
        config=encrypted_config,
        endpoint_ids=[str(ep_id) for ep_id in payload.endpoint_ids],
        subnet_filters=payload.subnet_filters,
        severity_filters=payload.severity_filters,
    )
    db.add(channel)
    await db.commit()
    await db.refresh(channel)

    masked_cfg = _mask_channel_config(channel.channel_type, payload.config)
    resp = AlertChannelResponse(
        id=channel.id,
        name=channel.name,
        channel_type=channel.channel_type,
        is_enabled=channel.is_enabled,
        config=masked_cfg,
        endpoint_ids=channel.endpoint_ids or [],
        subnet_filters=channel.subnet_filters or [],
        severity_filters=channel.severity_filters or [],
        created_at=channel.created_at,
        updated_at=channel.updated_at,
    )
    return APIResponse.success(data=resp)


@router.get("/channels/{channel_id}", response_model=APIResponse)
async def get_alert_channel(
    channel_id: UUID,
    current_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(AlertChannel).where(AlertChannel.id == channel_id)
    res = await db.execute(stmt)
    ch = res.scalar_one_or_none()
    if not ch:
        raise HTTPException(status_code=404, detail="Alert channel not found.")

    raw_cfg = _decrypt_config_dict(ch.config)
    masked_cfg = _mask_channel_config(ch.channel_type, raw_cfg)
    resp = AlertChannelResponse(
        id=ch.id,
        name=ch.name,
        channel_type=ch.channel_type,
        is_enabled=ch.is_enabled,
        config=masked_cfg,
        endpoint_ids=ch.endpoint_ids or [],
        subnet_filters=ch.subnet_filters or [],
        severity_filters=ch.severity_filters or [],
        created_at=ch.created_at,
        updated_at=ch.updated_at,
    )
    return APIResponse.success(data=resp)


@router.put("/channels/{channel_id}", response_model=APIResponse)
async def update_alert_channel(
    channel_id: UUID,
    payload: AlertChannelUpdate,
    current_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(AlertChannel).where(AlertChannel.id == channel_id)
    res = await db.execute(stmt)
    ch = res.scalar_one_or_none()
    if not ch:
        raise HTTPException(status_code=404, detail="Alert channel not found.")

    if payload.name is not None:
        ch.name = payload.name
    if payload.channel_type is not None:
        ch.channel_type = payload.channel_type.upper()
    if payload.is_enabled is not None:
        ch.is_enabled = payload.is_enabled
    if payload.endpoint_ids is not None:
        ch.endpoint_ids = [str(ep) for ep in payload.endpoint_ids]
    if payload.subnet_filters is not None:
        ch.subnet_filters = payload.subnet_filters
    if payload.severity_filters is not None:
        ch.severity_filters = payload.severity_filters

    if payload.config is not None:
        existing_cfg = _decrypt_config_dict(ch.config)
        merged_cfg = _merge_channel_config(existing_cfg, payload.config)

        # Validate URL if webhook
        if ch.channel_type in ("TEAMS", "DISCORD", "SLACK", "GENERIC_WEBHOOK"):
            url = merged_cfg.get("webhook_url")
            if url:
                try:
                    await validate_outbound_url_async(url)
                except ValueError as err:
                    raise HTTPException(status_code=400, detail=str(err))
        elif ch.channel_type == "EMAIL_SMTP":
            smtp_host = merged_cfg.get("smtp_host")
            if smtp_host:
                try:
                    await validate_outbound_url_async(f"http://{smtp_host}", allow_private=True, allow_loopback=True)
                except ValueError as err:
                    raise HTTPException(status_code=400, detail=str(err))
            else:
                raise HTTPException(status_code=400, detail="smtp_host is required for EMAIL_SMTP channel.")

        ch.config = encrypt_secret(json.dumps(merged_cfg))

    await db.commit()
    await db.refresh(ch)

    active_cfg = _decrypt_config_dict(ch.config)
    masked_cfg = _mask_channel_config(ch.channel_type, active_cfg)
    resp = AlertChannelResponse(
        id=ch.id,
        name=ch.name,
        channel_type=ch.channel_type,
        is_enabled=ch.is_enabled,
        config=masked_cfg,
        endpoint_ids=ch.endpoint_ids or [],
        subnet_filters=ch.subnet_filters or [],
        severity_filters=ch.severity_filters or [],
        created_at=ch.created_at,
        updated_at=ch.updated_at,
    )
    return APIResponse.success(data=resp)


@router.delete("/channels/{channel_id}", response_model=APIResponse)
async def delete_alert_channel(
    channel_id: UUID,
    current_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(AlertChannel).where(AlertChannel.id == channel_id)
    res = await db.execute(stmt)
    ch = res.scalar_one_or_none()
    if not ch:
        raise HTTPException(status_code=404, detail="Alert channel not found.")

    await db.delete(ch)
    await db.commit()
    return APIResponse.success(data={"message": f"Alert channel '{ch.name}' deleted successfully."})


@router.post("/channels/test", response_model=APIResponse)
async def test_alert_channel(
    payload: AlertTestRequest,
    current_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    if payload.channel_id:
        stmt = select(AlertChannel).where(AlertChannel.id == payload.channel_id)
        res = await db.execute(stmt)
        ch = res.scalar_one_or_none()
        if not ch:
            raise HTTPException(status_code=404, detail="Alert channel not found.")

        cfg = _decrypt_config_dict(ch.config)
        if payload.config:
            cfg = _merge_channel_config(cfg, payload.config)

        if ch.channel_type in ("TEAMS", "DISCORD", "SLACK", "GENERIC_WEBHOOK"):
            url = cfg.get("webhook_url")
            if url:
                try:
                    await validate_outbound_url_async(url)
                except ValueError as err:
                    raise HTTPException(status_code=400, detail=str(err))
        elif ch.channel_type == "EMAIL_SMTP":
            smtp_host = cfg.get("smtp_host")
            if smtp_host:
                try:
                    await validate_outbound_url_async(f"http://{smtp_host}", allow_private=True, allow_loopback=True)
                except ValueError as err:
                    raise HTTPException(status_code=400, detail=str(err))
            else:
                raise HTTPException(status_code=400, detail="smtp_host is required for EMAIL_SMTP channel.")

        test_ch = {
            "id": ch.id,
            "name": ch.name,
            "channel_type": ch.channel_type,
            "config": cfg,
        }
        result = await alert_dispatcher.send_test_alert(test_ch)
    elif payload.channel_type and payload.config:
        test_ch = {
            "id": None,
            "name": payload.name or "Diagnostic Probe Channel",
            "channel_type": payload.channel_type.upper(),
            "config": payload.config,
        }
        if test_ch["channel_type"] in ("TEAMS", "DISCORD", "SLACK", "GENERIC_WEBHOOK"):
            url = test_ch["config"].get("webhook_url")
            if url:
                try:
                    await validate_outbound_url_async(url)
                except ValueError as err:
                    raise HTTPException(status_code=400, detail=str(err))
        elif test_ch["channel_type"] == "EMAIL_SMTP":
            smtp_host = test_ch["config"].get("smtp_host")
            if not smtp_host:
                raise HTTPException(status_code=400, detail="smtp_host is required for EMAIL_SMTP channel.")
            try:
                await validate_outbound_url_async(f"http://{smtp_host}", allow_private=True, allow_loopback=True)
            except ValueError as err:
                raise HTTPException(status_code=400, detail=str(err))
        result = await alert_dispatcher.send_test_alert(test_ch)
    else:
        raise HTTPException(status_code=400, detail="Must provide either channel_id or channel_type and config.")

    return APIResponse.success(data=result)


@router.get("/history", response_model=APIResponse)
@router.get("/logs", response_model=APIResponse, include_in_schema=False)
async def get_alert_history(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    current_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    count_stmt = select(func.count()).select_from(AlertDeliveryLog)
    total_res = await db.execute(count_stmt)
    total = total_res.scalar() or 0

    offset = (page - 1) * page_size
    stmt = (
        select(AlertDeliveryLog)
        .order_by(AlertDeliveryLog.timestamp.desc())
        .offset(offset)
        .limit(page_size)
    )
    res = await db.execute(stmt)
    logs = res.scalars().all()

    items = [
        AlertDeliveryLogResponse(
            id=log.id,
            channel_id=log.channel_id,
            channel_name=log.channel_name,
            endpoint_id=log.endpoint_id,
            endpoint_name=log.endpoint_name,
            event_type=log.event_type,
            status=log.status,
            status_code=log.status_code,
            retry_count=log.retry_count,
            response_message=log.response_message,
            delivered_at=log.timestamp,
        )
        for log in logs
    ]

    total_pages = ceil(total / page_size) if total > 0 else 1

    return APIResponse.success(
        data=items,
        meta=PaginationMeta(
            total=total,
            page=page,
            page_size=page_size,
            total_pages=total_pages,
        ),
    )
