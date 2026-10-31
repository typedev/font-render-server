"""POST /glyphs: render glyph ids with a native engine (GDI; DirectWrite next) and lay them out.

Layout `cells`: every glyph is drawn alone at an integer pen origin, so its pixels relative to
(x0, baseline_y) do not depend on the other gids in the request. Layout `run`: the engine lays
the glyphs out in one call (spacing view), with its own advances or with the positions of a
shaped run (`advances`, `offsets` in font units). Each engine runs on its own
single worker thread; the output is deterministic (identical requests → identical PNG bytes).
"""

import asyncio
import base64
import hashlib
import io
import math
import re
import time
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from PIL import Image
from starlette.datastructures import UploadFile

from . import config, dwrite_engine, gdi, page, sfnt
from .engines import Cell, Positions, RenderError

router = APIRouter()

MAX_GIDS = 512
MAX_PPEM = 256
MAX_CELLS = 20000      # gids × ppem values per request
MAX_GAP = 64
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_NUM_RE = re.compile(r"^\d+(?:\.\d+)?$")
_SIGNED_RE = re.compile(r"^-?\d+(?:\.\d+)?$")
MAX_UNITS = 1_000_000  # |advance|, |offset| in font units


def available_engines() -> dict:
    """Engines this host can run: name -> engine instance."""
    out = {}
    if gdi.available():
        out["gdi"] = gdi.GdiEngine()
        out["dwrite"] = dwrite_engine.DWriteEngine()
    return out


ENGINES = available_engines()
_executors: dict[str, ThreadPoolExecutor] = {}
_in_flight = 0


def _executor(name: str) -> ThreadPoolExecutor:
    if name not in _executors:
        _executors[name] = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"engine-{name}")
    return _executors[name]


def _error(status: int, error: str, **extra) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": error, **extra})


def parse_gids(s: str) -> list[int]:
    """'12,13,40-60' -> [12, 13, 40, ..., 60] (order kept, duplicates kept)."""
    out = []
    for part in (p.strip() for p in s.split(",")):
        if not part:
            continue
        lo, _, hi = part.partition("-")
        a, b = int(lo), int(hi) if hi else int(lo)
        if a < 0 or b < a:
            raise ValueError(part)
        out.extend(range(a, b + 1))
        if len(out) > MAX_GIDS:
            break
    return out


def parse_ppems(s: str) -> list[float]:
    """'9-24' (integers) or '9,10.5,12' -> numbers in device pixels."""
    s = s.strip()
    if "-" in s and "," not in s:
        lo, hi = (int(x) for x in s.split("-"))
        return [float(x) for x in range(lo, hi + 1)]
    out = []
    for part in (p.strip() for p in s.split(",")):
        if not _NUM_RE.match(part):
            raise ValueError(part)
        out.append(float(part))
    return out


def _units(s: str) -> float:
    s = s.strip()
    if not _SIGNED_RE.match(s) or abs(float(s)) > MAX_UNITS:
        raise ValueError(s[:20])
    return float(s)


def parse_positions(fields: dict, count: int) -> Positions | None:
    """`advances` ('600,580,-12') and `offsets` ('0:0,-40:310', dx:dy) in font units, one per
    gid; None when neither is given. Offsets need advances (a shaper gives both)."""
    adv, off = fields.get("advances", "").strip(), fields.get("offsets", "").strip()
    if not adv and not off:
        return None
    if not adv:
        raise ValueError("offsets need advances")
    advances = [_units(x) for x in adv.split(",")]
    offsets = None
    if off:
        offsets = []
        for item in off.split(","):
            dx, sep, dy = item.partition(":")
            if not sep:
                raise ValueError(f"offset must be dx:dy: {item.strip()[:20]}")
            offsets.append((_units(dx), _units(dy)))
    if len(advances) != count or (offsets is not None and len(offsets) != count):
        raise ValueError(f"advances and offsets need one value per gid ({count})")
    return Positions(advances, offsets)


def parse_color(s: str, default: tuple[int, int, int]) -> tuple[int, int, int]:
    s = s.strip()
    if not s:
        return default
    if not page.BG_COLOR_RE.match(s):
        raise ValueError(s)
    h = s[1:]
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _num(x: float):
    """JSON number: an int when whole, so GDI coordinates read as integers."""
    return int(x) if float(x).is_integer() else x


