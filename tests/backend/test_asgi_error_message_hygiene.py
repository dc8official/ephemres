import pytest
from app.core.asgi import EphemresApp, TestClient


class SensitiveException(RuntimeError):
    pass


class TypeWithSensitiveError:
    @classmethod
    def __get_validators__(cls):
        yield cls.validate

    @classmethod
    def validate(cls, v):
        raise SensitiveException("Secret internal database password or stack trace")


def custom_converter(v):
    raise SensitiveException("Secret database connection string leaked!")


def test_coercion_error_message_generic_for_unexpected_exceptions(monkeypatch):
    import app.core.asgi as asgi_mod

    # Patch _coerce_value in asgi module
    orig_coerce = asgi_mod._coerce_value

    def fake_coerce(val, target_type, field_name, localns):
        if target_type is TypeWithSensitiveError:
            raise SensitiveException("Internal secret: /var/secrets/key.pem leaked")
        return orig_coerce(val, target_type, field_name, localns)

    monkeypatch.setattr(asgi_mod, "_coerce_value", fake_coerce)

    app = EphemresApp()

    @app.get("/test-param")
    async def get_test(param: TypeWithSensitiveError):
        return {"ok": True}

    client = TestClient(app)

    res = client.get("/test-param?param=anything")
    assert res.status_code == 422
    data = res.json()
    errors = data.get("detail", [])
    assert len(errors) == 1
    # Must NOT contain the sensitive message
    assert "Internal secret" not in errors[0]["msg"]
    assert errors[0]["msg"] == "Invalid value"


def test_coercion_error_message_preserves_value_error():
    app = EphemresApp()

    @app.get("/items/{item_id}")
    async def get_item(item_id: int):
        return {"item_id": item_id}

    client = TestClient(app)

    res = client.get("/items/not-an-int")
    assert res.status_code == 422
    data = res.json()
    errors = data.get("detail", [])
    assert len(errors) == 1
    # ValueError/TypeError messages from normal integer coercion are preserved
    assert errors[0]["type"] == "value_error"
    assert "Input should be a valid integer" in errors[0]["msg"]
