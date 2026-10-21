"""Ephemres Network Monitoring Platform — Runtime Settings Cache.

Admin-editable settings (Settings UI -> app_settings table) override their
config.toml counterparts at runtime. Every process keeps a cached snapshot:

* ephemres-api workers refresh at startup, after their own PATCH /settings,
  and on SYSTEM_SETTINGS_SYNC "RUNTIME_RELOAD" broadcasts from other workers.
* ephemres-engine and ephemres-flowd run in separate processes and refresh
  periodically via start_runtime_settings_refresh_task().
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional

from app.config import settings

logger = logging.getLogger(__name__)

TRUE_VALUES = ("true", "1", "yes")


@dataclass(frozen=True)
class RuntimeSettings:
    session_timeout_minutes: int
    lockout_threshold: int
    l2_auto_bypass: bool
    flow_sampling_multiplier: int
    flow_netflow_port: int
    flow_ipfix_port: int


def config_defaults() -> RuntimeSettings:
    """Values used when the corresponding app_settings row is absent or invalid."""
    flow_cfg = getattr(settings, "flow", None)
    return RuntimeSettings(
        session_timeout_minutes=int(getattr(settings.security, "session_timeout_minutes", 120)),
        lockout_threshold=5,
        l2_auto_bypass=True,
        flow_sampling_multiplier=int(getattr(flow_cfg, "sampling_multiplier", 1)),
        flow_netflow_port=int(getattr(flow_cfg, "netflow_port", 2055)),
        flow_ipfix_port=int(getattr(flow_cfg, "ipfix_port", 4739)),
    )


def _int_in_range(raw: Optional[str], default: int, lo: int, hi: Optional[int] = None) -> int:
    if raw is None:
        return default
    try:
        val = int(str(raw).strip())
    except ValueError:
        return default
    if val < lo or (hi is not None and val > hi):
        return default
    return val


def resolve(kv: Mapping[str, str]) -> RuntimeSettings:
    """Builds a RuntimeSettings snapshot from app_settings key/value rows over config defaults."""
    d = config_defaults()
    l2_raw = kv.get("l2_auto_bypass")
    return RuntimeSettings(
        session_timeout_minutes=_int_in_range(kv.get("session_timeout"), d.session_timeout_minutes, 1, 1440),
        lockout_threshold=_int_in_range(kv.get("lockout_threshold"), d.lockout_threshold, 1, 100),
        l2_auto_bypass=d.l2_auto_bypass if l2_raw is None else l2_raw.strip().lower() in TRUE_VALUES,
        flow_sampling_multiplier=_int_in_range(kv.get("flow_sampling_multiplier"), d.flow_sampling_multiplier, 1),
        flow_netflow_port=_int_in_range(kv.get("flow_netflow_port"), d.flow_netflow_port, 1, 65535),
        flow_ipfix_port=_int_in_range(kv.get("flow_ipfix_port"), d.flow_ipfix_port, 1, 65535),
    )


_current: Optional[RuntimeSettings] = None


def get_runtime_settings() -> RuntimeSettings:
    """Returns the cached snapshot (config defaults until the first refresh)."""
    return _current if _current is not None else config_defaults()


def set_runtime_settings(value: RuntimeSettings) -> None:
    global _current
    _current = value


async def refresh_runtime_settings(session_factory: Any) -> RuntimeSettings:
    """Reloads the snapshot from app_settings. On database errors the previous snapshot is kept."""
    from app.core.orm import select
    from app.models.system_setting import AppSetting

    try:
        async with session_factory() as db:
            res = await db.execute(select(AppSetting))
            kv: Dict[str, str] = {r.setting_key: r.setting_value for r in res.scalars().all()}
    except Exception as exc:
        logger.warning("RuntimeSettings: refresh failed, keeping previous values: %s", exc)
        return get_runtime_settings()

    snapshot = resolve(kv)
    if snapshot != _current:
        logger.info("RuntimeSettings: loaded %s", snapshot)
    set_runtime_settings(snapshot)
    return snapshot


async def start_runtime_settings_refresh_task(session_factory: Any, interval_seconds: float = 30.0) -> asyncio.Task:
    """Loads settings once, then keeps refreshing in the background (for non-API processes)."""
    await refresh_runtime_settings(session_factory)

    async def _loop() -> None:
        while True:
            await asyncio.sleep(interval_seconds)
            await refresh_runtime_settings(session_factory)

    return asyncio.create_task(_loop(), name="runtime_settings_refresh")
