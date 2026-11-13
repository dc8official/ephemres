"""SNMP credential sets, the Test button, and each endpoint's SNMP status (v3.3.6 build plan 8.3).

ephemres-snmpd does the polling; this router only stores what it needs (credential sets, a
probe request) and shows what it learned (snmp_device_state, snmp_interfaces,
snmp_interface_events).

Test runs the survey (monitoring/snmp/survey.py) from the API process against one address. It
sends UDP from the server to whatever address an admin types, so it is admin only, audited, and
limited to a few runs a minute per worker.
"""
from __future__ import annotations

import json
import time
from collections import deque
from typing import Any, Deque, Dict, Literal, Optional
from uuid import UUID

from app.core.asgi import APIRouter, Depends, HTTPException
from app.core.orm import AsyncSession, text
from app.core.schema import BaseModel, ConfigDict, Field
from app.database import get_db
from app.repositories.auth_repo import AuthRepository
from app.routers.auth import get_current_user, require_admin
from app.schemas import APIResponse
from app.services import snmp_credentials as creds_service

router = APIRouter(prefix="/snmp", tags=["SNMP"])

TEST_LIMIT_PER_MINUTE = 6
TEST_INTERVAL_SECONDS = 3.0  # the counter read in a test: short, the admin is waiting
_recent_tests: Deque[float] = deque()


class CredentialSetRequest(BaseModel):
    name: Optional[str] = None
    version: Optional[Literal["2c", "3"]] = None
    username: Optional[str] = Field(default=None, max_length=64)
    auth_protocol: Optional[Literal["md5", "sha", "sha224", "sha256", "sha384", "sha512"]] = None
    priv_protocol: Optional[Literal["des", "3des", "aes", "aes128", "aes192", "aes256", "aes192c", "aes256c"]] = None
    priv_same: Optional[bool] = None
    context: Optional[str] = Field(default=None, max_length=64)
    port: Optional[int] = Field(default=None, ge=1, le=65535)
    timeout_ms: Optional[int] = Field(default=None, ge=500, le=10000)
    retries: Optional[int] = Field(default=None, ge=0, le=3)
    # Write-only. Left out or empty on update: the stored value is kept.
    community: Optional[str] = Field(default=None, max_length=128)
    auth_password: Optional[str] = Field(default=None, max_length=128)
    priv_password: Optional[str] = Field(default=None, max_length=128)

    model_config = ConfigDict(from_attributes=True)

    def fields(self) -> Dict[str, Any]:
        keys = ("name", "version", "username", "auth_protocol", "priv_protocol", "priv_same",
                "context", "port", "timeout_ms", "retries")
        return {k: getattr(self, k) for k in keys if k in self.model_fields_set}

    def secrets(self) -> Dict[str, Optional[str]]:
        return {k: getattr(self, k) for k in creds_service.SECRET_FIELDS}


class TestRequest(BaseModel):
    address: str = Field(max_length=255)
    # A stored set, or an unsaved one (the form's current values) to try before saving.
    credential_id: Optional[UUID] = None
    credential: Optional[CredentialSetRequest] = None

    model_config = ConfigDict(from_attributes=True)


def _admin_uuid(user: dict) -> Optional[UUID]:
    try:
        return UUID(str(user.get("sub")))
    except (TypeError, ValueError):
        return None


async def _audit(db: AsyncSession, user: dict, action: str, target: UUID, details: Dict[str, Any]) -> None:
    await AuthRepository(db).create_audit_log(
        user_id=_admin_uuid(user), action=action, target_type="snmp_credentials", target_id=target, details=details,
    )


# --- credential sets -----------------------------------------------------------------------------

