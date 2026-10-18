import os
import stat
import pytest
from app.reset_legacy_passwords import reset_legacy_passwords
from app.services.auth_service import (
    hash_password,
    verify_password,
    needs_rehash,
    ScryptPasswordHasher,
)


class FakeRow:
    def __init__(self, id, username):
        self.id = id
        self.username = username


class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class FakeDB:
    def __init__(self, users):
        self.users = users
        self.committed = False

    async def execute(self, stmt, params=None):
        sql = stmt.text_str if hasattr(stmt, "text_str") else str(stmt)
        if "SELECT id, username FROM users WHERE password_hash NOT LIKE" in sql:
            rows = [
                FakeRow(u["id"], u["username"])
                for u in self.users
                if not str(u["password_hash"]).startswith("$scrypt$")
            ]
            return FakeResult(rows)
        elif "UPDATE users" in sql:
            uid = params["id"]
            for u in self.users:
                if str(u["id"]) == str(uid):
                    u["password_hash"] = params["h"]
                    u["must_change_password"] = True
            return FakeResult([])
        return FakeResult([])

    async def commit(self):
        self.committed = True


@pytest.mark.anyio
async def test_reset_legacy_passwords_workflow(tmp_path):
    report_file = tmp_path / "legacy_resets.txt"

    argon2_hash = "$argon2id$v=19$m=65536,t=3,p=4$abc$def"
    scrypt_hash = hash_password("existing_scrypt_password")

    users = [
        {
            "id": "11111111-1111-1111-1111-111111111111",
            "username": "legacy_alice",
            "password_hash": argon2_hash,
            "must_change_password": False,
        },
        {
            "id": "22222222-2222-2222-2222-222222222222",
            "username": "modern_bob",
            "password_hash": scrypt_hash,
            "must_change_password": False,
        },
    ]

    db = FakeDB(users)

    # 1. Run reset_legacy_passwords
    count = await reset_legacy_passwords(session=db, report_path=str(report_file))
    assert count == 1
    assert db.committed is True

    # Check Alice was updated to scrypt and must_change_password=True
    alice = next(u for u in users if u["username"] == "legacy_alice")
    assert alice["password_hash"].startswith("$scrypt$")
    assert alice["must_change_password"] is True

    # Check Bob was left untouched
    bob = next(u for u in users if u["username"] == "modern_bob")
    assert bob["password_hash"] == scrypt_hash
    assert bob["must_change_password"] is False

    # Check report file permissions and content
    assert os.path.exists(report_file)
    mode = stat.S_IMODE(os.stat(report_file).st_mode)
    assert mode == 0o600

    with open(report_file, "r", encoding="utf-8") as f:
        lines = [line.strip() for line in f if line.strip()]

    assert len(lines) == 1
    parts = lines[0].split("\t")
    assert len(parts) == 2
    assert parts[0] == "legacy_alice"
    temp_pwd = parts[1]
    assert verify_password(temp_pwd, alice["password_hash"]) is True

    # 2. Second run: idempotent (finds 0 legacy users)
    db.committed = False
    count2 = await reset_legacy_passwords(session=db, report_path=str(report_file))
    assert count2 == 0
    assert db.committed is False


def test_auth_service_argon2_and_needs_rehash():
    hasher = ScryptPasswordHasher()

    # verify returns False for $argon2...
    argon2_hash = "$argon2id$v=19$m=65536,t=3,p=4$abc$def"
    assert hasher.verify(argon2_hash, "password123") is False
    assert verify_password("password123", argon2_hash) is False

    # needs_rehash is True for $argon2...
    assert needs_rehash(argon2_hash) is True

    # needs_rehash is True for scrypt with outdated parameters
    outdated_scrypt = "$scrypt$n=1024,r=8,p=1$YWJjZGVmZ2hpamtsbW5vcA$abcdef"
    assert needs_rehash(outdated_scrypt) is True

    # Fresh hash produces needs_rehash == False
    fresh_hash = hash_password("secret_password")
    assert fresh_hash.startswith("$scrypt$")
    assert needs_rehash(fresh_hash) is False
    assert verify_password("secret_password", fresh_hash) is True
