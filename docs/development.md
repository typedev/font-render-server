# Development

## Setup

```bash
uv sync
uv run playwright install chromium
# Ubuntu newer than Playwright knows about:
PLAYWRIGHT_HOST_PLATFORM_OVERRIDE=ubuntu24.04-x64 uv run playwright install chromium
```

The server runs on Linux too (FreeType instead of DirectWrite) — enough for tests and
development, not for real rendering comparisons.

## Code layout

```
src/font_render/
  __main__.py     entry point: font-render-server [serve] (uvicorn on FONT_RENDER_HOST:PORT,
                  0.0.0.0:8000), install, uninstall, reset-admin, selfcheck, version
  install.py      install / repair / uninstall of the installed layout (service, firewall,
                  ACL, Start menu, Apps & features); side effects through Runner, DryRunner
                  for --dry-run and tests (docs/distribution.md)
  localadmin.py   commands for the server machine: elevation check, reset_admin(), audit log
  app.py          FastAPI app, lifespan (pool warm-up, janitor), /render, /fonts, /info, /health
  admin.py        /admin/* (client tokens, settings), /browsers/install, /update, /rollback
  auth.py         ClientRegistry (clients.json), AuthMiddleware, admin checks, web sessions
  web.py          web admin: /admin/ page, login/logout, /admin/api/*, restart, recycle, clear
  static/         admin.html, admin.css, admin.js (no build step, no external assets)
  stats.py        request stats: recent requests, traffic per client, per-minute load,
                  overload episodes (data/stats.json); sampler() reads the pool every second
  config.py       HOME / APP (checkout vs installed layout), paths, .env loading, limits and env settings; EDITABLE settings with
                  overrides from data/settings.json (set_setting, APPLY_HOOKS)
  pool.py         BrowserPool (browsers per key, queue, per-client limit, recycling),
                  per-browser page cache, render_screenshot()
  font_cache.py   FontCache (RAM, per-client namespaces, LRU + TTL)
  page.py         HTML templates, @font-face generation, build_page() -> PageParts
  geometry.py     JS that collects per-glyph boxes and baselines after the screenshot
  winenv.py       Windows build, DLL versions, SPI / ClearType Tuner / DWrite settings (/info)
  dwrite.py       DirectWrite through ctypes: COM vtable calls, isolated factory
  glyphs.py       POST /glyphs: parameters, one worker thread per engine, compose() layout
  engines.py      engine protocol, Cell (glyph ink + offset from the pen origin), RenderError
  gdi.py          GDI engine: AddFontMemResourceEx, ExtTextOutW(ETO_GLYPH_INDEX) into a DIB
  dwrite_engine.py DirectWrite engine: in-memory loader, custom rendering params, bitmap render
                  target; vtable indices checked with QueryInterface on every object
  sfnt.py         prepare(): family renamed to F_<sha[:16]>, STAT dropped, metrics, cache
client/           font-render-client: its own package (dependency: requests), a uv
                  workspace member the server package depends on, published to PyPI
  src/font_render_client/
    client.py       font-render (render, --info, --update/--rollback, --deploy/--release)
    login.py        font-render login | logout (config file)
    _env.py         environment < ./.env < repository .env < config file
    batch_render.py font-render-batch
    manage_clients.py font-render-clients
    deploy.py       packs a git ref / the working tree for developer-mode deploys
src/font_render_tools/
  pixel_check.py  pixel-check (renders in-process with the server package)
tests/            pytest suite
deploy/windows/   launcher.py (service launcher of the installed layout: APP\current, trial
                  starts, auto-revert), reset-admin.cmd, uninstall.cmd (self-elevating);
                  setup_service.bat, start_server.bat (checkout install)
scripts/          release_key.py (release signing key, docs/releasing.md)
```

Checkout vs installed layout: in a checkout `config.HOME` (data: `.env`, `clients.json`,
`data/`, `logs/`) is the repository root, as before. An installed version
(`APP\versions\<v>`) finds `APP`, and `HOME` through the `APP\home` pointer file;
`FONT_RENDER_HOME` / `FONT_RENDER_APP` override both. Try the installer anywhere with
`uv run font-render-server install --dry-run --app /tmp/a --home /tmp/h --source .`

