from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Dict, Generic, List, Literal, Optional, TypeVar
from uuid import UUID, uuid4

import pytest

from app.core.schema import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_serializer,
    field_validator,
    model_validator,
    to_camel,
)

T = TypeVar("T")


# ---------------------------------------------------------------------------
# 1. Verification of Requirements: python-multipart removed
# ---------------------------------------------------------------------------
def test_python_multipart_removed():
    """Ensure python-multipart is completely removed from backend/requirements.txt."""
    req_file = Path(__file__).resolve().parent.parent.parent / "backend" / "requirements.txt"
    content = req_file.read_text(encoding="utf-8")
    assert "python-multipart" not in content.lower()


# ---------------------------------------------------------------------------
# 2. to_camel & ConfigDict
# ---------------------------------------------------------------------------
def test_to_camel():
    assert to_camel("some_field_name") == "someFieldName"
    assert to_camel("created_at") == "createdAt"
    assert to_camel("id") == "id"
    assert to_camel("l2_auto_bypass") == "l2AutoBypass"
    assert to_camel("") == ""
    assert to_camel("_private_field") == "_privateField"


def test_config_dict():
    cfg = ConfigDict(from_attributes=True, alias_generator=to_camel, populate_by_name=True)
    assert cfg["from_attributes"] is True
    assert cfg["alias_generator"] == to_camel
    assert cfg["populate_by_name"] is True


# ---------------------------------------------------------------------------
# 3. Field constraints, bounds & default factories
# ---------------------------------------------------------------------------
class BoundedModel(BaseModel):
    name: str = Field(..., min_length=3, max_length=15, pattern=r"^[a-zA-Z0-9_\-]+$")
    score: int = Field(default=50, ge=0, le=100)
    ratio: float = Field(default=1.0, gt=0.0, lt=10.0)
    tags: List[str] = Field(default_factory=list)


def test_field_constraints_success():
    m = BoundedModel(name="valid-name", score=75, ratio=2.5)
    assert m.name == "valid-name"
    assert m.score == 75
    assert m.ratio == 2.5
    assert m.tags == []
    assert m.model_fields_set == {"name", "score", "ratio"}


def test_field_constraints_failures():
    # min_length
    with pytest.raises(ValidationError) as exc:
        BoundedModel(name="ab")
    assert exc.value.errors()[0]["loc"] == ("name",)

    # pattern
    with pytest.raises(ValidationError) as exc:
        BoundedModel(name="invalid name!")
    assert exc.value.errors()[0]["loc"] == ("name",)

    # ge / le bounds
    with pytest.raises(ValidationError) as exc:
        BoundedModel(name="valid", score=-1)
    assert exc.value.errors()[0]["loc"] == ("score",)

    with pytest.raises(ValidationError) as exc:
        BoundedModel(name="valid", score=101)
    assert exc.value.errors()[0]["loc"] == ("score",)

    # gt / lt bounds
    with pytest.raises(ValidationError) as exc:
        BoundedModel(name="valid", ratio=0.0)
    assert exc.value.errors()[0]["loc"] == ("ratio",)

    with pytest.raises(ValidationError) as exc:
        BoundedModel(name="valid", ratio=10.0)
    assert exc.value.errors()[0]["loc"] == ("ratio",)


# ---------------------------------------------------------------------------
# 4. Aliases, to_camel & populate_by_name
# ---------------------------------------------------------------------------
class ExporterSettings(BaseModel):
    exporter_name: str
    listen_port: int = Field(default=2055, ge=1, le=65535)
    sampling_rate: int = 1

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )


