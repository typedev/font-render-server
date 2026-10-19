# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Font Render Service — a Windows server renders fonts (TTF, OTF, WOFF, WOFF2) in headless
Chromium (DirectWrite rasterizer) and returns PNG screenshots, optionally with per-glyph
geometry, over HTTP. Clients run anywhere: the `font-render*` CLI tools in this repo,
hintbench (`../hintbench`), and the Telegram bot (separate repo `../font-render-bot`).

Docs: `README.md` (overview, commands), `docs/api.md`, `docs/auth.md`, `docs/admin.md`,
`docs/deployment.md`, `docs/development.md` (code layout, render pipeline, pixel identity),
`docs/distribution.md` (installer, release updates, developer mode — design, history),
`docs/releasing.md` (release signing key, release steps).
Keep them in sync with code changes.

## Commands

```bash
uv sync && uv run playwright install chromium     # setup (Ubuntu 26.04: PLAYWRIGHT_HOST_PLATFORM_OVERRIDE=ubuntu24.04-x64)
uv run pytest                                     # tests (tests/)
uv run font-render-server                         # run the server
uv run font-render FONTS... [--plain --zoom 4]    # render via the server in .env
uv run font-render --info | --update | --rollback # server info / newest release / previous version
uv run font-render --deploy [REF] | --deploy --local | --release   # developer mode: deploy to lurkbox
uv run font-render-clients list|add|rotate|...    # client tokens (admin)
uv run pixel-check capture|compare --fonts renders/pixel_fonts/* [--server URL]
```

`.env` (project root, auto-loaded by server and tools): `FONT_RENDER_SERVER`,
`FONT_RENDER_CLIENT_TOKEN` (Bearer), `FONT_RENDER_ADMIN_TOKEN` (admin Bearer, for --update etc.),
`FONT_RENDER_DEV_TOKEN` (developer-mode client `alexander`, for --deploy / --release),
`FONT_RENDER_TOKEN` (legacy admin form token; legacy_admin_token is off on lurkbox).
No linter or build step beyond `uv sync`.

## Layout

- `src/font_render/` — server package: `app.py` (FastAPI, lifespan, /render, /fonts, /info,
  /health), `admin.py` (/admin/*, /update, /rollback, /browsers/install), `auth.py`
  (clients.json, `AuthMiddleware`), `pool.py` (`BrowserPool`, page cache,
  `render_screenshot`), `font_cache.py`, `page.py` (templates, `build_page`), `geometry.py`,
  `config.py` (paths, env, limits), `web.py` + `static/` (web admin at `/admin/`),
  `stats.py` (traffic per client, per-minute load, overload episodes → `data/stats.json`),
  `winenv.py` (Windows build, DLL versions, SPI/ClearType/DWrite settings for /info),
  `dwrite.py` (DirectWrite via ctypes COM vtables), `glyphs.py` (POST /glyphs, native engines),
  `engines.py`, `gdi.py`, `dwrite_engine.py`, `sfnt.py` (in-memory family rename for GDI),
  `install.py` (install/repair/uninstall of the installed layout), `localadmin.py`
  (reset-admin, audit log), `updates.py` (release updates, job, rollback), `devmode.py`
  (developer mode, /dev/*), `release_key.py` (TRUSTED_KEYS).
- `client/` — `font-render-client` package (uv workspace member, only `requests`): `font-render`,
  `-batch`, `-clients`, `login`; `deploy.py` packs dev deploys. Keep its version = the server's.
- `src/font_render_tools/` — `pixel-check` (needs the server package).
- `tests/` — pytest; `deploy/windows/` — launcher.py + .cmd wrappers (installed layout),
  checkout service installer; `install.ps1`, `scripts/` (build_release, smoke_test, release_key),
  `.github/workflows/release.yml` (Windows smoke test; sign/publish only in the public repo).

## Rules

- **Pixel identity**: server changes must keep PNGs byte-identical — run `pixel-check compare`
  (local, and `--server` after deploy; on Windows it also covers /glyphs per engine) against a
  baseline captured before the change. If pixels are meant to change (launch flags, page HTML/CSS, Chromium/Playwright upgrade), bump
  `RENDER_VERSION` in `config.py` (native engines: their `render_version` attribute); hintbench
  keys its cache on it.
- **Font hygiene**: fonts are unreleased — never write them to disk (uploads stay in memory:
  `MultiPartParser.spool_max_size` is raised in `app.py`), never log font names, hashes or sizes.
- **Staleness**: `@font-face` families embed a content hash (`F<i>_<sha256[:16]>`); keep it so
  an edited font is never served from a reused page (guarded by `test_staleness.py`).
- Settings are read as `config.NAME` at call time so tests can monkeypatch them and the web
  admin can change them live (`config.EDITABLE`, overrides in `data/settings.json`); asyncio
  locks are recreated per app lifetime (`pool.reset_locks()`).
- Run the tests before deploying. A deploy runs `selfcheck` (imports every module) before it
  switches, and a build that fails to start 3 times is reverted by the launcher — but a build
  that starts and renders wrong pixels is only caught by `pixel-check compare --server`.
- Windows specifics (all found the hard way, see git log): quote paths with spaces in NSSM
  parameters; ASCII-only console output; no `icacls /T` with grants; no detached processes.
- Changes to install/update/uninstall: run the Release workflow by hand (Actions → Release →
  Run workflow; `gh workflow run release.yml`) — its Windows smoke test covers the whole cycle.

## Deployment

Repository: public `typedev/font-render-server` (since 2026-10-19). It started as a fresh
copy of the private `typedev/wintt-server-render` (archived): that history names font files —
never publish it, never push it here. Releases: tag `vX.Y.Z` → docs/releasing.md.

The server host is `lurkbox` (192.168.1.3 / lurkbox.local), Windows 11, installed layout since
2026-10-19 (`C:\Program Files\FontRender`, data `C:\ProgramData\FontRender`), NSSM service
`FontRenderService` running as LocalSystem. Web admin: `http://lurkbox.local:8000/admin/`.
Developer mode is on there (client `alexander`, no expiry, 192.168.1.0/24).
Deploy = commit (push optional), then `uv run font-render --deploy` (origin/main; push first)
or `--deploy --local` (working tree) → job: uv sync, Chromium, selfcheck, switch, restart;
then `pixel-check compare --server`. Back to the release: `--release`. The old checkout
`C:\WORK\wintt-server-render` is no longer used by the service (fallback only). Anonymous
rendering is off: every client needs a token (`docs/auth.md`).