def compose(rows: list[dict], gap: int, background) -> tuple[Image.Image, list[dict]]:
    """Stack engine rows into one image. Each row: {ppem, ascent, descent, cells, run?}.
    run = engine.run()'s (image, pen_x, baseline_y, pens, advances, offsets) for layout=run,
    else None (cells layout)."""
    placed, width, y = [], 1, gap
    for row in rows:
        cells: list[Cell] = row["cells"]
        run = row.get("run")
        offsets = run[5] if run is not None else None
        dys = [dy for _dx, dy in offsets] if offsets else [0] * len(cells)
        inked = [(c, dy) for c, dy in zip(cells, dys) if c.img is not None]
        asc = max([row["ascent"], 0] + [dy - c.top for c, dy in inked])
        desc = max([row["descent"], 0] + [c.top + c.img.height - dy for c, dy in inked])
        if run is None:
            xs, x = [], gap
            for c in cells:
                lead = max(0, -c.left) if c.img is not None else 0
                right = max(math.ceil(c.advance), c.left + c.img.width if c.img is not None else 0, 0)
                xs.append(x + lead)
                x += lead + right + gap
            row_w, paste, ys, advances = max(x, gap), None, None, [c.advance for c in cells]
        else:
            img, pen_x, base_y, pen, advances, _ = run
            origins = [p + dx for p, (dx, _dy) in zip(pen, offsets)] if offsets else pen
            end = pen[-1] + max(math.ceil(advances[-1]), 0) if pen else 0
            ink = [(pen_x + o + c.left, pen_x + o + c.left + c.img.width)
                   for o, c in zip(origins, cells) if c.img is not None]
            left = math.floor(min([pen_x] + [a for a, _ in ink]))  # offsets may be fractional
            right = math.ceil(max([pen_x + end] + [b for _, b in ink]))
            crop = img.crop((left, base_y - asc, right, base_y + desc))
            xs = [gap + pen_x - left + o for o in origins]
            ys = dys if offsets else None
            row_w, paste = gap + crop.width + gap, crop
        placed.append((row, y, asc, desc, xs, ys, advances, paste, row_w))
        width = max(width, row_w)
        y += asc + desc + gap

    out = Image.new("RGB", (width, max(y, 1)), background)
    geo = []
    for row, top, asc, desc, xs, ys, advances, paste, row_w in placed:
        baseline = top + asc
        if paste is not None:
            out.paste(paste, (gap, top))
        glyphs = []
        for i, (x0, adv, c) in enumerate(zip(xs, advances, row["cells"])):
            y0 = baseline - ys[i] if ys else baseline
            if c.img is not None:
                if paste is None:
                    out.paste(c.img, (x0 + c.left, baseline + c.top))
                box = [x0 + c.left, y0 + c.top, x0 + c.left + c.img.width,
                       y0 + c.top + c.img.height]
            else:
                box = [x0, y0, x0, y0]
            glyphs.append({"gid": c.gid, "x0": _num(x0), **({"y0": _num(y0)} if ys else {}),
                           "advance": _num(adv), "box": box, **c.extra})
        geo.append({"ppem": _num(row["ppem"]), "baseline_y": baseline,
                    "box": [gap, top, row_w - gap, top + asc + desc],
                    "ascent": row["ascent"], "descent": row["descent"], **row.get("extra", {}),
                    "glyphs": glyphs})
    return out, geo


def _render_sync(engine, font: sfnt.PreparedFont, gids, ppems, params, layout, gap,
                 background, ink, positions=None) -> tuple[bytes, list[dict]]:
    rows = []
    for ppem in ppems:
        ascent, descent, cells, *extra = engine.cells(font, gids, ppem, params, background, ink)
        run = (engine.run(font, gids, ppem, params, background, ink, positions)
               if layout == "run" else None)
        rows.append({"ppem": ppem, "ascent": ascent, "descent": descent, "cells": cells, "run": run,
                     "extra": extra[0] if extra else {}})
    img, geo = compose(rows, gap, background)
    for row in geo:
        for g in row["glyphs"]:
            g["design_advance"] = font.advances[g["gid"]]
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue(), geo


def _jsonable(params: dict) -> dict:
    """Engine params for the response: axes as {tag: value}."""
    return {k: dict(v) if k == "axes" else v for k, v in params.items()}


def _namespace(request: Request) -> str:
    client = request.scope["state"].get("client")
    return client[0] if client else "anonymous"