def test_aliasing_input_and_dump():
    # Instantiate with camelCase
    m1 = ExporterSettings(exporterName="EdgeRouter", listenPort=9996)
    assert m1.exporter_name == "EdgeRouter"
    assert m1.listen_port == 9996
    assert m1.sampling_rate == 1

    # Dump by_alias=True
    d_alias = m1.model_dump(by_alias=True)
    assert d_alias["exporterName"] == "EdgeRouter"
    assert d_alias["listenPort"] == 9996
    assert d_alias["samplingRate"] == 1

    # Dump by_alias=False
    d_snake = m1.model_dump(by_alias=False)
    assert d_snake["exporter_name"] == "EdgeRouter"
    assert d_snake["listen_port"] == 9996
    assert d_snake["sampling_rate"] == 1

    # Instantiate with snake_case
    m2 = ExporterSettings(exporter_name="CoreSwitch", listen_port=2055)
    assert m2.exporter_name == "CoreSwitch"
    assert m2.listen_port == 2055


# ---------------------------------------------------------------------------
# 5. field_validator (before & after), field_serializer, model_validator
# ---------------------------------------------------------------------------
class MetricPayload(BaseModel):
    operational_state: Literal["UP", "DOWN", "DEGRADED"]
    raw_samples: List[float]
    health_score: float
    gap_detected: bool = False
    gap_timestamp: Optional[datetime] = None

    @field_validator("raw_samples", mode="before")
    @classmethod
    def parse_samples(cls, v):
        if isinstance(v, str):
            return [float(x.strip()) for x in v.split(",") if x.strip()]
        return v

    @field_validator("health_score", mode="after")
    def validate_health(cls, v):
        if not (0.0 <= v <= 100.0):
            raise ValueError("health_score must be between 0.0 and 100.0")
        return round(v, 2)

    @model_validator(mode="after")
    def validate_gap_consistency(self):
        if self.gap_detected and self.gap_timestamp is None:
            raise ValueError("gap_timestamp is required when gap_detected is True")
        return self

    @field_serializer("gap_timestamp")
    def serialize_gap_time(self, v: Optional[datetime]) -> Optional[str]:
        if v is None:
            return None
        return v.strftime("%Y-%m-%d %H:%M:%S UTC")


def test_validators_and_serializers():
    dt = datetime(2026, 10, 17, 5, 0, 0, tzinfo=timezone.utc)
    m = MetricPayload(
        operational_state="UP",
        raw_samples="10.5, 20.25, 30.0",
        health_score=98.7654,
        gap_detected=True,
        gap_timestamp=dt,
    )

    assert m.operational_state == "UP"
    assert m.raw_samples == [10.5, 20.25, 30.0]
    assert m.health_score == 98.77

    # Test serializer
    d = m.model_dump()
    assert d["gap_timestamp"] == "2026-10-17 05:00:00 UTC"

    # Test after validator failure
    with pytest.raises(ValidationError) as exc:
        MetricPayload(
            operational_state="UP",
            raw_samples=[1.0],
            health_score=105.0,
        )
    assert exc.value.errors()[0]["loc"] == ("health_score",)

    # Test model validator failure
    with pytest.raises(ValidationError) as exc:
        MetricPayload(
            operational_state="UP",
            raw_samples=[1.0],
            health_score=80.0,
            gap_detected=True,
            gap_timestamp=None,
        )
    assert "gap_timestamp is required" in str(exc.value)


# ---------------------------------------------------------------------------
# 6. exclude_unset=True for PATCH endpoints
# ---------------------------------------------------------------------------
class PatchEndpointRequest(BaseModel):
    hostname: Optional[str] = None
    ip_address: Optional[str] = None
    monitoring_enabled: Optional[bool] = None
    device_role: Optional[str] = "ACTIVE_HOST"


def test_exclude_unset():
    # Only hostname explicitly passed
    req = PatchEndpointRequest(hostname="new-router")
    assert req.model_fields_set == {"hostname"}
    dumped = req.model_dump(exclude_unset=True)
    assert dumped == {"hostname": "new-router"}

    # Explicitly passing None still counts as set
    req2 = PatchEndpointRequest(hostname="new-router", ip_address=None)
    assert req2.model_fields_set == {"hostname", "ip_address"}
    dumped2 = req2.model_dump(exclude_unset=True)
    assert dumped2 == {"hostname": "new-router", "ip_address": None}

    # Combining exclude_unset and exclude_none
    dumped3 = req2.model_dump(exclude_unset=True, exclude_none=True)
    assert dumped3 == {"hostname": "new-router"}


