# Font Render Service

A client-server tool for comparing font rendering with the Windows DirectWrite rasterizer.
The server runs on Windows, renders fonts (TTF, OTF, WOFF, WOFF2) in headless Chromium and
returns PNG screenshots, optionally with per-glyph geometry. Clients run on any machine on
the network.

```
┌────────────────────────┐        HTTP + Bearer token       ┌──────────────────────────────┐
│  Clients (any OS)      │  ── POST /fonts, /render ──────▶ │  Windows (server)            │
│  font-render (CLI)     │  ◀─ PNG / JSON (+geometry) ───── │  FastAPI + Playwright        │
│  hintbench             │                                  │  pool of headless Chromium   │
│  font-render-bot       │                                  │  DirectWrite rasterization   │
│  (Telegram, own repo)  │                                  │  NSSM service                │
│                        │                                  │                              │
│  Admin: font-render,   │  ── POST /update (admin token) ▶ │  signed release from GitHub  │
│  web admin /admin/     │  ◀─ verify, install, restart ─── │                              │
└────────────────────────┘                                  └──────────────────────────────┘
```

The Telegram bot lives in its own repository
([font-render-bot](https://github.com/typedev/font-render-bot)) and runs on the server host as
a separate service with its own updates; for the server it is just another render client
with its own token. Admin calls (`/update`, `/admin/*`) come from `font-render` and the web
admin with an admin token.

## Quick start

**Server (Windows)** — one command in PowerShell (see [docs/deployment.md](docs/deployment.md),
which also covers the git checkout install for development):

```powershell
irm https://github.com/typedev/font-render-server/releases/latest/download/install.ps1 | iex
```

It prints the web admin address and the admin token. Repair or update: run it again.
Remove: Apps & features → Font Render Service. Lost token: Start → Font Render → Reset
admin token.

**Client (any OS)** — the `font-render-client` package ([client/README.md](client/README.md)):

```bash
uv tool install font-render-client           # from PyPI (or: pipx install font-render-client)
font-render login http://SERVER:8000          # asks for the token you got from the server's admin
font-render font_a.ttf font_b.otf             # comparison waterfall -> ./renders/
font-render font.ttf --plain --zoom 4         # text only, 4x nearest-neighbor
font-render --info                            # server info + which client you are
```

**From a checkout of this repository** (development; tokens in `.env` instead of `login`):

```bash
uv sync
cp .env.example .env   # FONT_RENDER_SERVER, FONT_RENDER_CLIENT_TOKEN (and FONT_RENDER_ADMIN_TOKEN for admin)
uv run font-render --info
# web admin: http://SERVER:8000/admin/ (log in with an admin token)
```

## Commands

Installed with the client package the commands run as they are; in a checkout prefix them
with `uv run`.

| Command | What it does |
|---------|--------------|
| `font-render login [SERVER] [--token T] [--admin-token T] [--dev-token T]` / `--show` / `logout` | Save, show, delete the server address and tokens ([client/README.md](client/README.md)) |
| `font-render FONTS... [options]` | Render fonts, save PNG to `./renders/` (`-o DIR`) |
| `font-render --ping` / `--info` | Server status / full info and which client you are |
| `font-render --update` / `--rollback` | Installed server: newest release / previous version; checkout server: git pull / previous commit (admin) |
| `font-render --install-browser NAME` | Install a Playwright browser on the server (admin) |
| `font-render --deploy [REF]` / `--deploy --local` / `--release` | Developer mode: deploy a commit / the working tree / back to a release ([docs/deployment.md](docs/deployment.md#developer-mode)) |
| `font-render-batch SRC_DIR OUT_DIR` | Render font pairs from two directories, all browser × ClearType combinations |
| `font-render-clients list\|add\|rotate\|disable\|enable\|remove\|settings` | Manage client tokens (admin), see [docs/auth.md](docs/auth.md) |
| `uv run pixel-check capture\|compare --fonts ...` | Byte-identity check of render output (checkout), see [docs/development.md](docs/development.md) |
| `font-render-server [serve\|install\|uninstall\|reset-admin\|dev-mode\|selfcheck\|version]` | The server and its local admin commands (Windows; installed: `bin\font-render-server.cmd`) |

Common options: `--server URL` / `-s` (default `FONT_RENDER_SERVER`), `--token` (legacy admin
form token of old checkout servers).

Render options of `font-render`:

| Option | Value | Description |
|--------|-------|-------------|
| `--ppem` | `9-24` or `9,12,16,20` | PPEM range or list for the waterfall |
| `--text` | string | Sample text |
| `--scale` | `1.0` / `1.5` / `2.0` | Device scale factor (1.0 = 96 DPI) |
| `--no-cleartype` | flag | Grayscale antialiasing instead of ClearType |
| `--width` | pixels | Viewport width |
| `--browser` | `chromium` / `firefox` / `webkit` | Browser engine (Firefox: grayscale only; see [docs/api.md](docs/api.md#browsers)) |
| `--colors` | `#hex,#hex,...` | Color per font |
| `--bg` | `#hex` | Page background (default white; on a dark one the text defaults to white) |
| `--invert` | flag | White text on black (reverse video) |
| `--plain` | flag | Text only, no header or labels |
| `--zoom` | 1–8 | Nearest-neighbor upscale after rendering |
| `--open` | flag | Open the result in an image viewer |

## Documentation

- [docs/api.md](docs/api.md) — HTTP API: `/render`, `/fonts`, geometry mode, errors, `Server-Timing`
- [docs/auth.md](docs/auth.md) — client tokens: issuing, rotating, closing anonymous access
- [docs/admin.md](docs/admin.md) — web admin: status, clients, requests, logs, maintenance
- [docs/deployment.md](docs/deployment.md) — Windows server setup, service, updates, logs, configuration
- [docs/development.md](docs/development.md) — code layout, tests, pixel identity, benchmarks
- [docs/releasing.md](docs/releasing.md) — release signing key, making a release
- [docs/distribution.md](docs/distribution.md) — how install, release updates, uninstall and developer mode work (design)
- [CHANGELOG.md](CHANGELOG.md) — releases
- [TODO.md](TODO.md) — planned work

## Project layout

```
src/font_render/         server package (FastAPI app, auth, browser pool, font cache, page builder)
client/                  font-render-client package (font-render, -batch, -clients, login)
src/font_render_tools/   pixel-check (needs the server package)
tests/                   pytest suite
install.ps1              one-command installer (published with each release)
deploy/windows/          launcher.py, font-render-server.cmd, reset-admin.cmd, uninstall.cmd
                         (installed layout); setup_service.bat, start_server.bat (checkout)
scripts/                 build_release.py, smoke_test.ps1 (CI), release_key.py (signing key)
.github/workflows/       release.yml: tests, build, Windows smoke test, sign, publish
docs/                    documentation
```
