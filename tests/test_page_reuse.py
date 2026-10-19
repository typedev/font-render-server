"""
Page reuse in POST /render: a page whose @font-face block is already loaded is
reused for the next request with the same fonts + scale (only style and body
are swapped). Output must be byte-identical to rendering on a fresh page.

    uv run pytest tests/test_page_reuse.py -v
"""

import pytest
from starlette.testclient import TestClient

from font_render import app as appmod, config
from test_geometry import _find_font
from test_staleness import _unhinted

REQUESTS = [
    {"text": "a", "ppem": "12", "layout": "plain"},
    {"text": "Hamburgefontsiv", "ppem": "9-16", "layout": "plain"},
    {"text": "b", "ppem": "12", "layout": "full"},
    {"text": "HEFI", "ppem": "10,20", "layout": "plain", "geometry": "true"},
    {"text": "xyz", "ppem": "14", "layout": "full", "colors": "#c00000,#0000c0", "line_height": "2"},
    {"text": "a", "ppem": "12", "layout": "plain", "width": "400"},
    {"text": "a", "ppem": "12", "layout": "plain", "zoom": "3"},
    {"text": "a", "ppem": "12", "layout": "plain"},
]


def _post(c, fonts, form):
    files = [("fonts", ("font.ttf", f)) for f in fonts]
    r = c.post("/render", files=files, data=form)
    assert r.status_code == 200, r.text
    return r


def _run_all(fonts):
    out = []
    with TestClient(appmod.app) as c:
        for form in REQUESTS:
            r = _post(c, fonts, form)
            out.append((r.content, r.headers["server-timing"]))
    return out


@pytest.fixture(scope="module")
def fonts():
    a = _find_font()
    return [a, _unhinted(a)]


def test_reuse_is_pixel_identical_to_fresh(fonts, monkeypatch):
    reused = _run_all(fonts)
    monkeypatch.setattr(config, "PAGE_CACHE", 0)
    fresh = _run_all(fonts)
    for i, ((png_r, timing_r), (png_f, timing_f)) in enumerate(zip(reused, fresh)):
        assert png_r == png_f, f"request #{i} {REQUESTS[i]} differs between reused and fresh page"
        assert 'page;desc="new"' in timing_f
    # after the first request every following one reuses the loaded page
    assert 'page;desc="new"' in reused[0][1]
    assert all('page;desc="reuse"' in t for _, t in reused[1:])


def test_reuse_follows_edited_bytes(fonts):
    """A -> A' -> A with page reuse on: output always follows the bytes."""
    a, a2 = fonts
    form = {"text": "Hamburgefontsiv", "ppem": "9,10,11,12", "layout": "plain"}
    with TestClient(appmod.app) as c:
        first = _post(c, [a], form).content
        edited = _post(c, [a2], form).content
        again = _post(c, [a], form)
        assert first != edited
        assert again.content == first
        assert 'page;desc="reuse"' in again.headers["server-timing"]
        assert _post(c, [a2], form).content == edited


def test_page_cache_is_bounded(fonts, monkeypatch):
    monkeypatch.setattr(config, "PAGE_CACHE", 2)
    monkeypatch.setattr(config, "POOL_SIZE", 1)
    form = {"text": "a", "ppem": "12", "layout": "plain"}
    a, a2 = fonts
    with TestClient(appmod.app) as c:
        for fset in ([a], [a2], [a, a2], [a2, a]):
            _post(c, fset, form)
        assert c.get("/info").json()["pool"]["page_cache"]["pages"] <= 2 * 2  # 2 per browser, ct on/off warm
        for slots in appmod._pool.slots.values():
            for slot in slots:
                assert len(slot.pages) <= 2


def test_rejected_font_is_an_error_not_a_fallback(fonts):
    broken = b"\x00\x01\x00\x00" + b"\x00" * 64  # TrueType magic, no tables
    with TestClient(appmod.app) as c:
        files = [("fonts", ("a.ttf", fonts[0])), ("fonts", ("b.ttf", broken))]
        r = c.post("/render", files=files, data={"text": "a", "ppem": "12", "layout": "plain"})
    assert r.status_code == 422, r.text
    assert r.json() == {"error": "font_not_loaded", "fonts": [1]}



def test_empty_font_is_refused(fonts):
    with TestClient(appmod.app) as c:
        r = c.post("/render", files=[("fonts", ("a.ttf", b""))],
                   data={"text": "a", "ppem": "12", "layout": "plain"})
    assert r.status_code == 400, r.text
    assert r.json() == {"error": "Font #1 is empty"}


def test_background_reverse_video(fonts):
    import io
    from PIL import Image

    form = {"text": "H", "ppem": "16", "layout": "plain"}
    with TestClient(appmod.app) as c:
        white = Image.open(io.BytesIO(_post(c, fonts[:1], form).content)).convert("RGB")
        black = Image.open(io.BytesIO(_post(c, fonts[:1], {**form, "background": "#000"}).content)).convert("RGB")
        bad = c.post("/render", files=[("fonts", ("a.ttf", fonts[0]))], data={**form, "background": "red"})
    assert white.getpixel((0, 0)) == (255, 255, 255)
    assert black.getpixel((0, 0)) == (0, 0, 0)
    # default text is white on a dark background: the darkest pixel of the white page
    # becomes (about) the lightest of the black one
    assert white.convert("L").getextrema()[0] < 64
    assert black.convert("L").getextrema()[1] > 192
    assert bad.status_code == 400, bad.text


def test_dark_background_dims_separators():
    from font_render import page

    assert page.is_dark("#000") and page.is_dark("#202020")
    assert not page.is_dark("#fff") and not page.is_dark("#c0c0c0")
    kw = dict(ppem_values=[12], text="a", font_names=["a"], font_data=[(".ttf", b"x")],
              colors=["#fff"], cleartype=True)
    assert page.DARK_BG_CSS in page.build_page(**kw, bg_color="#000000").style
    assert page.DARK_BG_CSS not in page.build_page(**kw).style
