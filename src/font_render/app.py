"""FastAPI app: render, font cache and info endpoints (admin ones live in admin.py)."""

import asyncio
import base64
import hashlib
import io
import os
import re
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from PIL import Image
from starlette.formparsers import MultiPartParser

from . import __version__, admin, auth, config, devmode, glyphs, page, pool, sfnt, updates, web, winenv
from .stats import sampler, stats
from .font_cache import FontCache
from .geometry import scale_geometry

# Keep uploaded fonts in memory: Starlette spools multipart parts larger than
# 1 MB to a temp file on disk by default. Fonts are unreleased — never on disk.
MultiPartParser.spool_max_size = config.MAX_FONT_SIZE + 1024 * 1024


def _set_spool(max_font_size: int):
    MultiPartParser.spool_max_size = max_font_size + 1024 * 1024


config.APPLY_HOOKS["MAX_FONT_SIZE"] = [_set_spool]
config.APPLY_HOOKS["FONT_CACHE_MAX_BYTES"] = [lambda v: _font_cache.resize(v)]
config.APPLY_HOOKS["FONT_CACHE_TTL_S"] = [lambda v: setattr(_font_cache, "ttl_s", v)]

_pool = pool.BrowserPool()
_font_cache = FontCache(config.FONT_CACHE_MAX_BYTES, config.FONT_CACHE_TTL_S)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _pool, _font_cache
    pool.reset_locks()
    _pool = pool.BrowserPool()
    _font_cache = FontCache(config.FONT_CACHE_MAX_BYTES, config.FONT_CACHE_TTL_S)
    app.state.pool = _pool  # for the web admin (web.py)
    app.state.font_cache = _font_cache
    stats.load()
    warm_up = asyncio.create_task(_pool.warm_up())
    background = [
        warm_up,
        asyncio.create_task(_pool.janitor(_font_cache)),
        asyncio.create_task(sampler(_pool)),
        asyncio.create_task(updates.mark_healthy(warm_up)),
        asyncio.create_task(updates.checker()),
    ]
    yield
    stats.save()
    for task in background:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    await _pool.close()
    await pool.stop_playwright()


app = FastAPI(
    title="Font Render Service",
    description="Render TrueType font comparisons using Windows DirectWrite",
    lifespan=lifespan,
)
app.add_middleware(auth.AuthMiddleware)
app.include_router(admin.router)
app.include_router(web.router)
app.include_router(glyphs.router)
app.include_router(devmode.router)


def _apply_zoom(png_bytes: bytes, zoom: int) -> bytes:
    """Upscale a PNG image by an integer factor using nearest-neighbor."""
    img = Image.open(io.BytesIO(png_bytes))
    img = img.resize((img.width * zoom, img.height * zoom), Image.NEAREST)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@app.get("/")
async def root():
    git = await admin.git_info()
    return {
        "service": "Font Render Service",
        "version": git["commit"],
        "branch": git["branch"],
        "commit_date": git["commit_date"],
        "endpoints": {
            "GET /info": "Full server information (browsers, render defaults)",
            "POST /render": "Render fonts as interleaved waterfall (returns PNG)",
            "POST /browsers/install": "Install a Playwright browser (requires token)",
            "POST /update": "Pull latest code and restart (requires token)",
            "POST /rollback": "Rollback to previous version (requires token)",
            "POST /fonts": "Cache fonts in RAM for this client, returns sha256 ids",
            "DELETE /fonts": "Drop this client's cached fonts",
            "GET /health": "Health check",
            "GET /admin/": "Web admin: status, clients, requests, logs, maintenance",
            "POST /admin/clients/{list,add,rotate,disable,enable,remove}": "Manage client tokens (admin)",
            "POST /admin/settings": "allow_anonymous_render, legacy_admin_token (admin)",
        },
    }


