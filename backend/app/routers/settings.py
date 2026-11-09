"""Settings page routes: system settings, organisation branding, and the flow-port preflight.

Saved settings go to the app_settings table and override config.toml at run time
(services/runtime_settings); other processes are told to reload (services/settings_sync).
Turning flow ingestion on or off starts or stops ephemres-flowd through systemctl, and turning
performance mode on or off moves the event broker between FastStore and PostgreSQL. Requests
may use snake_case or camelCase field names.
"""
import asyncio
import base64
import binascii
import logging
import socket
from typing import Any, Optional

from app.core.asgi import APIRouter, Depends, HTTPException
from app.core.background import spawn
from app.core.schema import BaseModel, ConfigDict, Field
from app.core.schema import to_camel
from app.core.orm import select
from app.core.orm import AsyncSession

from app.config import settings as app_cfg
from app.database import AsyncSessionLocal, get_db
from app.models.system_setting import AppSetting
from app.routers.auth import get_current_user, require_admin
from app.schemas import APIResponse
from app.schemas.bandwidth import FlowPreflightResponse
from app.services.driver_manager import driver_manager
from app.services.runtime_settings import get_runtime_settings, refresh_runtime_settings, resolve as resolve_runtime_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/settings", tags=["settings"])


import os

async def manage_flow_service(enable: bool) -> tuple[bool, str]:
    """
    Attempts to start or stop the ephemres-flowd (or legacy netmon-flowd) systemd service.
    First tries passwordless sudo systemctl, falling back to direct systemctl.

    Starting also enables the unit (so it comes back after a reboot), stopping disables it.
    A failure is reported, not raised: the setting is saved either way.
    """
    service_name = "ephemres-flowd"
    if not os.path.exists(f"/etc/systemd/system/{service_name}.service"):
        if os.path.exists("/etc/systemd/system/netmon-flowd.service"):
            service_name = "netmon-flowd"
        else:
            logger.debug("%s.service not installed; skipping service management", service_name)
            return False, f"{service_name}.service not installed"

    action = "restart" if enable else "stop"
    commands = [
        ["sudo", "-n", "systemctl", action, service_name],
        ["systemctl", action, service_name],
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
                logger.info("Successfully executed %s for %s via %s", action, service_name, cmd[0])
                if enable:
                    for en_cmd in [["sudo", "-n", "systemctl", "enable", service_name], ["systemctl", "enable", service_name]]:
                        try:
                            en_proc = await asyncio.create_subprocess_exec(*en_cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                            await en_proc.communicate()
                            if en_proc.returncode == 0:
                                break
                        except Exception:
                            pass
                else:
                    for dis_cmd in [["sudo", "-n", "systemctl", "disable", service_name], ["systemctl", "disable", service_name]]:
                        try:
                            dis_proc = await asyncio.create_subprocess_exec(*dis_cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                            await dis_proc.communicate()
                            if dis_proc.returncode == 0:
                                break
                        except Exception:
                            pass
                return True, f"Service {service_name} {action}ed successfully."
            else:
                last_err = stderr.decode().strip() or stdout.decode().strip()
        except Exception as exc:
            last_err = str(exc)

    logger.debug("Could not automatically %s %s via systemctl: %s", action, service_name, last_err)
    return False, last_err


async def _flow_daemon_active() -> bool:
    """True if ephemres-flowd (or legacy netmon-flowd) is active according to systemd."""
    for service_name in ("ephemres-flowd", "netmon-flowd"):
        try:
            proc = await asyncio.create_subprocess_exec(
                "systemctl", "is-active", "--quiet", service_name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            if await proc.wait() == 0:
                return True
        except Exception as exc:
            logger.debug("Could not query %s state via systemctl: %s", service_name, exc)
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

    rt = resolve_runtime_settings(kv)
    l2_bypass = rt.l2_auto_bypass
    session_timeout = rt.session_timeout_minutes
    lockout_threshold = rt.lockout_threshold

    alerting_enabled = True
    if "alerting_enabled" in kv:
        alerting_enabled = kv["alerting_enabled"].strip().lower() in ("true", "1", "yes")

    flow_enabled = False
    if "flow_ingestion_enabled" in kv:
        flow_enabled = kv["flow_ingestion_enabled"].strip().lower() in ("true", "1", "yes")
    elif hasattr(app_cfg, "flow"):
        flow_enabled = bool(getattr(app_cfg.flow, "enabled", False))

    flow_nf_port = rt.flow_netflow_port
    flow_ipfix_port = rt.flow_ipfix_port
    flow_sampling = rt.flow_sampling_multiplier

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


LOGO_MAGIC = {
    "image/png": b"\x89PNG\r\n\x1a\n",
    "image/jpeg": b"\xff\xd8",
    "image/jpg": b"\xff\xd8",
}


def _is_embeddable_logo(data_uri: str) -> bool:
    """Accepts only base64 PNG/JPEG data URIs whose bytes match the declared type (see app.core.pdf)."""
    header, sep, payload = data_uri.partition(",")
    if not sep or not header.startswith("data:") or not header.endswith(";base64"):
        return False
    magic = LOGO_MAGIC.get(header[len("data:"):-len(";base64")].lower())
    if magic is None:
        return False
    try:
        head = base64.b64decode(payload[:16], validate=True)
    except (binascii.Error, ValueError):
        return False
    return head.startswith(magic)


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

    current_flow_enabled = bool(current_settings.get("flow_ingestion_enabled", False))

    # Turning flow ingestion on runs the same preflight as the Flow tab: FastStore opens (flowd
    # keeps its exporter banner and interface lists there) and nothing else holds the UDP ports.
    # It does not depend on Performance Mode, which only moves the API's event broker to
    # FastStore: flows go from the collector to the aggregator in memory. Requiring it, and a
    # FastStore stream write, was left over from when flowd buffered flows in a stream.
    if flow_ingestion_val and not current_flow_enabled:
        ready, err_msg = await check_flow_prerequisites(
            payload.flow_netflow_port or payload.flowNetflowPort,
            payload.flow_ipfix_port or payload.flowIpfixPort,
        )
        if not ready:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot enable Network Flow Telemetry: preflight check failed ({err_msg})",
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
        spawn(manage_flow_service(enable=bool(flow_ingestion_val)), "manage_flow_service")

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

    # Apply admin-editable runtime settings in this worker now and notify the other workers.
    await refresh_runtime_settings(AsyncSessionLocal)
    from app.services.settings_sync import broadcast_settings_reload

    await broadcast_settings_reload(action="RUNTIME_RELOAD")

    # ephemres-flowd binds its UDP ports at startup, so a port change needs a restart.
    ports_changed = (
        (flow_nf_val is not None and int(flow_nf_val) != current_settings.get("flow_netflow_port"))
        or (flow_ipfix_val is not None and int(flow_ipfix_val) != current_settings.get("flow_ipfix_port"))
    )
    if ports_changed and flow_ingestion_val is None and current_flow_enabled:
        spawn(manage_flow_service(enable=True), "manage_flow_service")

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
            if len(logo_str) > 2_000_000:
                raise HTTPException(status_code=400, detail="Logo image file exceeds maximum allowed size (1 MB).")
            if not _is_embeddable_logo(logo_str):
                raise HTTPException(
                    status_code=400,
                    detail="Logo must be a base64 PNG or JPEG data URI (PDF reports can only embed PNG and JPEG).",
                )
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


async def _run_flow_check(netflow_port: Optional[int] = None, ipfix_port: Optional[int] = None) -> FlowPreflightResponse:
    """Checks what ephemres-flowd needs to run: FastStore opens (flowd stops at start-up
    without it), and the UDP ports it will listen on are free. rmem_max below 2 MB is reported
    but does not fail the check: flowd still runs, it just drops packets sooner under load."""
    rmem_val = None
    rmem_ok = True
    try:
        if os.path.exists("/proc/sys/net/core/rmem_max"):
            with open("/proc/sys/net/core/rmem_max", "r") as f:
                rmem_val = int(f.read().strip())
                rmem_ok = rmem_val >= 2097152
    except Exception as e:
        logger.debug("Could not inspect /proc/sys/net/core/rmem_max: %s", e)

    # The ports flowd will use: the ones saved in Settings (runtime settings), which override
    # config.toml, or the ones being saved right now. Checking config.toml's tested the wrong
    # ports once an admin had changed them.
    rt = get_runtime_settings()
    ports = (netflow_port or rt.flow_netflow_port, ipfix_port or rt.flow_ipfix_port)
    flow_bind = getattr(app_cfg.flow, "bind_host", "0.0.0.0")
    port_conflicts = await _udp_port_conflicts(flow_bind, ports)
    udp_ports_ok = len(port_conflicts) == 0

    faststore_ok = False
    faststore_error = ""
    try:
        from app.core.faststore import FastStoreClient

        client = FastStoreClient(decode_responses=True)
        faststore_ok = bool(await client.ping())
        await client.aclose()
    except Exception as exc:
        faststore_error = f"{type(exc).__name__}: {exc}"

    ready = faststore_ok and udp_ports_ok
    problems = []
    if not faststore_ok:
        problems.append(f"FastStore could not be opened ({faststore_error or 'no answer'}).")
    if not udp_ports_ok:
        problems.append(f"UDP port(s) already in use: {', '.join(str(p) for p in port_conflicts)}.")
    if not rmem_ok and rmem_val is not None:
        problems.append(f"Kernel rmem_max is {rmem_val} B, below the recommended 2 MB (2097152 B).")
    message = " ".join(problems) if problems else "Ready: FastStore opens and the flow ports are free."

    return FlowPreflightResponse(
        faststore_connected=faststore_ok,
        rmem_max=rmem_val,
        rmem_max_supported=rmem_ok,
        udp_ports_available=udp_ports_ok,
        port_conflicts=port_conflicts,
        ready=ready,
        message=message,
    )


async def check_flow_prerequisites(netflow_port: Optional[int] = None, ipfix_port: Optional[int] = None) -> tuple[bool, str]:
    """The flow preflight as (ready, message), for PATCH /settings."""
    res = await _run_flow_check(netflow_port, ipfix_port)
    return res.ready, res.message


@router.post("/flow/preflight", response_model=APIResponse)
async def test_flow_preflight(
    current_user: dict = Depends(require_admin),
):
    """The Flow tab's Diagnostic Preflight (see _run_flow_check)."""
    res = await _run_flow_check()
    return APIResponse.success(data=res)
