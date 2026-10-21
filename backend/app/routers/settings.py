import asyncio
import logging
import socket
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings as app_cfg
from app.database import get_db
from app.models.system_setting import AppSetting
from app.routers.auth import get_current_user, require_admin
from app.schemas import APIResponse
from app.schemas.bandwidth import FlowPreflightResponse
from app.services.driver_manager import driver_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/settings", tags=["settings"])


import os

async def manage_flow_service(enable: bool) -> tuple[bool, str]:
    """
    Attempts to start or stop the netmon-flowd systemd service.
    First tries passwordless sudo systemctl, falling back to direct systemctl.
    """
    if not os.path.exists("/etc/systemd/system/netmon-flowd.service"):
        logger.debug("netmon-flowd.service not installed; skipping service management")
        return False, "netmon-flowd.service not installed"

    action = "restart" if enable else "stop"
    commands = [
        ["sudo", "-n", "systemctl", action, "netmon-flowd"],
        ["systemctl", action, "netmon-flowd"],
    ]
    last_err = ""
    for cmd in commands:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode == 0:
                logger.info("Successfully executed %s for netmon-flowd via %s", action, cmd[0])
                if enable:
                    for en_cmd in [["sudo", "-n", "systemctl", "enable", "netmon-flowd"], ["systemctl", "enable", "netmon-flowd"]]:
                        try:
                            en_proc = await asyncio.create_subprocess_exec(*en_cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                            await en_proc.communicate()
                            if en_proc.returncode == 0:
                                break
                        except Exception:
                            pass
                else:
                    for dis_cmd in [["sudo", "-n", "systemctl", "disable", "netmon-flowd"], ["systemctl", "disable", "netmon-flowd"]]:
                        try:
                            dis_proc = await asyncio.create_subprocess_exec(*dis_cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                            await dis_proc.communicate()
                            if dis_proc.returncode == 0:
                                break
                        except Exception:
                            pass
                return True, f"Service netmon-flowd {action}ed successfully."
            else:
                last_err = stderr.decode().strip() or stdout.decode().strip()
        except Exception as exc:
            last_err = str(exc)

    logger.debug("Could not automatically %s netmon-flowd via systemctl: %s", action, last_err)
    return False, last_err


class SettingsUpdate(BaseModel):
    performance_mode: Optional[bool] = None
    l2_auto_bypass: Optional[bool] = None
    session_timeout: Optional[int] = Field(default=None, ge=1, le=1440)
    lockout_threshold: Optional[int] = Field(default=None, ge=1, le=100)
    alerting_enabled: Optional[bool] = None
    alertingEnabled: Optional[bool] = None
    flow_ingestion_enabled: Optional[bool] = None
    flowIngestionEnabled: Optional[bool] = None
    flow_netflow_port: Optional[int] = Field(default=None, ge=1, le=65535)
    flowNetflowPort: Optional[int] = Field(default=None, ge=1, le=65535)
    flow_ipfix_port: Optional[int] = Field(default=None, ge=1, le=65535)
    flowIpfixPort: Optional[int] = Field(default=None, ge=1, le=65535)
    flow_sampling_multiplier: Optional[int] = Field(default=None, ge=1)
    flowSamplingMultiplier: Optional[int] = Field(default=None, ge=1)

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
    )


class SettingsPayload(BaseModel):
    performance_mode: bool
    l2_auto_bypass: bool
    session_timeout: int
    lockout_threshold: int
    alerting_enabled: bool
    alertingEnabled: bool
    flow_ingestion_enabled: bool
    flowIngestionEnabled: bool
    flow_netflow_port: int
    flowNetflowPort: int
    flow_ipfix_port: int
    flowIpfixPort: int
    flow_sampling_multiplier: int
    flowSamplingMultiplier: int

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
    )


class OrganizationBranding(BaseModel):
    company_name: str = Field(default="", max_length=150)
    department: str = Field(default="", max_length=150)
    logo_data: str = Field(default="", description="Base64 data URL for company logo")
    report_footer: str = Field(default="", max_length=255)

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )


class OrganizationBrandingUpdate(BaseModel):
    company_name: Optional[str] = Field(default=None, max_length=150)
    department: Optional[str] = Field(default=None, max_length=150)
    logo_data: Optional[str] = Field(default=None)
    report_footer: Optional[str] = Field(default=None, max_length=255)

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )


async def _read_settings_dict(db: AsyncSession) -> dict[str, Any]:
    stmt = select(AppSetting)
    res = await db.execute(stmt)
    rows = res.scalars().all()
    kv = {r.setting_key: r.setting_value for r in rows}

    perf_mode = False
    if "performance_mode" in kv:
        perf_mode = kv["performance_mode"].strip().lower() in (
            "true",
            "1",
            "redis",
            "yes",
        )
    elif hasattr(app_cfg, "redis"):
        perf_mode = bool(getattr(app_cfg.redis, "performance_mode", False))

    l2_bypass = True
    if "l2_auto_bypass" in kv:
        l2_bypass = kv["l2_auto_bypass"].strip().lower() in ("true", "1", "yes")

    session_timeout = 120
    if "session_timeout" in kv:
        try:
            session_timeout = int(kv["session_timeout"])
        except ValueError:
            pass

    lockout_threshold = 5
    if "lockout_threshold" in kv:
        try:
            lockout_threshold = int(kv["lockout_threshold"])
        except ValueError:
            pass

    alerting_enabled = True
    if "alerting_enabled" in kv:
        alerting_enabled = kv["alerting_enabled"].strip().lower() in ("true", "1", "yes")

    flow_enabled = False
    if "flow_ingestion_enabled" in kv:
        flow_enabled = kv["flow_ingestion_enabled"].strip().lower() in ("true", "1", "yes")
    elif hasattr(app_cfg, "flow"):
        flow_enabled = bool(getattr(app_cfg.flow, "enabled", False))

    flow_nf_port = 2055
    if "flow_netflow_port" in kv:
        try:
            flow_nf_port = int(kv["flow_netflow_port"])
        except ValueError:
            pass
    elif hasattr(app_cfg, "flow"):
        flow_nf_port = int(getattr(app_cfg.flow, "netflow_port", 2055))

    flow_ipfix_port = 4739
    if "flow_ipfix_port" in kv:
        try:
            flow_ipfix_port = int(kv["flow_ipfix_port"])
        except ValueError:
            pass
    elif hasattr(app_cfg, "flow"):
        flow_ipfix_port = int(getattr(app_cfg.flow, "ipfix_port", 4739))

    flow_sampling = 1
    if "flow_sampling_multiplier" in kv:
        try:
            flow_sampling = int(kv["flow_sampling_multiplier"])
        except ValueError:
            pass
    elif hasattr(app_cfg, "flow"):
        flow_sampling = int(getattr(app_cfg.flow, "sampling_multiplier", 1))

    return {
        "performance_mode": perf_mode,
        "l2_auto_bypass": l2_bypass,
        "session_timeout": session_timeout,
        "lockout_threshold": lockout_threshold,
        "alerting_enabled": alerting_enabled,
        "alertingEnabled": alerting_enabled,
        "flow_ingestion_enabled": flow_enabled,
        "flowIngestionEnabled": flow_enabled,
        "flow_netflow_port": flow_nf_port,
        "flowNetflowPort": flow_nf_port,
        "flow_ipfix_port": flow_ipfix_port,
        "flowIpfixPort": flow_ipfix_port,
        "flow_sampling_multiplier": flow_sampling,
        "flowSamplingMultiplier": flow_sampling,
    }


async def _upsert_setting(db: AsyncSession, key: str, value: str) -> None:
    stmt = select(AppSetting).where(AppSetting.setting_key == key)
    res = await db.execute(stmt)
    setting = res.scalar_one_or_none()
    if setting is not None:
        setting.setting_value = value
    else:
        setting = AppSetting(setting_key=key, setting_value=value)
    db.add(setting)
    await db.flush()