@app.get("/info")
async def info(request: Request):
    """Full server information: version, platform, browsers, render defaults."""
    import platform
    git = await admin.git_info()
    installed = await pool.browsers_installed()
    live = _pool.live_browsers()
    active = {f"{name}(ct={ct})": True for (name, ct) in live}
    browser_version = {name: b.version for (name, _ct), b in live.items()}
    ns = _namespace(request)
    return {
        "service": "Font Render Service",
        "api": 3,
        "version": git["commit"],
        "release": __version__,
        "layout": "checkout" if config.APP is None else "installed",
        "dev_mode": {k: v for k, v in devmode.view().items() if k in ("enabled", "until")},
        "branch": git["branch"],
        "commit_date": git["commit_date"],
        "render_version": config.RENDER_VERSION,
        "engines": ["chromium", *glyphs.ENGINES],
        "render_versions": {"chromium": config.RENDER_VERSION, **glyphs.info()},
        "windows": await asyncio.to_thread(winenv.info),
        "browser_version": browser_version,
        "platform": platform.platform(),
        "python": platform.python_version(),
        "browsers": {
            "supported": list(config.SUPPORTED_BROWSERS),
            "installed": installed,
            "active": active,
        },
        "pool": _pool.info(),
        "font_cache": {
            "enabled": True,
            "max_bytes": config.FONT_CACHE_MAX_BYTES,
            "ttl_s": config.FONT_CACHE_TTL_S,
            "client": _font_cache.namespace_stats(ns),
        },
        "limits": {
            "max_fonts": config.MAX_FONTS,
            "max_font_size": config.MAX_FONT_SIZE,
            "max_ppem_values": config.MAX_PPEM_VALUES,
        },
        "auth": {
            "required": not auth.registry.settings().get("allow_anonymous_render", True),
            "client": (request.scope["state"].get("client") or (None,))[0],
        },
        "render": {
            "max_fonts": config.MAX_FONTS,
            "default_ppem": "9-24",
            "default_text": "Hamburgefontsiv ABCDEFGHIJKLMNOPQRSTUVWXYZ 0123456789",
            "default_width": 1920,
            "default_scale": 1.0,
            "geometry": True,
        },
    }



@app.get("/health")
async def health():
    git = await admin.git_info()
    return {"status": "ok", "version": git["commit"], "release": __version__}


_TIMING_PHASES = ("queue", "fonts", "load", "shot", "geom")


def _timed(timing: dict[str, float], response: Response) -> Response:
    """Attach a Server-Timing header (ms) with the measured render phases."""
    parts = [f"{k};dur={timing[k] * 1000:.1f}" for k in _TIMING_PHASES if k in timing]
    parts.append(f"total;dur={(time.perf_counter() - timing['_start']) * 1000:.1f}")
    if "_state" in timing and "queue" in timing:
        timing["_state"]["queue_s"] = timing["queue"]  # for stats (AuthMiddleware)
    if "load" in timing:
        parts.append(f'page;desc="{"reuse" if timing.get("_reused") else "new"}"')
    response.headers["Server-Timing"] = ", ".join(parts)
    return response


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _namespace(request: Request) -> str:
    """Font-cache namespace: the client id (anonymous callers share one)."""
    client = request.scope["state"].get("client")
    return client[0] if client else "anonymous"


async def _read_uploads(fonts: list[UploadFile]) -> list[tuple[str, bytes]] | JSONResponse:
    """(filename, bytes) per upload, or a 400 response if one is empty or too large."""
    out = []
    for i, upload in enumerate(fonts, 1):
        data = await upload.read()
        if not data:
            return JSONResponse(status_code=400, content={"error": f"Font #{i} is empty"})
        if len(data) > config.MAX_FONT_SIZE:
            return JSONResponse(
                status_code=400,
                content={"error": f"Font #{i} exceeds {config.MAX_FONT_SIZE // 1024 // 1024}MB limit"},
            )
        out.append((upload.filename or f"font_{i}.ttf", data))
    return out


def _resolve_variations(spec: str, named: list[tuple[str, bytes]]) -> list[dict | None] | JSONResponse:
    """Per font: None (no variation CSS) or every fvar axis at the requested/default value."""
    if not spec.strip():
        return [None] * len(named)
    entries = spec.split(";")
    if len(entries) > len(named):
        return JSONResponse(status_code=400, content={
            "error": "bad_variations", "detail": f"{len(entries)} locations for {len(named)} fonts"})
    entries += [""] * (len(named) - len(entries))
    out = []
    for i, (entry, (_name, data)) in enumerate(zip(entries, named), 1):
        if not entry.strip():
            out.append(None)
            continue
        try:
            axes = sfnt.font_axes(data, hashlib.sha256(data).hexdigest())
            if not axes:
                raise ValueError("not a variable font")
            out.append(sfnt.resolve_location(axes, sfnt.parse_location(entry)))
        except (ValueError, sfnt.FontError) as e:
            return JSONResponse(status_code=400, content={
                "error": "bad_variations", "font": i, "detail": str(e)})
    return out