@router.get("/credentials", response_model=APIResponse)
async def list_credentials(_: dict = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    return APIResponse.success(data=await creds_service.list_sets(db))


@router.post("/credentials", response_model=APIResponse)
async def create_credentials(request: CredentialSetRequest, user: dict = Depends(require_admin),
                             db: AsyncSession = Depends(get_db)):
    fields = request.fields()
    fields.setdefault("version", "2c")
    secrets = {k: v for k, v in request.secrets().items() if v}
    created = await creds_service.create_set(db, fields, secrets)
    await _audit(db, user, "SNMP_CREDENTIALS:CREATE", created.id,
                 {"name": created.name, "level": created.level, "secrets_set": sorted(secrets)})
    await db.commit()
    return APIResponse.success(data=created.public())


@router.patch("/credentials/{set_id}", response_model=APIResponse)
async def update_credentials(set_id: UUID, request: CredentialSetRequest, user: dict = Depends(require_admin),
                             db: AsyncSession = Depends(get_db)):
    changed_secrets = sorted(k for k, v in request.secrets().items() if v)
    updated = await creds_service.update_set(db, set_id, request.fields(), request.secrets())
    await _audit(db, user, "SNMP_CREDENTIALS:UPDATE", set_id,
                 {"name": updated.name, "changed": sorted(request.fields()), "secrets_changed": changed_secrets})
    await db.commit()
    return APIResponse.success(data=updated.public())


@router.delete("/credentials/{set_id}", response_model=APIResponse)
async def delete_credentials(set_id: UUID, user: dict = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    found = await creds_service.require_set(db, set_id)
    n = await creds_service.delete_set(db, set_id)
    await _audit(db, user, "SNMP_CREDENTIALS:DELETE", set_id, {"name": found.name, "endpoints_turned_off": n})
    await db.commit()
    return APIResponse.success(data={"deleted": str(set_id), "endpoints_turned_off": n})


# --- Test ----------------------------------------------------------------------------------------

def _rate_limit() -> None:
    now = time.monotonic()
    while _recent_tests and now - _recent_tests[0] > 60:
        _recent_tests.popleft()
    if len(_recent_tests) >= TEST_LIMIT_PER_MINUTE:
        raise HTTPException(status_code=429, detail="Too many SNMP tests in the last minute; wait a moment.")
    _recent_tests.append(now)


@router.post("/test", response_model=APIResponse)
async def test_credentials(request: TestRequest, user: dict = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    """Surveys one address with a stored or an unsaved set and returns what it answered."""
    from monitoring.snmp.survey import _jsonable, render_text, run_survey

    if request.credential_id:
        cred = await creds_service.require_set(db, request.credential_id)
    elif request.credential:
        fields = request.credential.fields()
        fields.setdefault("version", "2c")
        cred = creds_service.adhoc_set(fields, {k: v for k, v in request.credential.secrets().items() if v})
    else:
        raise HTTPException(status_code=422, detail="Choose a credential set or fill one in.")
    address = request.address.strip()
    if not address:
        raise HTTPException(status_code=422, detail="Enter the device's address.")
    _rate_limit()
    report = await run_survey(address, cred.client_credentials(), port=cred.port, timeout=cred.timeout_ms / 1000,
                              retries=cred.retries, interval=TEST_INTERVAL_SECONDS)
    await _audit(db, user, "SNMP:TEST", cred.id, {"address": address, "set": cred.name, "ok": bool(report.get("ok"))})
    await db.commit()
    return APIResponse.success(data={
        "ok": bool(report.get("ok")),
        "error": report.get("error"),
        "text": render_text(report),
        "findings": report.get("findings", []),
        "profile": report.get("profile"),
        "interfaces": len(report.get("interfaces") or []),
        "system": _jsonable(report.get("system") or {}),
    })


# --- per endpoint --------------------------------------------------------------------------------

@router.get("/endpoints/{endpoint_id}", response_model=APIResponse)
async def endpoint_snmp(endpoint_id: UUID, _: dict = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """What ephemres-snmpd knows about one endpoint: status, profile, interfaces, recent events."""
    ep = (await db.execute(text("""
        SELECT e.snmp_credential_id, c.name FROM endpoints e
        LEFT JOIN snmp_credentials c ON c.id = e.snmp_credential_id
        WHERE e.id = CAST(:id AS uuid) AND e.endpoint_status != 'DELETED'
    """), {"id": str(endpoint_id)})).fetchone()
    if ep is None:
        raise HTTPException(status_code=404, detail="Endpoint not found.")
    state = (await db.execute(text("""
        SELECT status, last_error, last_poll_at, last_ok_at, last_probe_at, probe_requested_at,
               last_reboot_at, sys_uptime, inventory_at, profile
        FROM snmp_device_state WHERE endpoint_id = CAST(:id AS uuid)
    """), {"id": str(endpoint_id)})).fetchone()
    ifs = (await db.execute(text("""
        SELECT if_index, if_name, if_descr, if_alias, if_type, speed_mbps, mac, admin_status, oper_status,
               last_change, hc_counters
        FROM snmp_interfaces WHERE endpoint_id = CAST(:id AS uuid) ORDER BY if_index
    """), {"id": str(endpoint_id)})).fetchall()
    events = (await db.execute(text("""
        SELECT at, kind, if_index, if_name, detail FROM snmp_interface_events
        WHERE endpoint_id = CAST(:id AS uuid) ORDER BY at DESC LIMIT 50
    """), {"id": str(endpoint_id)})).fetchall()
    neighbours = (await db.execute(text("""
        SELECT n.protocol, n.local_if_index, n.local_port, n.remote_name, n.remote_chassis, n.remote_port,
               n.remote_port_desc, n.remote_platform, n.remote_addresses, n.matched_endpoint_id, n.matched_by,
               m.hostname AS matched_hostname, n.first_seen, n.last_seen
        FROM snmp_neighbours n
        LEFT JOIN endpoints m ON m.id = n.matched_endpoint_id AND m.endpoint_status != 'DELETED'
        WHERE n.endpoint_id = CAST(:id AS uuid)
        ORDER BY n.local_if_index NULLS LAST, n.remote_name
    """), {"id": str(endpoint_id)})).fetchall()
    neighbour_keys = ("protocol", "local_if_index", "local_port", "remote_name", "remote_chassis", "remote_port",
                      "remote_port_desc", "remote_platform", "remote_addresses", "matched_endpoint_id", "matched_by",
                      "matched_hostname", "first_seen", "last_seen")
    state_keys = ("status", "last_error", "last_poll_at", "last_ok_at", "last_probe_at", "probe_requested_at",
                  "last_reboot_at", "sys_uptime", "inventory_at", "profile")
    if_keys = ("if_index", "if_name", "if_descr", "if_alias", "if_type", "speed_mbps", "mac", "admin_status",
               "oper_status", "last_change", "hc_counters")
    state_view = dict(zip(state_keys, state)) if state else None
    if state_view and isinstance(state_view.get("profile"), str):
        state_view["profile"] = json.loads(state_view["profile"])
    return APIResponse.success(data={
        "credential_id": str(ep[0]) if ep[0] else None,
        "credential_name": ep[1],
        "state": state_view,
        "interfaces": [dict(zip(if_keys, r)) for r in ifs],
        "events": [dict(zip(("at", "kind", "if_index", "if_name", "detail"), r)) for r in events],
        # LLDP/CDP neighbours (v3.3.6 step 3); matched_endpoint_id when it is a monitored endpoint.
        "neighbours": [
            {**dict(zip(neighbour_keys, tuple(r))),
             "matched_endpoint_id": str(r[9]) if r[9] else None,
             "remote_addresses": list(r[8] or [])}
            for r in neighbours
        ],
    })


@router.post("/endpoints/{endpoint_id}/probe", response_model=APIResponse)
async def request_probe(endpoint_id: UUID, user: dict = Depends(require_admin), db: AsyncSession = Depends(get_db)):
    """Asks ephemres-snmpd to re-read the whole device (profile, inventory, neighbours) at its
    next turn, which is within one poll interval."""
    ep = (await db.execute(text("SELECT snmp_credential_id FROM endpoints WHERE id = CAST(:id AS uuid) AND endpoint_status != 'DELETED'"),
                           {"id": str(endpoint_id)})).fetchone()
    if ep is None:
        raise HTTPException(status_code=404, detail="Endpoint not found.")
    if not ep[0]:
        raise HTTPException(status_code=400, detail="This endpoint has no SNMP credential set.")
    await db.execute(text("""
        INSERT INTO snmp_device_state (endpoint_id, probe_requested_at) VALUES (CAST(:id AS uuid), NOW())
        ON CONFLICT (endpoint_id) DO UPDATE SET probe_requested_at = NOW(), updated_at = NOW()
    """), {"id": str(endpoint_id)})
    await db.commit()
    return APIResponse.success(data={"probe_requested": True})
