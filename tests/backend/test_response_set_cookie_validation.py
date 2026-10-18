import pytest
from app.core.asgi import Response


def test_set_cookie_rejects_control_chars_and_semicolon():
    resp = Response()

    # Valid cookie
    resp.set_cookie("valid_key", "valid_value")
    assert any("valid_key=valid_value" in h for h in resp.headers.getlist("set-cookie"))

    # Invalid in key
    for bad in (";", "\r", "\n", "key;inject", "key\rname", "key\nname"):
        with pytest.raises(ValueError, match="invalid character"):
            resp.set_cookie(bad, "value")

    # Invalid in value
    for bad in (";", "\r", "\n", "val;inject", "val\rbreak", "val\nbreak"):
        with pytest.raises(ValueError, match="invalid character"):
            resp.set_cookie("key", bad)
