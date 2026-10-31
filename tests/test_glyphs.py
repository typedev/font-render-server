"""
POST /glyphs with a fake engine (the real ones need Windows): parameters, layout, cell
independence, determinism; sfnt.prepare keeps every table but `name`/`head` byte-identical.

    uv run pytest tests/test_glyphs.py -v
"""

import base64
import hashlib
import io

import pytest
from fontTools.ttLib import TTFont
from PIL import Image

from font_render import glyphs, sfnt
from font_render.engines import Cell, Positions, pens, place
from test_font_cache import _upload, c, fonts  # noqa: F401  (fixtures)


class FakeEngine:
    """Draws gid-dependent rectangles; advance = ppem/2 + gid % 3; ink can start left of origin."""

    name = "gdi"
    render_version = 1

    def ppem_ok(self, ppem):
        return ppem == int(ppem)

    def parse_params(self, form):
        mode = form.get("mode") or "cleartype"
        if mode not in ("bw", "cleartype"):
            raise ValueError("mode")
        return {"mode": mode}

    def _cell(self, gid, ppem, ink):
        if gid == 0:
            return Cell(gid, None, 0, 0, 0)
        w, h = 1 + gid % 5, int(ppem)
        img = Image.new("RGB", (w, h), ink)
        return Cell(gid, img, -(gid % 2), -h + 2, int(ppem) // 2 + gid % 3, {"abc": [0, w, 0]})

    def resolve(self, font, params):
        return params

    def cells(self, font, gids, ppem, params, background, ink):
        return int(ppem), int(ppem) // 4, [self._cell(g, ppem, ink) for g in gids]

    def run(self, font, gids, ppem, params, background, ink, positions=None):
        cells = [self._cell(g, ppem, ink) for g in gids]
        advances, offsets = [c.advance for c in cells], None
        if positions is not None:
            advances, offsets = place(font, gids, ppem, advances, positions, whole=True)
            advances = [int(a) for a in advances]
        pen = [int(p) for p in pens(advances)]
        off = [(int(dx), int(dy)) for dx, dy in offsets] if offsets else [(0, 0)] * len(gids)
        pen_x, base_y = 100, 2 * int(ppem) + 100
        img = Image.new("RGB", (pen[-1] + advances[-1] + 200, 4 * int(ppem) + 200), background)
        for p, (dx, dy), cell in zip(pen, off, cells):
            if cell.img is not None:
                img.paste(cell.img, (pen_x + p + dx + cell.left, base_y - dy + cell.top))
        return img, pen_x, base_y, pen, advances, offsets and off


@pytest.fixture(autouse=True)
def fake_engine(monkeypatch):
    monkeypatch.setattr(glyphs, "ENGINES", {"gdi": FakeEngine()})


def _post(c, who, font_id, **form):
    return c.post("/glyphs", data={"engine": "gdi", "font_ids": font_id, **form},
                  headers=c.tokens[who])


def _image(r) -> Image.Image:
    return Image.open(io.BytesIO(base64.b64decode(r.json()["image"]))).convert("RGB")


def _glyph_pixels(r, ppem, gid):
    """The glyph's ink box relative to (x0, baseline): (bytes, relative box)."""
    img = _image(r)
    row = next(x for x in r.json()["geometry"]["rows"] if x["ppem"] == ppem)
    g = next(x for x in row["glyphs"] if x["gid"] == gid)
    rel = (g["box"][0] - g["x0"], g["box"][1] - row["baseline_y"],
           g["box"][2] - g["x0"], g["box"][3] - row["baseline_y"])
    return img.crop(g["box"]).tobytes(), rel


def test_cells_independent_of_neighbours(c, fonts):
    [fid] = _upload(c, "alice", ("a.ttf", fonts[0]))
    alone = _post(c, "alice", fid, gids="7", ppem="12")
    mixed = _post(c, "alice", fid, gids="3,1,7,9,7", ppem="10,12,16")
    assert alone.status_code == 200, alone.text
    assert _glyph_pixels(alone, 12, 7) == _glyph_pixels(mixed, 12, 7)


def test_identical_requests_identical_bytes(c, fonts):
    [fid] = _upload(c, "alice", ("a.ttf", fonts[0]))
    a = _post(c, "alice", fid, gids="1-20", ppem="9-14")
    b = _post(c, "alice", fid, gids="1-20", ppem="9-14")
    assert a.json()["image"] == b.json()["image"]


def test_geometry_shape(c, fonts):
    [fid] = _upload(c, "alice", ("a.ttf", fonts[0]))
    font = TTFont(io.BytesIO(fonts[0]))
    r = _post(c, "alice", fid, gids="0,1,2", ppem="12", mode="bw").json()
    geo = r["geometry"]
    assert geo["engine"] == "gdi" and geo["params"]["mode"] == "bw"
    assert geo["upem"] == font["head"].unitsPerEm
    [row] = geo["rows"]
    assert row["ppem"] == 12 and isinstance(row["baseline_y"], int)
    assert [g["gid"] for g in row["glyphs"]] == [0, 1, 2]
    order = font.getGlyphOrder()
    for g in row["glyphs"]:
        assert isinstance(g["x0"], int)
        assert g["design_advance"] == font["hmtx"][order[g["gid"]]][0]
    assert row["glyphs"][1]["abc"] == [0, 2, 0]
    xs = [g["x0"] for g in row["glyphs"]]
    assert xs == sorted(xs)


def test_run_layout_uses_engine_offsets(c, fonts):
    [fid] = _upload(c, "alice", ("a.ttf", fonts[0]))
    r = _post(c, "alice", fid, gids="1,2,3", ppem="12", layout="run")
    assert r.status_code == 200, r.text
    row = r.json()["geometry"]["rows"][0]
    xs = [g["x0"] for g in row["glyphs"]]
    advances = [g["advance"] for g in row["glyphs"]]
    assert xs[1] - xs[0] == advances[0] and xs[2] - xs[1] == advances[1]


def test_run_positions(c, fonts):
    """advances/offsets in font units: own advance + (given - hmtx) scaled, offsets scaled,
    rounded for whole-pixel engines; x0/y0 are the origins used, boxes follow the glyphs."""
    [fid] = _upload(c, "alice", ("a.ttf", fonts[0]))
    font = TTFont(io.BytesIO(fonts[0]))
    upem, order = font["head"].unitsPerEm, font.getGlyphOrder()
    hmtx = [font["hmtx"][order[g]][0] for g in (1, 2, 3)]
    plain = _post(c, "alice", fid, gids="1,2,3", ppem="20", layout="run")
    kern = -upem // 10                                    # -2 px at 20 ppem
    r = _post(c, "alice", fid, gids="1,2,3", ppem="20", layout="run",
              advances=f"{hmtx[0] + kern},{hmtx[1]},{hmtx[2]}",
              offsets=f"0:0,0:{upem // 4},{upem // 20}:0")  # glyph 2 up 5 px, glyph 3 right 1 px
    assert r.status_code == 200, r.text
    geo = r.json()["geometry"]
    assert geo["params"]["positions"] == "advances+offsets"
    [row], [prow] = geo["rows"], plain.json()["geometry"]["rows"]
    g, pg = row["glyphs"], prow["glyphs"]
    assert g[0]["advance"] == pg[0]["advance"] - 2 and g[1]["advance"] == pg[1]["advance"]
    assert g[1]["x0"] - g[0]["x0"] == g[0]["advance"]
    assert g[2]["x0"] - g[1]["x0"] == g[1]["advance"] + 1
    assert row["baseline_y"] - g[1]["y0"] == 5 and g[0]["y0"] == row["baseline_y"]
    assert g[1]["box"][1] - g[1]["y0"] == pg[1]["box"][1] - prow["baseline_y"]
    img = _image(r)
    assert img.crop(g[1]["box"]).tobytes() == _image(plain).crop(pg[1]["box"]).tobytes()
    assert "y0" not in pg[0]

    only = _post(c, "alice", fid, gids="1,2", ppem="20", layout="run",
                 advances=f"{hmtx[0]},{hmtx[1]}").json()["geometry"]
    assert only["params"]["positions"] == "advances" and "y0" not in only["rows"][0]["glyphs"][0]
    a = _post(c, "alice", fid, gids="1,2,3", ppem="20", layout="run", advances="1,2,3")
    b = _post(c, "alice", fid, gids="1,2,3", ppem="20", layout="run", advances="1,2,3")
    assert a.json()["image"] == b.json()["image"]


def test_run_positions_validation(c, fonts):
    [fid] = _upload(c, "alice", ("a.ttf", fonts[0]))
    run = dict(gids="1,2", ppem="12", layout="run")
    for bad in (dict(advances="500"), dict(advances="500,500", offsets="0:0"),
                dict(offsets="0:0,0:0"), dict(advances="500,x"), dict(advances="500,500",
                offsets="0:0,5"), dict(advances="5e3,1")):
        r = _post(c, "alice", fid, **run, **bad)
        assert r.status_code == 400 and r.json()["error"] == "bad_params", bad
    r = _post(c, "alice", fid, gids="1,2", ppem="12", advances="500,500")
    assert r.status_code == 400 and "layout=run" in r.json()["error"]
    assert _post(c, "alice", fid, **run, advances="-12.5,500", offsets="-3:4.5,0:0").status_code == 200


def test_validation(c, fonts):
    [fid] = _upload(c, "alice", ("a.ttf", fonts[0]))
    assert _post(c, "alice", fid, gids="1", ppem="12.5").json()["error"] == "bad_ppem"
    assert _post(c, "alice", fid, gids="1", mode="nope").json()["error"] == "bad_params"
    assert _post(c, "alice", fid, gids="", ppem="12").status_code == 400
    assert _post(c, "alice", fid, gids="1", layout="x").status_code == 400
    r = _post(c, "alice", fid, gids="1,999999", ppem="12")
    assert r.status_code == 400 and r.json()["error"] == "gid_out_of_range"
    assert _post(c, "alice", "0" * 64, gids="1").status_code == 409
    r = c.post("/glyphs", data={"engine": "dwrite", "font_ids": fid, "gids": "1"},
               headers=c.tokens["alice"])
    assert r.status_code == 501


def test_upload_instead_of_id(c, fonts):
    r = c.post("/glyphs", data={"engine": "gdi", "gids": "1,2", "ppem": "12"},
               files=[("fonts", ("a.ttf", fonts[0]))], headers=c.tokens["alice"])
    assert r.status_code == 200, r.text


def test_parse_helpers():
    assert glyphs.parse_gids("3,5-7, 3") == [3, 5, 6, 7, 3]
    assert glyphs.parse_ppems("9-11") == [9.0, 10.0, 11.0]
    assert glyphs.parse_ppems("9,10.5") == [9.0, 10.5]
    assert glyphs.parse_color("#f00", (0, 0, 0)) == (255, 0, 0)
    with pytest.raises(ValueError):
        glyphs.parse_gids("5-3")


def test_prepare_renames_only_name(fonts):
    data = fonts[0]
    sha = hashlib.sha256(data).hexdigest()
    prepared = sfnt.prepare(data, sha)
    a, b = TTFont(io.BytesIO(data)), TTFont(io.BytesIO(prepared.data))
    assert sorted(a.reader.keys()) == sorted(b.reader.keys())
    changed = [t for t in a.reader.keys() if a.reader[t] != b.reader[t]]
    assert set(changed) <= {"name", "head"}
    head_a, head_b = bytearray(a.reader["head"]), bytearray(b.reader["head"])
    head_a[8:12] = head_b[8:12] = b"\0\0\0\0"  # checkSumAdjustment
    assert head_a == head_b
    assert b["name"].getDebugName(1) == prepared.face == f"F_{sha[:16]}"
    assert prepared.num_glyphs == len(a.getGlyphOrder())


def test_dwrite_params():
    from font_render.dwrite_engine import DEFAULTS, DWriteEngine, axis_tag
    e = DWriteEngine()
    assert {k: v for k, v in e.parse_params({}).items() if k != "axes"} == DEFAULTS
    p = e.parse_params({"rendering_mode": "gdi_classic", "antialias": "grayscale", "gamma": "2.2",
                        "axes": "wght=700, wdth=80"})
    assert p["rendering_mode"] == "gdi_classic" and p["gamma"] == 2.2
    assert p["axes"] == {"wght": 700.0, "wdth": 80.0}
    for bad in ({"rendering_mode": "outline"}, {"gamma": "9"}, {"cleartype_level": "-1"},
                {"axes": "toolong=1"}, {"pixel_geometry": "rgbw"}):
        with pytest.raises(ValueError):
            e.parse_params(bad)
    assert e.parse_params({})["axes"] == {}
    assert axis_tag("wght") == 0x74686777


def test_prepare_drops_stat():
    """A partial VF instance keeps fvar; GDI would name its default face from STAT."""
    from pathlib import Path
    src = Path("/usr/share/fonts/truetype/ubuntu/UbuntuSans[wdth,wght].ttf")
    if not src.is_file():
        pytest.skip("no variable font with STAT found")
    from fontTools.varLib.instancer import instantiateVariableFont
    font = TTFont(src)
    instantiateVariableFont(font, {"wght": 300}, inplace=True)
    buf = io.BytesIO()
    font.save(buf)
    data = buf.getvalue()
    prepared = sfnt.prepare(data, hashlib.sha256(data).hexdigest())
    a, b = TTFont(io.BytesIO(data)), TTFont(io.BytesIO(prepared.data))
    assert "STAT" in a and "STAT" not in b and "fvar" in b
    assert prepared.is_variable
    assert all(a.reader[t] == b.reader[t] for t in b.reader.keys() if t not in ("name", "head"))


VF_PATH = "/usr/share/fonts/truetype/ubuntu/UbuntuSans[wdth,wght].ttf"


@pytest.fixture(scope="module")
def vf():
    from pathlib import Path
    if not Path(VF_PATH).is_file():
        pytest.skip("no variable font found")
    return Path(VF_PATH).read_bytes()


def test_resolve_location(vf):
    axes = sfnt.font_axes(vf, hashlib.sha256(vf).hexdigest())
    tags = [a[0] for a in axes]
    loc = sfnt.resolve_location(axes, sfnt.parse_location("wght=350.5, wdth=10"))
    assert list(loc) == tags                      # every axis, fvar order
    wdth = next(a for a in axes if a[0] == "wdth")
    assert loc["wght"] == 350.5 and loc["wdth"] == wdth[1]   # clamped to min
    with pytest.raises(ValueError):
        sfnt.resolve_location(axes, {"XXXX": 1.0})
    for bad in ("wght", "wght=abc", "toolong=1", "wght=nan"):
        with pytest.raises(ValueError):
            sfnt.parse_location(bad)


def test_dwrite_resolve_fills_axes(vf):
    from font_render.dwrite_engine import DWriteEngine
    e = DWriteEngine()
    font = sfnt.prepare(vf, hashlib.sha256(vf).hexdigest())
    p = e.resolve(font, e.parse_params({"axes": "wght=300"}))
    assert dict(p["axes"])["wght"] == 300 and len(p["axes"]) == len(font.axes)
    with pytest.raises(ValueError):
        e.resolve(font, e.parse_params({"axes": "opsz=12"}))


def test_gdi_rejects_axes():
    from font_render.gdi import GdiEngine
    with pytest.raises(ValueError, match="static instance"):
        GdiEngine().parse_params({"axes": "wght=300"})


def test_page_variations_one_face_per_font(vf):
    from font_render import page
    loc = {"wdth": 87.0, "wght": 350.5}
    parts = page.build_page([12], "Ham", ["a", "a", "a"], [(".ttf", vf)] * 3, ["#000"] * 3,
                            True, layout="plain", variations=[loc, None, {"wdth": 100.0, "wght": 700.0}])
    assert len(set(parts.families)) == 1 and len(parts.fonts_key) == 1
    assert parts.font_faces().count("@font-face") == 1
    assert 'font-variation-settings: "wdth" 87, "wght" 350.5;' in parts.style
    assert parts.style.count("font-variation-settings") == 2


def test_page_features():
    from font_render import page
    assert page.parse_features("") == ""
    assert page.parse_features(' "kern" 1, \'liga\' off,ss01, cv02 3 ') == \
        '"kern" 1, "liga" 0, "ss01" 1, "cv02" 3'
    for bad in ('"ke" 1', '"kern" 70000', 'kern -1', '"kern" 1; } body {', '"k\\"r" 1',
                ", ".join(["kern"] * 65)):
        with pytest.raises(ValueError):
            page.parse_features(bad)
    fd = [(".ttf", b"\0\1\0\0a"), (".ttf", b"\0\1\0\0b")]
    plain = page.build_page([12], "Ham", ["a", "b"], fd, ["#000"] * 2, True, layout="plain")
    feat = page.build_page([12], "Ham", ["a", "b"], fd, ["#000"] * 2, True, layout="plain",
                           features='"kern" 1, "liga" 0')
    assert "font-feature-settings" not in plain.style
    assert feat.style.count('font-feature-settings: "kern" 1, "liga" 0;') == 2
    assert feat.body == plain.body and feat.fonts_key == plain.fonts_key