Conventions:

- Modules read settings as `config.NAME` at call time (never `from .config import NAME`),
  so tests can monkeypatch them.
- asyncio locks are recreated per app lifetime (`pool.reset_locks()` in the lifespan).
- No font names, hashes or sizes in logs.

## How a render works

1. `AuthMiddleware` resolves the client from the Bearer token (before the body is read).
2. `/render` collects fonts (cache by `font_ids`, then uploads) and builds the page
   (`page.build_page`): `<style id="ff">` with `@font-face` (fonts as base64 data URIs,
   family names `F<i>_<sha256[:16]>`), `<style id="st">` with the rest of the CSS, the body.
3. `BrowserPool.acquire` waits for a slot (global concurrency, bounded queue, per-client
   limit) and picks a browser — preferring one that already has these fonts loaded.
4. `render_screenshot` either reuses a cached page (swaps only `#st` and the body) or opens
   a new context, loads every `@font-face` explicitly (`FontFace.load()`; `document.fonts.ready`
   alone let fresh pages be shot with the fallback font). A face that fails is logged (position,
   status, error; never the name) and raises `FontNotLoaded` (422). Then it fits the viewport to the content,
   screenshots, and optionally collects geometry.
5. Zoom (Pillow nearest-neighbor) and the response with `Server-Timing`.

## Tests

```bash
uv run pytest
```

| File | Covers |
|------|--------|
| `test_geometry.py` | geometry mode, pixel identity with/without geometry |
| `test_staleness.py` | an edited font (same name, new bytes) is never rendered stale |
| `test_pool.py` | queue/429, timeout/504, recycling, crash replacement (fake browser) |
| `test_page_reuse.py` | reused pages are byte-identical to fresh ones |
| `test_auth.py` | tokens, scopes, anonymous switch, legacy token, reload |
| `test_font_cache.py` | `/fonts`, `font_ids`, namespaces, TTL/LRU, per-client limit |
| `test_web_admin.py` | web admin: login/session cookie, CSRF origin check, lockout guards, status/requests/logs, maintenance actions |
| `test_settings.py` | settings editable in the web admin: validation, overrides in `data/settings.json`, live apply |
| `test_stats.py` | traffic per client, per-minute load, overload episodes, persistence |
| `test_glyphs.py` | `/glyphs` with a fake engine: parameters, cell independence, determinism, run layout; `sfnt.prepare` touches only `name`/`head` |

Tests use a system TTF (skip if none) and `fonttools` (dev dependency) to build an
unhinted copy.

## Pixel identity

Any change to the server must leave rendered pixels byte-identical unless that is the
point of the change. Capture a baseline before, compare after:

```bash
uv run pixel-check capture --fonts renders/pixel_fonts/*                    # in-process
uv run pixel-check compare --fonts renders/pixel_fonts/*
uv run pixel-check capture --server http://192.168.1.3:8000 --fonts ...     # a running server
uv run pixel-check compare --server http://192.168.1.3:8000 --fonts ...
```

It renders a fixed matrix (each font and all together × ClearType on/off × scale 1/1.5 ×
plain/full × geometry on/off) and compares sha256 digests. Baselines are stored in
`renders/pixel_baseline/` (platform-specific, not committed); fonts are not in the repo.

If a change does alter pixels (launch flags, page HTML/CSS, Chromium/Playwright upgrade),
bump `RENDER_VERSION` in `config.py` — clients key their render caches on it.

## Benchmarks

Measured on the Windows host (Chromium, ClearType on), 20 fonts / 5.3 MB, one character,
ppem 12, geometry:

| | p50 latency | throughput |
|---|---|---|
| single context per request (before the pool) | 1099 ms | 0.9 req/s sequential, ~2 req/s at c=4 |
| pool + page reuse, fonts uploaded | 228 ms | ~7.8 req/s at c=2–4 |
| pool + page reuse, `font_ids` | 68 ms | — |

Ad-hoc benchmark scripts (`benchmark*.py`) are kept out of the repo (gitignored).
