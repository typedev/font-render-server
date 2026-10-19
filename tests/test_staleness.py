"""
Staleness guard for POST /render: an edited font (new bytes, same file name,
same internal family name) must never be rendered from a stale copy.

Builds A' from a system TTF by stripping its hinting (fontTools), so A and A'
differ only in bytes that change rasterization at small PPEM.

    uv run pytest tests/test_staleness.py -v
"""

import io

import pytest
from starlette.testclient import TestClient

from font_render.app import app
from test_geometry import _find_font

fontTools = pytest.importorskip("fontTools")
from fontTools.ttLib import TTFont  # noqa: E402

FORM = {"ppem": "9,10,11,12,13", "text": "Hamburgefontsiv 0123", "layout": "plain"}
NAME = "font.ttf"


def _unhinted(data: bytes) -> bytes:
    font = TTFont(io.BytesIO(data))
    for tag in ("fpgm", "prep", "cvt ", "hdmx", "LTSH", "VDMX"):
        if tag in font:
            del font[tag]
    glyf = font["glyf"]
    for name in glyf.keys():
        glyph = glyf[name]
        if hasattr(glyph, "program"):
            glyph.program.fromBytecode(b"")
    out = io.BytesIO()
    font.save(out)
    return out.getvalue()


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def fonts():
    a = _find_font()
    a2 = _unhinted(a)
    assert a != a2
    return a, a2


def _render(client, *datas: bytes, **extra) -> bytes:
    files = [("fonts", (NAME, d)) for d in datas]
    r = client.post("/render", files=files, data={**FORM, **extra})
    assert r.status_code == 200, r.text
    return r.content


@pytest.mark.parametrize("cleartype", ["true", "false"])
def test_output_follows_bytes(client, fonts, cleartype):
    a, a2 = fonts
    png_a = _render(client, a, cleartype=cleartype)
    png_a2 = _render(client, a2, cleartype=cleartype)
    assert png_a != png_a2, "hinting change must be visible, otherwise the test proves nothing"
    # back and forth: every render must match the bytes it was given
    assert _render(client, a, cleartype=cleartype) == png_a
    assert _render(client, a2, cleartype=cleartype) == png_a2
    assert _render(client, a, cleartype=cleartype) == png_a


def test_same_name_twice_in_one_request(client, fonts):
    a, a2 = fonts
    mixed = _render(client, a, a2)
    assert mixed != _render(client, a, a)
    assert mixed != _render(client, a2, a2)
    assert _render(client, a, a2) == mixed