@router.get("", response_model=APIResponse)
async def get_settings(
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    settings_data = await _read_settings_dict(db)
    return APIResponse.success(data=SettingsPayload(**settings_data))


@router.patch("", response_model=APIResponse)
async def update_settings(
    payload: SettingsUpdate,
    current_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    perf_mode_val = payload.performance_mode
    l2_bypass_val = payload.l2_auto_bypass
    session_timeout_val = payload.session_timeout
    lockout_val = payload.lockout_threshold
    alerting_val = payload.alerting_enabled

    flow_ingestion_val = payload.flow_ingestion_enabled
    if flow_ingestion_val is None and payload.flowIngestionEnabled is not None:
        flow_ingestion_val = payload.flowIngestionEnabled

    current_settings = await _read_settings_dict(db)

    # Calculate effective values
    if perf_mode_val is not None:
        effective_perf_mode = bool(perf_mode_val)
    else:
        effective_perf_mode = bool(current_settings.get("performance_mode", False))

    if flow_ingestion_val is not None:
        effective_flow_enabled = bool(flow_ingestion_val)
    else:
        effective_flow_enabled = bool(current_settings.get("flow_ingestion_enabled", False))

    current_flow_enabled = bool(current_settings.get("flow_ingestion_enabled", False))

    # Rule C (Prevent Orphaned Flow Ingestion)
    if current_flow_enabled and perf_mode_val is False and flow_ingestion_val is not False:
        raise HTTPException(
            status_code=400,
            detail="Cannot disable Redis while Network Flow Telemetry is enabled. Please disable Flow Ingestion first.",
        )

    # Rule A (Enforce Redis Memory Acceleration)
    if effective_flow_enabled and not effective_perf_mode:
        raise HTTPException(
            status_code=400,
            detail="Cannot enable Network Flow Telemetry: Redis Memory Acceleration must be activated first.",
        )

    # Rule B (Enforce Active Redis Service Connectivity & Stream Capability)
    if effective_flow_enabled:
        ready, err_msg = await check_redis_flow_prerequisites()
        if not ready:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot enable Network Flow Telemetry: Redis prerequisite check failed ({err_msg}). Please ensure the Redis 6.0+ service is running.",
            )

    reinit_driver = False

    if perf_mode_val is not None:
        await _upsert_setting(
            db, "performance_mode", "true" if perf_mode_val else "false"
        )
        reinit_driver = True

    if l2_bypass_val is not None:
        await _upsert_setting(
            db, "l2_auto_bypass", "true" if l2_bypass_val else "false"
        )

    if session_timeout_val is not None:
        await _upsert_setting(db, "session_timeout", str(session_timeout_val))

    if lockout_val is not None:
        await _upsert_setting(db, "lockout_threshold", str(lockout_val))

    if alerting_val is not None:
        await _upsert_setting(db, "alerting_enabled", "true" if alerting_val else "false")

    if flow_ingestion_val is not None:
        await _upsert_setting(
            db, "flow_ingestion_enabled", "true" if flow_ingestion_val else "false"
        )
        asyncio.create_task(manage_flow_service(enable=bool(flow_ingestion_val)))

    flow_nf_val = payload.flow_netflow_port
    if flow_nf_val is None and payload.flowNetflowPort is not None:
        flow_nf_val = payload.flowNetflowPort
    if flow_nf_val is not None:
        await _upsert_setting(db, "flow_netflow_port", str(flow_nf_val))

    flow_ipfix_val = payload.flow_ipfix_port
    if flow_ipfix_val is None and payload.flowIpfixPort is not None:
        flow_ipfix_val = payload.flowIpfixPort
    if flow_ipfix_val is not None:
        await _upsert_setting(db, "flow_ipfix_port", str(flow_ipfix_val))

    flow_sampling_val = payload.flow_sampling_multiplier
    if flow_sampling_val is None and payload.flowSamplingMultiplier is not None:
        flow_sampling_val = payload.flowSamplingMultiplier
    if flow_sampling_val is not None:
        await _upsert_setting(db, "flow_sampling_multiplier", str(flow_sampling_val))

    await db.commit()

    if reinit_driver:
        logger.info("performance_mode updated. Re-initializing storage driver manager...")
        try:
            await driver_manager.initialize()
            from app.services.settings_sync import broadcast_settings_reload

            await broadcast_settings_reload(action="DRIVER_RELOAD")
        except Exception as e:
            logger.error("Failed to reinitialize driver manager: %s", e)

    updated_data = await _read_settings_dict(db)
    return APIResponse.success(data=SettingsPayload(**updated_data))


@router.get("/organization", response_model=APIResponse)
async def get_organization_settings(
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Retrieves current organization branding settings (company name, department, logo, report footer).
    Available to all authenticated users for client-side rendering.
    """
    stmt = select(AppSetting).where(
        AppSetting.setting_key.in_([
            "org:company_name",
            "org:department",
            "org:logo_data",
            "org:report_footer",
        ])
    )
    res = await db.execute(stmt)
    kv = {s.setting_key: s.setting_value for s in res.scalars().all()}
    branding = OrganizationBranding(
        company_name=kv.get("org:company_name", ""),
        department=kv.get("org:department", ""),
        logo_data=kv.get("org:logo_data", ""),
        report_footer=kv.get("org:report_footer", ""),
    )
    return APIResponse.success(data=branding)


@router.patch("/organization", response_model=APIResponse)
async def update_organization_settings(
    payload: OrganizationBrandingUpdate,
    current_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """
    Admin-only endpoint to configure organization branding and corporate reporting headers.
    """
    if payload.logo_data is not None:
        logo_str = payload.logo_data.strip()
        if logo_str:
            if not logo_str.startswith("data:image/"):
                raise HTTPException(status_code=400, detail="Logo must be a valid image data URI (PNG, JPG, SVG).")
            if len(logo_str) > 2_000_000:
                raise HTTPException(status_code=400, detail="Logo image file exceeds maximum allowed size (1 MB).")
        await _upsert_setting(db, "org:logo_data", logo_str)

    if payload.company_name is not None:
        await _upsert_setting(db, "org:company_name", payload.company_name.strip())

    if payload.department is not None:
        await _upsert_setting(db, "org:department", payload.department.strip())

    if payload.report_footer is not None:
        await _upsert_setting(db, "org:report_footer", payload.report_footer.strip())

    await db.commit()

    stmt = select(AppSetting).where(
        AppSetting.setting_key.in_([
            "org:company_name",
            "org:department",
            "org:logo_data",
            "org:report_footer",
        ])
    )
    res = await db.execute(stmt)
    kv = {s.setting_key: s.setting_value for s in res.scalars().all()}
    branding = OrganizationBranding(
        company_name=kv.get("org:company_name", ""),
        department=kv.get("org:department", ""),
        logo_data=kv.get("org:logo_data", ""),
        report_footer=kv.get("org:report_footer", ""),
    )
    return APIResponse.success(data=branding)


async def _run_redis_flow_check() -> FlowPreflightResponse:

    # 1. Linux Kernel Socket Receive Buffer (rmem_max) check
    rmem_val = None
    rmem_ok = True
    try:
        if os.path.exists("/proc/sys/net/core/rmem_max"):
            with open("/proc/sys/net/core/rmem_max", "r") as f:
                rmem_val = int(f.read().strip())
                rmem_ok = rmem_val >= 2097152
    except Exception as e:
        logger.debug("Could not inspect /proc/sys/net/core/rmem_max: %s", e)

    # 2. UDP Ports bind availability check
    netflow_p = getattr(app_cfg.flow, "netflow_port", 2055)
    ipfix_p = getattr(app_cfg.flow, "ipfix_port", 4739)
    flow_bind = getattr(app_cfg.flow, "bind_host", "0.0.0.0")
    port_conflicts = await _udp_port_conflicts(flow_bind, (netflow_p, ipfix_p))
    udp_ports_ok = len(port_conflicts) == 0

    r_host = getattr(app_cfg.redis, "host", "127.0.0.1")
    r_port = getattr(app_cfg.redis, "port", 6379)
    r_db = getattr(app_cfg.redis, "db", 0)

    try:
        import redis.asyncio as aioredis

        client = aioredis.Redis(
            host=r_host,
            port=r_port,
            db=r_db,
            socket_connect_timeout=2.0,
            decode_responses=True,
        )
        await client.ping()
        info = await client.info("server")
        redis_version = info.get("redis_version", "0.0.0")

        major = 0
        try:
            major = int(redis_version.split(".")[0])
        except (ValueError, IndexError):
            pass

        version_supported = major >= 6

        stream_ok = False
        test_key = "stream:netflow:preflight_test"
        try:
            msg_id = await client.xadd(test_key, {"preflight": "ok"})
            if msg_id:
                stream_ok = True
                await client.xdel(test_key, msg_id)
        except Exception as str_err:
            logger.warning("Preflight stream test failed: %s", str_err)

        await client.aclose()

        ready = version_supported and stream_ok and udp_ports_ok
        diag_parts = []
        if ready:
            diag_parts.append(f"Redis v{redis_version} ready with Stream support.")
        elif not version_supported:
            diag_parts.append(f"Redis v{redis_version} detected, but Redis 6.0+ is required.")
        elif not stream_ok:
            diag_parts.append("Redis stream capabilities failed.")

        if not rmem_ok and rmem_val is not None:
            diag_parts.append(f"Kernel rmem_max ({rmem_val} B) is below recommended 2MB (2097152 B).")
        if not udp_ports_ok:
            diag_parts.append(f"UDP port conflict on port(s): {port_conflicts}.")

        msg = " ".join(diag_parts) if diag_parts else "Flow preflight passed successfully."

        return FlowPreflightResponse(
            redis_connected=True,
            redis_version=redis_version,
            redis_version_supported=version_supported,
            stream_write_success=stream_ok,
            rmem_max=rmem_val,
            rmem_max_supported=rmem_ok,
            udp_ports_available=udp_ports_ok,
            port_conflicts=port_conflicts,
            ready=ready,
            message=msg,
        )
    except Exception as exc:
        return FlowPreflightResponse(
            redis_connected=False,
            redis_version=None,
            redis_version_supported=False,
            stream_write_success=False,
            rmem_max=rmem_val,
            rmem_max_supported=rmem_ok,
            udp_ports_available=udp_ports_ok,
            port_conflicts=port_conflicts,
            ready=False,
            message=f"Redis connection failed: {exc}",
        )


async def _flow_daemon_active() -> bool:
    """True if netmon-flowd is active according to systemd."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "systemctl", "is-active", "--quiet", "netmon-flowd",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        return await proc.wait() == 0
    except Exception as exc:
        logger.debug("Could not query netmon-flowd state via systemctl: %s", exc)
        return False


async def _udp_port_conflicts(bind_host: str, ports: tuple[int, ...]) -> list[int]:
    """UDP ports the flow collector could not bind on `bind_host` right now.

    Probes the exact address the collector binds, without SO_REUSEADDR (which would let the probe
    bind alongside an existing listener and hide every conflict). Ports held while the flow daemon
    is active are its own, so they are not reported.
    """
    conflicts = []
    for port in ports:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.bind((bind_host, port))
        except OSError:
            conflicts.append(port)
        finally:
            sock.close()
    if conflicts and await _flow_daemon_active():
        return []
    return conflicts


async def check_redis_flow_prerequisites() -> tuple[bool, str]:
    """
    Validates Redis 6+ connectivity and stream write capability required for Flow Telemetry.
    Returns (ready: bool, message: str).
    """
    res = await _run_redis_flow_check()
    return res.ready, res.message


@router.post("/flow/preflight", response_model=APIResponse)
async def test_flow_preflight(
    current_user: dict = Depends(require_admin),
):
    """
    Validates Redis 6+ connectivity and stream write capability required for Flow Telemetry.
    """
    res = await _run_redis_flow_check()
    return APIResponse.success(data=res)
