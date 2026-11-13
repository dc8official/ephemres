"""SNMP credential sets (v3.3.6 build plan 8.3, decided: shared sets assigned to endpoints).

A set is a name plus everything a device needs to answer: v2c community, or a v3 user with any
authentication / privacy combination the vendor research found (monitoring/snmp/usm.py), one
password for both when the device wants that (Meraki, UniFi), a context, port and timeouts.

Secrets (community, v3 passwords) are one JSON value encrypted with core/crypto's
encrypt_secret, the envelope alert channels use, so backup restore re-encrypts them the same
way. They are write-only: the API reports whether each is set, never its value. Updating a set
without sending a secret keeps the stored one.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from uuid import UUID

from app.core.asgi import HTTPException
from app.core.crypto import DecryptionError, decrypt_secret, encrypt_secret
from app.core.orm import AsyncSession, text

# Kept in step with monitoring/snmp/usm.py (AUTH_PROTOCOLS, PRIV_PROTOCOLS); a test checks it.
AUTH_PROTOCOLS = ("md5", "sha", "sha224", "sha256", "sha384", "sha512")
PRIV_PROTOCOLS = ("des", "3des", "aes", "aes128", "aes192", "aes256", "aes192c", "aes256c")
SECRET_FIELDS = ("community", "auth_password", "priv_password")
_NAME_RE = re.compile(r"^[A-Za-z0-9 _.\-()/]{1,64}$")
_COLUMNS = ("id", "name", "version", "username", "auth_protocol", "priv_protocol", "priv_same",
            "context", "port", "timeout_ms", "retries", "secrets", "created_at", "updated_at")


@dataclass
class CredentialSet:
    id: UUID
    name: str
    version: str
    username: Optional[str]
    auth_protocol: Optional[str]
    priv_protocol: Optional[str]
    priv_same: bool
    context: str
    port: int
    timeout_ms: int
    retries: int
    secrets: Dict[str, str]
    created_at: Any = None
    updated_at: Any = None

    @property
    def level(self) -> str:
        if self.version == "2c":
            return "v2c"
        return "authPriv" if self.priv_protocol else "authNoPriv" if self.auth_protocol else "noAuthNoPriv"

    def public(self, endpoints: int = 0) -> Dict[str, Any]:
        """What the API shows: everything but the secrets, which are only reported as set."""
        return {
            "id": str(self.id), "name": self.name, "version": self.version, "level": self.level,
            "username": self.username, "auth_protocol": self.auth_protocol,
            "priv_protocol": self.priv_protocol, "priv_same": self.priv_same, "context": self.context,
            "port": self.port, "timeout_ms": self.timeout_ms, "retries": self.retries,
            "has_community": bool(self.secrets.get("community")),
            "has_auth_password": bool(self.secrets.get("auth_password")),
            "has_priv_password": bool(self.secrets.get("priv_password")),
            "endpoints": endpoints,
            "created_at": self.created_at, "updated_at": self.updated_at,
        }

    def client_credentials(self) -> Any:
        """The monitoring/snmp client's credentials. Imported here, not at module level, so the
        API starts even where the SNMP package is not on the path (it then fails only on use)."""
        from monitoring.snmp.client import CommunityAuth, UserAuth

        if self.version == "2c":
            return CommunityAuth(self.secrets.get("community", "").encode())
        auth_pw = self.secrets.get("auth_password")
        priv_pw = None if self.priv_same else self.secrets.get("priv_password")
        return UserAuth(
            (self.username or "").encode(), self.auth_protocol,
            auth_pw.encode() if auth_pw else None, self.priv_protocol,
            priv_pw.encode() if priv_pw else None, context=(self.context or "").encode(),
        )


def _decode_secrets(raw: str) -> Dict[str, str]:
    if not raw:
        return {}
    try:
        data = json.loads(decrypt_secret(raw))
    except (DecryptionError, ValueError, TypeError):
        # Unreadable (a restore without the old key): treated as empty, so the set shows as
        # missing its secrets and polls fail visibly until an admin re-enters them.
        return {}
    return {k: str(v) for k, v in data.items() if k in SECRET_FIELDS and v}


def _from_row(row: Any) -> CredentialSet:
    m = dict(zip(_COLUMNS, tuple(row)))
    return CredentialSet(
        id=m["id"], name=m["name"], version=m["version"], username=m["username"],
        auth_protocol=m["auth_protocol"], priv_protocol=m["priv_protocol"], priv_same=bool(m["priv_same"]),
        context=m["context"] or "", port=int(m["port"]), timeout_ms=int(m["timeout_ms"]),
        retries=int(m["retries"]), secrets=_decode_secrets(m["secrets"] or ""),
        created_at=m["created_at"], updated_at=m["updated_at"],
    )


def validate(fields: Dict[str, Any], secrets: Dict[str, str]) -> None:
    """Checks a set as it will be stored (fields merged with what was there). Raises 422 with
    a sentence an admin can act on."""
    def bad(msg: str) -> None:
        raise HTTPException(status_code=422, detail=msg)

    if not _NAME_RE.match(fields.get("name") or ""):
        bad("Name: 1 to 64 letters, digits, spaces or _ . - ( ) /")
    version = fields.get("version")
    if version not in ("2c", "3"):
        bad("Version must be 2c or 3.")
    if version == "2c":
        if not secrets.get("community"):
            bad("A v2c set needs a community.")
        return
    if not fields.get("username"):
        bad("A v3 set needs a user name.")
    auth, priv = fields.get("auth_protocol"), fields.get("priv_protocol")
    if auth is not None and auth not in AUTH_PROTOCOLS:
        bad(f"Authentication protocol must be one of {', '.join(AUTH_PROTOCOLS)}.")
    if priv is not None and priv not in PRIV_PROTOCOLS:
        bad(f"Privacy protocol must be one of {', '.join(PRIV_PROTOCOLS)}.")
    if priv and not auth:
        bad("Privacy needs authentication too (authPriv).")
    if auth and len(secrets.get("auth_password") or "") < 8:
        bad("The authentication password must be at least 8 characters (RFC 3414).")
    if priv and not fields.get("priv_same") and len(secrets.get("priv_password") or "") < 8:
        bad("The privacy password must be at least 8 characters, or use the authentication password for both.")


async def list_sets(db: AsyncSession) -> List[Dict[str, Any]]:
    res = await db.execute(text(f"""
        SELECT {", ".join("c." + c for c in _COLUMNS)},
               (SELECT COUNT(*) FROM endpoints e WHERE e.snmp_credential_id = c.id AND e.endpoint_status != 'DELETED') AS n
        FROM snmp_credentials c ORDER BY lower(c.name)
    """))
    # ORM rows index but do not slice, hence tuple().
    return [_from_row(tuple(r)[:len(_COLUMNS)]).public(int(tuple(r)[len(_COLUMNS)] or 0)) for r in res.fetchall()]


async def get_set(db: AsyncSession, set_id: UUID | str) -> Optional[CredentialSet]:
    res = await db.execute(text(f"SELECT {', '.join(_COLUMNS)} FROM snmp_credentials WHERE id = CAST(:id AS uuid)"),
                           {"id": str(set_id)})
    row = res.fetchone()
    return _from_row(row) if row else None


async def require_set(db: AsyncSession, set_id: UUID | str) -> CredentialSet:
    found = await get_set(db, set_id)
    if found is None:
        raise HTTPException(status_code=400, detail="That SNMP credential set does not exist.")
    return found


async def _name_taken(db: AsyncSession, name: str, other_than: Optional[str] = None) -> bool:
    res = await db.execute(
        text("SELECT 1 FROM snmp_credentials WHERE lower(name) = lower(:name) AND (CAST(:id AS uuid) IS NULL OR id <> CAST(:id AS uuid))"),
        {"name": name, "id": other_than},
    )
    return res.fetchone() is not None


def _clean(fields: Dict[str, Any]) -> Dict[str, Any]:
    """Drops what does not apply to the version, so a v2c set keeps no v3 leftovers."""
    out = dict(fields)
    if out.get("version") == "2c":
        out.update(username=None, auth_protocol=None, priv_protocol=None, priv_same=False, context="")
    elif not out.get("priv_protocol"):
        out["priv_same"] = False
    return out


def _clean_secrets(version: str, fields: Dict[str, Any], secrets: Dict[str, str]) -> Dict[str, str]:
    if version == "2c":
        return {"community": secrets["community"]} if secrets.get("community") else {}
    keep = {}
    if fields.get("auth_protocol") and secrets.get("auth_password"):
        keep["auth_password"] = secrets["auth_password"]
    if fields.get("priv_protocol") and not fields.get("priv_same") and secrets.get("priv_password"):
        keep["priv_password"] = secrets["priv_password"]
    return keep


async def create_set(db: AsyncSession, fields: Dict[str, Any], secrets: Dict[str, str]) -> CredentialSet:
    fields = _clean(fields)
    validate(fields, secrets)
    if await _name_taken(db, fields["name"]):
        raise HTTPException(status_code=409, detail="A credential set with that name already exists.")
    stored = _clean_secrets(fields["version"], fields, secrets)
    res = await db.execute(text("""
        INSERT INTO snmp_credentials (name, version, username, auth_protocol, priv_protocol, priv_same,
                                      context, port, timeout_ms, retries, secrets)
        VALUES (:name, :version, :username, :auth_protocol, :priv_protocol, :priv_same,
                :context, :port, :timeout_ms, :retries, :secrets)
        RETURNING id
    """), {**{k: fields.get(k) for k in ("name", "version", "username", "auth_protocol", "priv_protocol")},
           "priv_same": bool(fields.get("priv_same")), "context": fields.get("context") or "",
           "port": int(fields.get("port") or 161), "timeout_ms": int(fields.get("timeout_ms") or 2000),
           "retries": int(fields.get("retries") if fields.get("retries") is not None else 1),
           "secrets": encrypt_secret(json.dumps(stored)) if stored else ""})
    new_id = res.fetchone()[0]
    return await get_set(db, new_id)


async def update_set(db: AsyncSession, set_id: UUID | str, changes: Dict[str, Any],
                     new_secrets: Dict[str, Optional[str]]) -> CredentialSet:
    """`changes` holds only the fields sent; a secret sent as a value replaces the stored one,
    sent empty or left out keeps it (the form never shows stored secrets)."""
    current = await require_set(db, set_id)
    fields = {k: getattr(current, k) for k in ("name", "version", "username", "auth_protocol", "priv_protocol",
                                               "priv_same", "context", "port", "timeout_ms", "retries")}
    fields.update(changes)
    fields = _clean(fields)
    secrets = dict(current.secrets)
    secrets.update({k: v for k, v in new_secrets.items() if v})
    validate(fields, secrets)
    if fields["name"] != current.name and await _name_taken(db, fields["name"], str(set_id)):
        raise HTTPException(status_code=409, detail="A credential set with that name already exists.")
    stored = _clean_secrets(fields["version"], fields, secrets)
    await db.execute(text("""
        UPDATE snmp_credentials SET name = :name, version = :version, username = :username,
               auth_protocol = :auth_protocol, priv_protocol = :priv_protocol, priv_same = :priv_same,
               context = :context, port = :port, timeout_ms = :timeout_ms, retries = :retries,
               secrets = :secrets, updated_at = NOW()
        WHERE id = CAST(:id AS uuid)
    """), {**{k: fields.get(k) for k in ("name", "version", "username", "auth_protocol", "priv_protocol")},
           "priv_same": bool(fields.get("priv_same")), "context": fields.get("context") or "",
           "port": int(fields["port"]), "timeout_ms": int(fields["timeout_ms"]), "retries": int(fields["retries"]),
           "secrets": encrypt_secret(json.dumps(stored)) if stored else "", "id": str(set_id)})
    return await get_set(db, set_id)


async def delete_set(db: AsyncSession, set_id: UUID | str) -> int:
    """Deletes the set; its endpoints stop being polled (the foreign key sets them to NULL).
    Returns how many endpoints that was."""
    await require_set(db, set_id)
    res = await db.execute(text("SELECT COUNT(*) FROM endpoints WHERE snmp_credential_id = CAST(:id AS uuid)"),
                           {"id": str(set_id)})
    n = int(res.scalar() or 0)
    await db.execute(text("DELETE FROM snmp_credentials WHERE id = CAST(:id AS uuid)"), {"id": str(set_id)})
    return n


def adhoc_set(fields: Dict[str, Any], secrets: Dict[str, str]) -> CredentialSet:
    """A set that is not stored, for Test before saving."""
    fields = _clean(fields)
    validate({**fields, "name": fields.get("name") or "test"}, secrets)
    return CredentialSet(
        id=UUID(int=0), name=fields.get("name") or "test", version=fields["version"], username=fields.get("username"),
        auth_protocol=fields.get("auth_protocol"), priv_protocol=fields.get("priv_protocol"),
        priv_same=bool(fields.get("priv_same")), context=fields.get("context") or "",
        port=int(fields.get("port") or 161), timeout_ms=int(fields.get("timeout_ms") or 2000),
        retries=int(fields.get("retries") if fields.get("retries") is not None else 1),
        secrets=_clean_secrets(fields["version"], fields, secrets),
    )
