"""
Tests for the geometry mode of POST /render.

Run locally on any OS with a Playwright chromium installed:
    uv run pytest tests/test_geometry.py -v

Uses a system TTF font; skips if none is found.
"""

import base64
import io
from pathlib import Path

import pytest
from PIL import Image
from starlette.testclient import TestClient

from font_render.app import app

FONT_CANDIDATES = [
    "/usr/share/fonts/liberation-sans-fonts/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/google-carlito-fonts/Carlito-Regular.ttf",
    "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "C:/Windows/Fonts/arial.ttf",
]

NBSP = " "
TEXT = NBSP.join("HEFI")  # glyphs separated like the QA client does
PPEMS = "9,12,16,24"


def _find_font() -> bytes:
    for path in FONT_CANDIDATES:
        p = Path(path)
        if p.is_file():
            return p.read_bytes()
    pytest.skip("no system TTF font found")


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def font_bytes():
    return _find_font()


def _render(client, font_bytes, **form):
    data = {
        "ppem": PPEMS,
        "text": TEXT,
        "layout": "plain",
        "cleartype": "false",  # LCD filtering irrelevant for geometry tests
        **{k: str(v) for k, v in form.items()},
    }
    files = [("fonts", ("test.ttf", io.BytesIO(font_bytes), "font/ttf"))]
    resp = client.post("/render", data=data, files=files)
    assert resp.status_code == 200, resp.text
    return resp


def test_png_without_geometry(client, font_bytes):
    resp = _render(client, font_bytes)
    assert resp.headers["content-type"] == "image/png"
    assert resp.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_geometry_structure(client, font_bytes):
    resp = _render(client, font_bytes, geometry="true")
    assert resp.headers["content-type"].startswith("application/json")
    payload = resp.json()
    img = Image.open(io.BytesIO(base64.b64decode(payload["image"])))
    geo = payload["geometry"]
    rows = geo["rows"]

    ppems = [int(p) for p in PPEMS.split(",")]
    assert [r["ppem"] for r in rows] == ppems

    for row in rows:
        x0, y0, x1, y1 = row["box"]
        # baseline sits inside the row's line box
        assert y0 <= row["baseline_y"] <= y1
        # every char of the text got a rect (NBSP separators included)
        assert len(row["glyphs"]) == len(TEXT)
        assert "".join(g["char"] for g in row["glyphs"]) == TEXT
        # advance boxes are ordered and non-overlapping, inside the image
        for a, b in zip(row["glyphs"], row["glyphs"][1:]):
            assert a["x1"] <= b["x0"] + 0.01
        for g in row["glyphs"]:
            assert 0 <= g["x0"] < g["x1"] <= img.width
        assert 0 <= y0 < y1 <= img.height


def test_nbsp_separators_have_distinct_rects(client, font_bytes):
    """The empirical unknown: NBSP runs must not merge into one rect."""
    resp = _render(client, font_bytes, geometry="true")
    rows = resp.json()["geometry"]["rows"]
    for row in rows:
        seps = [g for g in row["glyphs"] if g["char"] == NBSP]
        assert len(seps) == TEXT.count(NBSP)
        for g in seps:
            assert g["x1"] > g["x0"]  # nonzero advance


def test_pixel_identity_with_and_without_geometry(client, font_bytes):
    """Geometry collection must not change a single pixel."""
    plain = _render(client, font_bytes).content
    with_geo = base64.b64decode(
        _render(client, font_bytes, geometry="true").json()["image"])
    img_a = Image.open(io.BytesIO(plain)).convert("RGB")
    img_b = Image.open(io.BytesIO(with_geo)).convert("RGB")
    assert img_a.size == img_b.size
    assert img_a.tobytes() == img_b.tobytes()


def test_line_height_spreads_rows(client, font_bytes):
    tight = _render(client, font_bytes, geometry="true").json()
    loose = _render(client, font_bytes, geometry="true",
                    line_height="2.0").json()
    def min_baseline_step(payload):
        rows = payload["geometry"]["rows"]
        return min(b["baseline_y"] - a["baseline_y"]
                   for a, b in zip(rows, rows[1:]))
    assert min_baseline_step(loose) > min_baseline_step(tight)
    assert [r["ppem"] for r in loose["geometry"]["rows"]] == \
           [r["ppem"] for r in tight["geometry"]["rows"]]


def test_scale_factor_scales_coordinates(client, font_bytes):
    base = _render(client, font_bytes, geometry="true").json()
    scaled = _render(client, font_bytes, geometry="true", scale="2.0").json()
    b_rows = base["geometry"]["rows"]
    s_rows = scaled["geometry"]["rows"]
    assert scaled["geometry"]["device_scale"] == 2.0
    # coordinates roughly double (layout may shift by a fraction of a px)
    assert abs(s_rows[0]["baseline_y"] - 2 * b_rows[0]["baseline_y"]) < 2.0
    img = Image.open(io.BytesIO(base64.b64decode(scaled["image"])))
    for row in s_rows:
        assert row["box"][3] <= img.height + 0.01


def test_zoom_scales_coordinates(client, font_bytes):
    base = _render(client, font_bytes, geometry="true").json()
    zoomed = _render(client, font_bytes, geometry="true", zoom="4").json()
    img = Image.open(io.BytesIO(base64.b64decode(zoomed["image"])))
    b_img = Image.open(io.BytesIO(base64.b64decode(base["image"])))
    assert img.size == (b_img.width * 4, b_img.height * 4)
    assert abs(zoomed["geometry"]["rows"][0]["baseline_y"]
               - 4 * base["geometry"]["rows"][0]["baseline_y"]) < 0.01


def test_multi_font_rows_tagged(client, font_bytes):
    data = {
        "ppem": "12,16",
        "text": TEXT,
        "layout": "plain",
        "geometry": "true",
    }
    files = [
        ("fonts", ("a.ttf", io.BytesIO(font_bytes), "font/ttf")),
        ("fonts", ("b.ttf", io.BytesIO(font_bytes), "font/ttf")),
    ]
    resp = client.post("/render", data=data, files=files)
    assert resp.status_code == 200, resp.text
    rows = resp.json()["geometry"]["rows"]
    assert [(r["ppem"], r["font"]) for r in rows] == \
           [(12, 1), (12, 2), (16, 1), (16, 2)]


def test_info_advertises_geometry(client):
    resp = client.get("/info")
    assert resp.json()["render"]["geometry"] is True