@app.post("/fonts")
async def upload_fonts(
    request: Request,
    fonts: list[UploadFile] = File(..., description="Fonts to keep in the RAM cache"),
):
    """Cache fonts for later /render calls by font_ids (sha256), in upload order."""
    if not 1 <= len(fonts) <= config.MAX_FONTS:
        return JSONResponse(status_code=400, content={"error": f"1..{config.MAX_FONTS} fonts required"})
    uploads = await _read_uploads(fonts)
    if isinstance(uploads, JSONResponse):
        return uploads
    ns = _namespace(request)
    return {"fonts": [
        {"sha256": _font_cache.put(ns, data, name), "size": len(data)} for name, data in uploads
    ]}


@app.delete("/fonts")
async def delete_fonts(request: Request):
    """Drop all cached fonts of the calling client."""
    return {"deleted": _font_cache.drop_namespace(_namespace(request))}


@app.post("/render")
async def render(
    request: Request,
    fonts: list[UploadFile] | None = File(None, description="Font files (TTF/OTF/WOFF/WOFF2)"),
    font_ids: str = Form("", description="Comma-separated sha256 of fonts cached via POST /fonts; "
                                         "rendered first, then uploaded files"),
    ppem: str = Form("9-24", description="PPEM range: '9-24' or '9,12,16,20'"),
    text: str = Form(
        "Hamburgefontsiv ABCDEFGHIJKLMNOPQRSTUVWXYZ 0123456789",
        description="Sample text to render",
    ),
    scale: float = Form(1.0, description="Device scale factor (1.0=96dpi, 1.5=144dpi, 2.0=192dpi)"),
    cleartype: bool = Form(True, description="Enable ClearType/LCD rendering"),
    width: int = Form(1920, description="Viewport width in pixels"),
    colors: str = Form("", description="Comma-separated colors per font (default: black; white on a dark background)"),
    background: str = Form("", description="Page background, #rgb or #rrggbb (default: white)"),
    browser: str = Form("chromium", description="Browser engine: chromium, firefox, or webkit"),
    layout: str = Form("full", description="Layout: 'full' (header+labels) or 'plain' (text only)"),
    zoom: int = Form(1, description="Post-render nearest-neighbor upscale factor (1=no zoom)"),
    delay_font: int = Form(0, description="Delay (ms) after the fonts are loaded"),
    delay_resize: int = Form(0, description="Delay (ms) after viewport resize"),
    geometry: bool = Form(False, description="Return JSON with base64 PNG + per-glyph geometry"),
    line_height: float = Form(0, description="CSS line-height for rows (0=browser default)"),
    variations: str = Form("", description="Variable-font location per font, ';'-separated in font "
                                           "order, e.g. 'wght=350.5,wdth=87;;wght=700' (empty = none)"),
):
    timing: dict = {"_start": time.perf_counter(), "_state": request.scope["state"]}
    fonts = fonts or []
    ids = [x.strip().lower() for x in font_ids.split(",") if x.strip()]
    if len(ids) + len(fonts) < 1:
        return _timed(timing, JSONResponse(
            status_code=400,
            content={"error": "At least 1 font required"},
        ))
    if len(ids) + len(fonts) > config.MAX_FONTS:
        return _timed(timing, JSONResponse(
            status_code=400,
            content={"error": f"Too many fonts (max {config.MAX_FONTS})"},
        ))

    ppem_values = page.parse_ppem_range(ppem)
    if len(ppem_values) > config.MAX_PPEM_VALUES:
        return _timed(timing, JSONResponse(
            status_code=400,
            content={"error": f"Too many PPEM values (max {config.MAX_PPEM_VALUES})"},
        ))

    # Validate layout
    if browser not in config.SUPPORTED_BROWSERS:
        return _timed(timing, JSONResponse(
            status_code=400,
            content={"error": f"browser must be one of {', '.join(config.SUPPORTED_BROWSERS)}"},
        ))

    if layout not in ("full", "plain"):
        return _timed(timing, JSONResponse(
            status_code=400,
            content={"error": "layout must be 'full' or 'plain'"},
        ))

    # Clamp zoom to 1–8
    zoom = max(1, min(zoom, 8))

    if bad := [x for x in ids if not _SHA256_RE.match(x)]:
        return _timed(timing, JSONResponse(
            status_code=400,
            content={"error": "font_ids must be sha256 hex", "invalid": bad[:5]},
        ))

    # Fonts: cached (font_ids) first, then uploaded files
    ns = _namespace(request)
    named: list[tuple[str, bytes]] = []
    missing = []
    for sha in ids:
        entry = _font_cache.get(ns, sha)
        if entry is None:
            missing.append(sha)
        else:
            named.append((entry.filename or sha[:12], entry.data))
    if missing:
        return _timed(timing, JSONResponse(
            status_code=409,
            content={"error": "missing_fonts", "missing": list(dict.fromkeys(missing))},
        ))
    uploads = await _read_uploads(fonts)
    if isinstance(uploads, JSONResponse):
        return _timed(timing, uploads)
    named += uploads

    # Parse colors
    bg_color = background.strip() or "white"
    if bg_color != "white" and not page.BG_COLOR_RE.match(bg_color):
        return _timed(timing, JSONResponse(
            status_code=400, content={"error": "background must be #rgb or #rrggbb"}))
    dark = bg_color != "white" and page.is_dark(bg_color)
    if colors.strip():
        color_list = [c.strip() for c in colors.split(",")]
    else:
        color_list = ["#ffffff" if dark else "#000000"] * len(named)

    locations = _resolve_variations(variations, named)
    if isinstance(locations, JSONResponse):
        return _timed(timing, locations)

    font_names = [name for name, _ in named]
    font_data = [(os.path.splitext(name)[1].lower() or ".ttf", data) for name, data in named]

    parts = page.build_page(
        ppem_values=ppem_values,
        text=text,
        font_names=font_names,
        font_data=font_data,
        colors=color_list,
        bg_color=bg_color,
        cleartype=cleartype,
        scale=scale,
        browser=browser,
        layout=layout,
        zoom=zoom,
        line_height=line_height,
        variations=locations,
    )
    timing["fonts"] = time.perf_counter() - timing["_start"]

    try:
        page_key = (scale, parts.fonts_key)
        async with _pool.acquire(browser, cleartype, timing, page_key, ns) as slot:
            try:
                screenshot, geo_rows = await asyncio.wait_for(
                    pool.render_screenshot(
                        parts, scale, width, slot,
                        delay_font=max(0, delay_font), delay_resize=max(0, delay_resize),
                        collect_geometry=geometry, timing=timing,
                    ),
                    timeout=config.TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                slot.retiring = True  # a hung browser is not reused
                return _timed(timing, JSONResponse(status_code=504, content={"error": "timeout"}))
            except pool.FontNotLoaded as e:
                return _timed(timing, JSONResponse(
                    status_code=422, content={"error": "font_not_loaded", "fonts": e.indices}))
    except pool.PoolBusy as busy:
        request.scope["state"]["busy"] = busy.reason
        resp = JSONResponse(status_code=429, content={"error": "busy"})
        resp.headers["Retry-After"] = str(busy.retry_after)
        return _timed(timing, resp)

    if zoom > 1:
        t = time.perf_counter()
        screenshot = _apply_zoom(screenshot, zoom)
        timing["shot"] += time.perf_counter() - t

    if geometry:
        geo_rows = scale_geometry(geo_rows or [], float(zoom))
        return _timed(timing, JSONResponse(content={
            "image": base64.b64encode(screenshot).decode("ascii"),
            "geometry": {
                "device_scale": scale,
                "zoom": zoom,
                "rows": geo_rows,
                **({"fonts": [
                    {"index": i, "sha256": hashlib.sha256(data).hexdigest(), "variations": loc}
                    for i, ((_n, data), loc) in enumerate(zip(named, locations), 1)
                ]} if variations.strip() else {}),
            },
        }))

    return _timed(timing, Response(content=screenshot, media_type="image/png"))