# ---------------------------------------------------------------------------
# 7. mode="json" serialization & complex types (UUID, datetime, Enum)
# ---------------------------------------------------------------------------
class DeviceStatus(str, Enum):
    ACTIVE = "active"
    STANDBY = "standby"


class TelemetryItem(BaseModel):
    device_id: UUID
    status: DeviceStatus
    recorded_at: datetime
    metadata: Dict[str, str] = Field(default_factory=dict)


def test_json_mode_serialization():
    uid = uuid4()
    now = datetime(2026, 10, 17, 5, 30, 0, tzinfo=timezone.utc)
    item = TelemetryItem(
        device_id=str(uid),
        status="active",
        recorded_at="2026-10-17T05:30:00Z",
        metadata={"rack": "R1"},
    )

    assert item.device_id == uid
    assert item.status == DeviceStatus.ACTIVE
    assert item.recorded_at == now

    # mode="python" keeps native python types
    py_dump = item.model_dump(mode="python")
    assert isinstance(py_dump["device_id"], UUID)
    assert isinstance(py_dump["recorded_at"], datetime)

    # mode="json" coerces to JSON primitives
    json_dump = item.model_dump(mode="json")
    assert json_dump["device_id"] == str(uid)
    assert json_dump["status"] == "active"
    assert json_dump["recorded_at"] == "2026-10-17T05:30:00+00:00"

    # model_dump_json() returns valid JSON string
    json_str = item.model_dump_json()
    assert str(uid) in json_str
    assert "2026-10-17T05:30:00+00:00" in json_str


# ---------------------------------------------------------------------------
# 8. from_attributes=True and from_orm
# ---------------------------------------------------------------------------
class UserSummary(BaseModel):
    id: UUID
    username: str
    is_active: bool

    model_config = ConfigDict(from_attributes=True)


class MockDatabaseRow:
    def __init__(self, id: UUID, username: str, is_active: bool, secret_hash: str):
        self.id = id
        self.username = username
        self.is_active = is_active
        self.secret_hash = secret_hash


def test_from_attributes_and_from_orm():
    uid = uuid4()
    row = MockDatabaseRow(id=uid, username="admin_user", is_active=True, secret_hash="argon2id$...")

    # from_orm
    summary = UserSummary.from_orm(row)
    assert summary.id == uid
    assert summary.username == "admin_user"
    assert summary.is_active is True
    assert not hasattr(summary, "secret_hash")

    # model_validate with from_attributes
    summary2 = UserSummary.model_validate(row, from_attributes=True)
    assert summary2 == summary


# ---------------------------------------------------------------------------
# 9. Generic[T] parameterization (APIResponse[T])
# ---------------------------------------------------------------------------
class APIResponse(BaseModel, Generic[T]):
    status: Literal["success", "error"]
    data: Optional[T] = None
    model_config = ConfigDict(from_attributes=True)

    @classmethod
    def success(cls, data: T) -> APIResponse[T]:
        return cls(status="success", data=data)


def test_generic_model():
    # Parameterized with primitive
    res_str = APIResponse[str].success("pong")
    assert res_str.status == "success"
    assert res_str.data == "pong"

    # Parameterized with list of models
    uid1 = uuid4()
    uid2 = uuid4()
    res_list = APIResponse[List[UserSummary]](
        status="success",
        data=[
            {"id": str(uid1), "username": "alice", "is_active": True},
            {"id": str(uid2), "username": "bob", "is_active": False},
        ],
    )
    assert len(res_list.data) == 2
    assert isinstance(res_list.data[0], UserSummary)
    assert res_list.data[0].id == uid1
    assert res_list.data[1].username == "bob"

    # JSON serialization of generic container
    d = res_list.model_dump(mode="json")
    assert d["status"] == "success"
    assert d["data"][0]["id"] == str(uid1)
    assert d["data"][1]["username"] == "bob"