@router.post("/glyphs")
async def glyphs(request: Request):
    """Render glyph ids with a native Windows engine; JSON with a base64 PNG + geometry."""
    global _in_flight
    t0 = time.perf_counter()
    form = await request.form()
    fields = {k: v for k, v in form.items() if isinstance(v, str)}
    uploads = [v for k, v in form.multi_items() if k == "fonts" and isinstance(v, UploadFile)]

    engine_name = fields.get("engine", "")
    if engine_name not in ("gdi", "dwrite"):
        return _error(400, "engine must be gdi or dwrite")
    engine = ENGINES.get(engine_name)
    if engine is None:
        return _error(501, "engine_unavailable", engine=engine_name)
    try:
        params = engine.parse_params(fields)
        gids = parse_gids(fields.get("gids", ""))
        ppems = parse_ppems(fields.get("ppem", "9-24"))
        background = parse_color(fields.get("background", ""), (255, 255, 255))
        ink = parse_color(fields.get("ink", ""), (0, 0, 0))
        gap = int(fields.get("gap", "4"))
        positions = parse_positions(fields, len(gids))
    except ValueError as e:
        return _error(400, "bad_params", detail=str(e))
    layout = fields.get("layout", "cells")
    if layout not in ("cells", "run"):
        return _error(400, "layout must be cells or run")
    if positions is not None and layout != "run":
        return _error(400, "advances and offsets need layout=run")
    if not gids or len(gids) > MAX_GIDS:
        return _error(400, f"1..{MAX_GIDS} gids required")
    if not ppems or len(ppems) > config.MAX_PPEM_VALUES or len(gids) * len(ppems) > MAX_CELLS:
        return _error(400, f"1..{config.MAX_PPEM_VALUES} ppem values, at most {MAX_CELLS} cells")
    if not all(1 <= p <= MAX_PPEM and engine.ppem_ok(p) for p in ppems):
        return _error(400, "bad_ppem", detail=f"1..{MAX_PPEM}"
                      + (", whole pixels for gdi" if engine_name == "gdi" else ""))
    if not 0 <= gap <= MAX_GAP:
        return _error(400, f"gap must be 0..{MAX_GAP}")

    ids = [x.strip().lower() for x in fields.get("font_ids", "").split(",") if x.strip()]
    if len(ids) + len(uploads) != 1:
        return _error(400, "exactly one font required (font_ids or fonts)")
    if ids:
        if not _SHA256_RE.match(ids[0]):
            return _error(400, "font_ids must be sha256 hex")
        entry = request.app.state.font_cache.get(_namespace(request), ids[0])
        if entry is None:
            return _error(409, "missing_fonts", missing=ids)
        data, sha = entry.data, ids[0]
    else:
        data = await uploads[0].read()
        if not data or len(data) > config.MAX_FONT_SIZE:
            return _error(400, "font empty or too large")
        sha = hashlib.sha256(data).hexdigest()

    if _in_flight >= config.CONCURRENCY + config.QUEUE_MAX:
        request.scope["state"]["busy"] = "queue_full"
        resp = _error(429, "busy")
        resp.headers["Retry-After"] = "1"
        return resp
    _in_flight += 1
    loop = asyncio.get_running_loop()
    try:
        try:
            font = await loop.run_in_executor(_executor("prepare"), sfnt.prepare, data, sha)
        except sfnt.FontError:
            return _error(422, "font_unreadable")
        try:
            params = engine.resolve(font, params)
        except ValueError as e:
            return _error(400, "bad_params", detail=str(e))
        if bad := [g for g in gids if g >= font.num_glyphs]:
            return _error(400, "gid_out_of_range", max=font.num_glyphs - 1, invalid=bad[:5])
        try:
            png, rows = await asyncio.wait_for(loop.run_in_executor(
                _executor(engine_name), _render_sync, engine, font, gids, ppems, params,
                layout, gap, background, ink, positions), timeout=config.TIMEOUT_S)
        except asyncio.TimeoutError:
            return _error(504, "timeout")
        except RenderError as e:
            return _error(422, str(e))
    finally:
        _in_flight -= 1

    resp = JSONResponse(content={
        "image": base64.b64encode(png).decode("ascii"),
        "geometry": {
            "engine": engine_name,
            "render_version": engine.render_version,
            "params": {**_jsonable(params), "layout": layout, "gap": gap,
                       **({"positions": "advances" if positions.offsets is None
                           else "advances+offsets"} if positions else {}),
                       "background": "#%02x%02x%02x" % background, "ink": "#%02x%02x%02x" % ink},
            "upem": font.upem,
            "gasp": [{"max_ppem": m, "flags": f} for m, f in font.gasp] if font.gasp is not None else None,
            "rows": rows,
        },
    })
    resp.headers["Server-Timing"] = f"total;dur={(time.perf_counter() - t0) * 1000:.1f}"
    return resp


def info() -> dict:
    """For /info: engines and their render versions."""
    return {name: e.render_version for name, e in ENGINES.items()}

