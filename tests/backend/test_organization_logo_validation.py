import base64

import pytest

from app.routers.settings import _is_embeddable_logo

PNG_1PX = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)
JPEG_HEAD = "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01" + b"\x00" * 8).decode()
SVG = "data:image/svg+xml;base64," + base64.b64encode(b'<svg xmlns="http://www.w3.org/2000/svg"/>').decode()


@pytest.mark.parametrize("uri", [PNG_1PX, JPEG_HEAD, JPEG_HEAD.replace("image/jpeg", "image/jpg")])
def test_png_and_jpeg_logos_are_accepted(uri):
    assert _is_embeddable_logo(uri)


@pytest.mark.parametrize(
    "uri",
    [
        SVG,
        PNG_1PX.replace("image/png", "image/jpeg"),  # declared type doesn't match the bytes
        "data:image/png,not-base64",
        "data:image/png;base64,!!!!",
        "https://example.com/logo.png",
    ],
)
def test_unembeddable_logos_are_rejected(uri):
    assert not _is_embeddable_logo(uri)
