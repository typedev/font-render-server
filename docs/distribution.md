# Distribution: install, update, uninstall, developer mode

Design and implementation plan (status: **planned**, 2026-10-18). Goal: a colleague or
another type studio installs the server on a Windows machine with one command, without
Git, Python or manual configuration; updates, rollback and a clean uninstall work from
the web admin and "Apps & features"; a lost admin token can be re-issued locally; and we
keep full remote access to our own instance (lurkbox) through an explicit, visible,
expiring developer mode instead of a `git pull` endpoint.

Today's flow (`docs/deployment.md`) stays valid for lurkbox until step 4 migrates it.

## Two repositories

| Repository | Visibility | Role |
|---|---|---|
| `typedev/wintt-server-render` | private, archived | development until 2026-10-19 |
| `typedev/font-render-server` | **public** | the project since then: code, releases, issues |

The private history names font files, so it is never published. When steps 1–5 are done
here, the current tree is imported into `font-render-server` as its first commit (no
history), development continues there, lurkbox and the clients point to it, and this
repository is archived. Releases are built and signed by the public repository's own
workflow, from the public source — anyone can check what a release contains.

## Overview

```
   font-render-server ── tag vX.Y.Z ──► release workflow (build, sign, smoke test)
                 GitHub Release vX.Y.Z
                 ├─ install.ps1
                 ├─ font-render-X.Y.Z.zip      code + uv.lock + nssm.exe + launcher
                 ├─ SHA256SUMS
                 └─ SHA256SUMS.sig             Ed25519 signature (release key)
                          │
   irm …/releases/latest/download/install.ps1 | iex      (first install, repair)
                          │
                          ▼
   C:\Program Files\FontRender\      code (replaceable)
   C:\ProgramData\FontRender\        data (kept across updates and reinstalls)
                          │
   web admin → "Update to X.Y.Z"     (release channel, signed)
   dev token → /dev/deploy (upload of a tree)   (developer mode only, off by default)
```

Principles:

- **Code and data are separate.** Updating, rolling back, repairing or reinstalling never
  touches client tokens, settings or statistics.
- **One code path.** First install, repair and update all go through the same staging
  pipeline (download → verify → `uv sync --frozen` → browsers → self-check → switch).
- **Versions are directories.** Switching or rolling back is a pointer change, not a
  `git reset`. A version that crash-loops after a switch is reverted automatically.
- **Network tokens never gain code execution.** Release updates only install signed
  releases. Arbitrary code (a git ref or an uploaded tree) needs developer mode, which only
  a local Windows administrator can switch on.
- **Pixel identity per release.** A release pins Python, Playwright and therefore the
  Chromium build via `uv.lock`; every install of vX.Y.Z renders the same pixels.

## On-disk layout

```
C:\Program Files\FontRender\                APP   (FONT_RENDER_APP)
  bin\
    uv.exe                                  pinned uv, used for installs and updates
    nssm.exe                                from the release zip (no nssm.cc download)
    launcher.py                             stdlib-only supervisor run by NSSM
    reset-admin.cmd, uninstall.cmd          self-elevating wrappers
  python\                                   uv-managed CPython (UV_PYTHON_INSTALL_DIR)
  versions\
    1.2.0\                                  release: src\, pyproject.toml, uv.lock, .venv\
    1.3.0\
    dev-a1b2c3d\                            developer-mode build (git ref)
    dev-upload-20261020-153000-9f8e7d6\     developer-mode build (uploaded tree)
  current                                   "1.3.0"
  previous                                  "1.2.0"
  trial                                     present while a just-switched version is unproven
  browsers\                                 PLAYWRIGHT_BROWSERS_PATH (shared by versions)
  cache\uv\                                 UV_CACHE_DIR

C:\ProgramData\FontRender\                  HOME  (FONT_RENDER_HOME)
  .env                                      configuration (port, limits; optional)
  install.json                              install facts: version, paths, port, channel
  clients.json                              client token hashes + auth settings
  .session_key                              web admin session signing key
  devmode.json                              developer mode state (absent = off)
  data\settings.json, data\stats.json, data\firefox_prefs.json
  logs\service.log                          NSSM stdout/stderr, rotated at 5 MB
  logs\audit.log                            JSON lines: security-relevant actions
  staging\                                  downloads being verified (emptied after use)
```

`HOME` is ACL'd to `SYSTEM` and `Administrators` only (inheritance removed): it holds
token hashes and the session key, and its write protection is what makes "local admin
only" enforceable for `reset-admin` and developer mode.

Nothing is written elsewhere: Playwright's browsers and registry live in `APP\browsers`
(not the SYSTEM profile — this also ends the "browser installed for the wrong user"
problem), uv's cache in `APP\cache`, Chromium's temporary profiles are removed by
Playwright on close. Fonts are still never written to disk.

Checkout mode (development, lurkbox until migrated): with `FONT_RENDER_HOME` unset, HOME
is the repository root and every path is what it is today, so nothing changes for an
existing checkout.

## Service and launcher

NSSM runs a **stable** command that never changes between versions:

```
APP\python\python.exe APP\bin\launcher.py
  env: FONT_RENDER_HOME=…  FONT_RENDER_APP=…  PLAYWRIGHT_BROWSERS_PATH=APP\browsers
```

`launcher.py` (stdlib only, ~80 lines):

1. read `current`;
2. if `trial` exists: increment its start counter; at 3 failed starts write
   `current = previous`, delete `trial`, append `auto-revert` to `audit.log`;
3. run `versions\<current>\.venv\Scripts\python.exe -m font_render serve`, pass the exit
   code through (NSSM restarts after 5 s, as today).

The server deletes `trial` once it is proven healthy: lifespan finished, browser warm-up
succeeded, `/health` served. This replaces the risk noted in CLAUDE.md ("a deploy that
breaks import but compiles will crash-loop the service").

Service name stays `FontRenderService` (LocalSystem, auto-start, log rotation as today).

## Releases (CI)

`.github/workflows/release.yml` runs only in the public repository
(`if: github.repository == 'typedev/font-render-server'`), on a tag `vX.Y.Z`. Until the
switch, release candidates are tested from here as dev builds and with `--dry-run`.

1. **check** (ubuntu): `uv run pytest`.
2. **build** (ubuntu): `git archive` of the tag → add `bin\nssm.exe` (downloaded from a
   pinned URL, SHA256 checked) → `font-render-X.Y.Z.zip`; `install.ps1` with the version
   and the zip's SHA256 baked in; `SHA256SUMS`.
3. **sign** (environment `release` of the public repository, required reviewer =
   maintainer, `v*` tags only): sign
   `SHA256SUMS` with the Ed25519 release key from the environment secret →
   `SHA256SUMS.sig` (`scripts/release_key.py sign`). The public keys are `TRUSTED_KEYS` in
   `src/font_render/release_key.py`; key handling: [releasing.md](releasing.md).
4. **smoke** (windows-latest): run `install.ps1` against the built artifacts → `/health` →
   render a font generated with fontTools → `update` to the same build (exercises the
   switch) → `uninstall --purge` → assert: no service, no firewall rule, no Uninstall
   registry key, no Start menu entries, no `APP`, no `HOME`.
5. **publish**: GitHub Release with the four assets; tags of a pre-release version (`v1.3.0b1`, PEP 440 as in `pyproject.toml`)
   are published as pre-releases (beta channel).

Before tagging (manual checklist, `docs/development.md`): `pixel-check compare --server`
on lurkbox running the release candidate as a dev build; bump `RENDER_VERSION` if pixels
are meant to change. CI cannot run pixel-check — the fonts are unreleased.

Version reporting: `/info` and `/health` carry `version` (the running version directory:
`1.3.0`, `dev-a1b2c3d`; the git commit in a checkout) and `release` (package version);
`/info` also `layout` and `dev_mode`.

## Install (`install.ps1`)

Stable URL: `https://github.com/typedev/font-render-server/releases/latest/download/install.ps1`

```powershell
# elevated PowerShell
irm https://github.com/typedev/font-render-server/releases/latest/download/install.ps1 | iex

# with options
& ([scriptblock]::Create((irm https://github.com/typedev/font-render-server/releases/latest/download/install.ps1))) `
    -Version 1.2.0 -Port 8080 -NoFirewall -Channel beta
```

For cautious admins (`docs/deployment.md` will show both): download `install.ps1`, compare
its SHA256 with `SHA256SUMS` on the release page, read it, run it.

Parameters: `-Version` (default: the release the script came from), `-Port` (8000),
`-NoFirewall`, `-Channel stable|beta`, `-InstallDir`, `-DataDir`, `-Quiet` (no prompts,
for scripted installs).

What the script does (kept small; everything else is Python and tested):

1. Not elevated → save itself to `%TEMP%` and relaunch with `Start-Process -Verb RunAs`.
2. Force TLS 1.2 (Windows PowerShell 5.1), check Windows 10 1809+ / x64.
3. Download pinned `uv.exe` (SHA256 baked into the script) to `APP\bin`.
4. `uv python install 3.12` into `APP\python`.
5. Download the zip + `SHA256SUMS`, check the SHA256 (trust on first install = HTTPS
   from the same release as the script; updates additionally verify the signature).
6. Extract to `APP\versions\X.Y.Z`, `uv sync --frozen --no-dev`.
7. `versions\X.Y.Z\.venv\Scripts\python.exe -m font_render install --port … [--no-firewall]`.

`font_render install` (idempotent: on an existing install it is **repair**):

1. Create `HOME`, set its ACL; write `install.json`; keep an existing `.env`.
2. `playwright install chromium` into `APP\browsers`.
3. Copy `launcher.py`, `nssm.exe`, wrappers to `APP\bin`; write `current`.
4. (Re)register `FontRenderService` with the launcher command and environment above.
5. Firewall rule `Font Render Service`: TCP port, `profile=private remoteip=localsubnet`.
6. New install: `clients.json` with `allow_anonymous_render=false`,
   `legacy_admin_token=false` and an `admin` client (scopes `render,admin`).
   Existing install: clients untouched (lost token → `reset-admin`).
7. Start menu folder "Font Render": *Web admin* (URL), *Reset admin token*, *Uninstall*.
8. Uninstall entry `HKLM\…\Uninstall\FontRenderService`: DisplayName "Font Render
   Service", DisplayVersion, Publisher `Alexander Lubovenko`, URLInfoAbout, InstallLocation, DisplayIcon, EstimatedSize,
   `UninstallString` = `bin\uninstall.cmd`, `QuietUninstallString` = `… --quiet --keep-data`,
   NoModify, NoRepair.
9. Start the service, wait for `/health`, print:

```
✓ Font Render Service 1.2.0 installed and running
  Web admin:   http://STUDIO-PC:8000/admin/
  Admin token: fr_…        (copied to the clipboard; shown only once)
  Lost it? Start → Font Render → Reset admin token
```

Running the same command again on an installed machine repairs it (same or newer
version; an older `-Version` is refused — use rollback in the web admin).

## Updates

Release channel, from the web admin (Maintenance) or `font-render --update`:

- **Check**: the server polls `GET api.github.com/repos/typedev/font-render-server/releases`
  every `UPDATE_CHECK_HOURS` (default 6, `0` = only on demand; editable in the admin) with
  ETag caching; stable = latest non-prerelease, beta = latest including pre-releases. The
  admin shows "Version 1.3.0 available — release notes" (release body rendered as text).
  Check requests carry no client or host information beyond what HTTPS implies.
- **Update** (admin scope), a background job with progress in the admin:
  1. download zip, `SHA256SUMS`, `SHA256SUMS.sig` into `HOME\staging`;
  2. verify the signature with the embedded public key, then the zip's SHA256 — any
     failure stops here;
  3. extract to `versions\X.Y.Z.partial`, `uv sync --frozen --no-dev`;
  4. `playwright install chromium` with that version's Python (no-op if the build is there);
  5. self-check: `python -m font_render selfcheck` in the new venv (imports the app and
     every module, checks the browser executable exists);
  6. rename to `versions\X.Y.Z`; `previous = current`, `current = X.Y.Z`, create `trial`;
  7. audit `update`, exit → NSSM restarts → launcher starts the new version.
- **Rollback** (admin scope): `current ↔ previous`, restart. Instant, no network.
- **Retention**: keep `current`, `previous` and the newest other release; prune older
  version directories and browser builds no kept version uses.
- Updates never install a version older than `previous` and never install unsigned code.

`/update` and `/rollback` keep their paths and meaning ("newest" / "previous"); in
checkout mode they keep the git behaviour until lurkbox is migrated, then the git code
is removed.

## Uninstall

"Apps & features" → Font Render Service → Uninstall, or Start → Font Render → Uninstall, or
`APP\bin\uninstall.cmd [--quiet] [--purge | --keep-data]`:

1. elevate; ask "Also delete data (client tokens, settings, statistics, logs)? [y/N]"
   (default keep, so a reinstall picks up the clients);
2. stop and remove the service; remove the firewall rule, Start menu folder and the
   Uninstall registry key;
3. delete `APP` except the running interpreter, then hand the final `rmdir` of `APP` to a
   detached `cmd /c` that waits for the uninstaller to exit;
4. `--purge` (or "y"): delete `HOME`.

After a purge nothing of the service remains on the machine; the CI smoke job asserts it.

## Admin recovery (`reset-admin`)

Start → Font Render → *Reset admin token*, or in an elevated console:

```
font-render-server reset-admin [--client admin]
```

- Requires an elevated process (checked) — and `HOME` is only writable by administrators
  anyway, so the check cannot be bypassed from the network.
- Client `admin` exists → new token, enabled, scopes include `admin`; missing → created.
  The old token stops working at once (`clients.json` is reloaded on change) and its web
  admin sessions end (session fingerprints change).
- Prints the token once and copies it to the clipboard; appends `reset-admin` to the
  audit log.

This replaces the current recovery advice "delete `clients.json`", which on a new install
would open anonymous rendering and leave no admin.

## Developer mode

Full remote control of one instance for its developers: deploy any commit or a local
working tree. **Off by default; only a local Windows administrator can switch it on;
anyone with an admin token can switch it off; it expires.**

### Switching it on — locally only

```
font-render-server dev-mode on --client alexander \
    --reason "Server development: deploy from main" \
    [--days 30] [--allow-from 192.168.1.0/24]
font-render-server dev-mode status
font-render-server dev-mode off
```

- Elevated process required (as `reset-admin`). There is **no HTTP endpoint** that
  enables, extends or widens developer mode.
- `--client`: clients that get the `dev` scope (created if missing, token printed once).
  `dev` is not implied by `admin`, and the admin API refuses to grant it — an admin token
  cannot turn itself into a deploy token.
- `--reason`: required; shown in the web admin and the audit log.
- `--days`: default **30**; `0` = until switched off (explicit, for lurkbox). The admin
  shows a warning from 7 days before expiry.
- `--allow-from`: CIDRs allowed to call `/dev/*` (default: any address the server accepts).

The development repository is private, so the server cannot fetch commits itself: every
developer deploy is an upload from the developer's machine (below). Developer mode is
therefore exactly "this client may upload code"; there is no separate upload switch.

State in `HOME\devmode.json` (reloaded on change, like `clients.json`):

```json
{"enabled": true, "since": "2026-10-20T15:30:00", "until": null,
 "by": "LURKBOX\\alexander", "reason": "Server development: deploy from main",
 "clients": ["alexander"], "allow_from": ["192.168.1.0/24"]}
```

### What the `dev` scope allows

| Endpoint | Does |
|---|---|
| `GET /dev/status` | mode, expiry, current build, recent dev deploys |
| `POST /dev/deploy` (zip + `{commit, dirty}`) | run the update pipeline (steps 3–7, no signature) for an uploaded tree (≤ 50 MB) into `versions\dev-<commit[:7]>` or, for a working tree with changes, `versions\dev-<commit[:7]>-dirty-<sha256[:7]>` |
| `POST /dev/release` | switch back to the newest installed release (or install the latest one) |

Rollback, logs and settings stay on the admin scope as before. No git on the server, no
PAT, no access to the private repository. `commit` and `dirty` are what the client says;
they label the build in the admin and the audit log.

When developer mode is off or expired, or the caller's address is not in `allow_from`,
`/dev/*` answers **404** (as if it did not exist); a client without `dev` gets 403.
Expiry does not switch the running build — a surprise change of pixels is worse — the
banner says a dev build is running and offers *Install latest release*.

### Client side

```bash
# .env: FONT_RENDER_DEV_TOKEN=…
uv run font-render --deploy              # origin/main (git archive, after git fetch)
uv run font-render --deploy a1b2c3d      # any commit or branch
uv run font-render --deploy --local      # the working tree, uncommitted changes included
uv run font-render --release             # back to the latest release
```

A ref is packed with `git archive <ref>`. `--local` packs `git ls-files --cached --others --exclude-standard` — tracked files plus
untracked, non-ignored ones, with uncommitted changes — so ignored paths (`renders/`,
`.env`, fonts) are never uploaded. The tool prints the file count and size first.

### Visibility

- Web admin: a red banner on every tab while enabled — since/until, who enabled it and
  why, clients with `dev`, source repository, running build — with **Switch off**
  (admin scope).
- `/info` (authenticated): `dev_mode: {"enabled": true, "until": …}`, and `version` is the
  build directory (`dev-a1b2c3d`), so clients (hintbench, the bot) can tell a dev build
  from a release. The public `/health` does not say whether developer mode is on.
- `logs\audit.log` (JSON lines, shown in Maintenance): `dev-mode-on`, `dev-mode-off`
  (who, local or which client), `dev-mode-expired`, `dev-deploy` (client, commit, dirty,
  size), `dev-release`, plus `install`, `update`, `update-failed`, `update-healthy`,
  `rollback`, `auto-revert`, `reset-admin`, `uninstall`.
- Fonts: the client refuses to pack font files (and names them locally); the server
  refuses an archive with any font file without naming it (font names never reach its logs).

### Why this is not a backdoor

Everything is in the public code and documented here; it is off on every install; turning
it on requires the same local administrator rights that already control the machine, so
it grants nobody anything they could not already do; it is visible to every admin, can be
switched off by any of them, and expires by default.

## Client package

A light distribution `font-render-client` (dependency: `requests`) in the same repository
(uv workspace), published to PyPI with trusted publishing from the release workflow:

```bash
uv tool install font-render-client
font-render login http://studio-pc:8000 fr_…     # → ~/.config/font-render/config.toml
                                                 #   (%APPDATA%\font-render on Windows)
font-render FONT.ttf
```

Precedence: command-line flags > environment > `./.env` > config file. The full package
keeps the admin tools (`font-render-clients`, `pixel-check`) and the existing entry
points, so hintbench and current checkouts keep working.

## Implementation plan

Each step is shippable on its own and leaves lurkbox working. Pixel identity: steps 1–5
do not touch the render path; still run `pixel-check compare` (local, then `--server`)
after each deploy, per CLAUDE.md.

### Step 1 — HOME, CLI, install/uninstall/reset-admin (code, testable on Linux) — done

Implemented as planned (`tests/test_install.py`); not yet run on Windows — the first real
run is the CI smoke job of step 2.

- `config.py`: `HOME = Path(FONT_RENDER_HOME or SERVICE_DIR)`, `APP = FONT_RENDER_APP or
  None` (None = checkout mode); load `HOME/.env`; derive `CLIENTS_FILE`,
  `SESSION_KEY_FILE`, `LOG_FILE`, `STATS_FILE`, `SETTINGS_FILE`, `FIREFOX_PREFS_FILE`,
  `LAST_GOOD_COMMIT`, new `AUDIT_LOG`, `DEVMODE_FILE` from `HOME` (explicit env vars still
  win); `PORT`/`HOST` from `FONT_RENDER_PORT`/`FONT_RENDER_HOST` (8000 / 0.0.0.0).
- `__main__.py`: subcommands `serve` (default, so `font-render-server` with no arguments
  behaves as today), `install`, `uninstall`, `reset-admin`, `selfcheck`, `version`.
- New `src/font_render/install.py`: the steps of *Install* and *Uninstall* as functions
  over a command runner (nssm, netsh, icacls, reg via `winreg`, shortcuts via PowerShell);
  `--dry-run` prints the plan. On Linux the runner is faked in tests.
- New `src/font_render/localadmin.py`: elevation check (`IsUserAnAdmin`),
  `reset_admin()`, audit log writer (shared with later steps).
- `deploy/windows/launcher.py`, `uninstall.cmd`, `reset-admin.cmd`.
- Tests: `test_home.py` (paths follow `FONT_RENDER_HOME`, checkout mode unchanged),
  `test_install.py` (command plan for fresh install / repair / uninstall with and without
  purge; idempotency), `test_reset_admin.py` (rotate, create, old token and sessions
  invalid), `test_launcher.py` (trial counter, auto-revert).
- Docs: `development.md` (code layout), `auth.md` (Recovery → `reset-admin`).

### Step 2 — Releases and `install.ps1` — done (publishing waits for step 6)

`install.ps1`, `scripts/build_release.py`, `.github/workflows/release.yml`,
`scripts/smoke_test.ps1`; release key `4fdea217d5c9d919` in `TRUSTED_KEYS`, its private
half in the public repository's `release` environment. The workflow passes on a Windows
runner (run it by hand: Actions → Release → Run workflow): install, render, anonymous
rendering closed, data dir ACL, Apps & features entry, firewall rule, repair keeps the
token, reset-admin, uninstall --purge leaves nothing. Sign and publish run only for a tag
in `typedev/font-render-server`.

- `install.ps1` (repo root), `.github/workflows/release.yml` (runs in the public
  repository only), release key set up per [releasing.md](releasing.md) (public key
  committed),
  `font_render.__version__`, `version`/`channel` in `/info` and `/health`.
- First release `v1.0.0`; install it on a spare Windows machine or VM besides CI.
- Docs: rewrite `deployment.md` around `install.ps1` (checkout install moves to
  `development.md`), README quick start.

### Step 3 — Updates from the web admin — done (not yet run against a real release)

`updates.py` (check with ETag, job, signature + hash, uv sync, Chromium, selfcheck,
`refresh_after_switch`, pointers, trial, prune, rollback, `mark_healthy`, periodic check),
`/update` / `/rollback` by layout, `/admin/api/update-status`, `/admin/api/update-channel`,
the Maintenance tab, `font-render --update` following the job; `tests/test_updates.py`
(fake GitHub with signed releases). Not done: pruning browser builds no kept version uses.
The first end-to-end run is the first release in step 6 (install 1.0.0, update to 1.0.1).

- `src/font_render/updates.py`: check, staging pipeline, switch, rollback, retention;
  signature verification (`cryptography`, Ed25519).
- `web.py` / `static/`: update card with progress, release notes, channel, check interval;
  audit log view. `admin.py`: `/update`, `/rollback` dispatch by mode.
- Server clears `trial` after a healthy start (lifespan + warm-up).
- Tests with a local fake release server: bad signature, bad hash, failed `uv sync`,
  failed self-check, switch, rollback, retention, auto-revert.
- Docs: `admin.md` (Maintenance), `api.md` (admin endpoints, `/info` fields).

### Step 4 — Developer mode, lurkbox migration — done (2026-10-19)

Done: `devmode.py`, `/dev/status|deploy|release`, the `dev-mode` subcommand, the `dev`
scope outside `auth.SCOPES`, banner + switch-off + audit log in the web admin,
`font_render_tools/deploy.py` and `font-render --deploy [REF] | --deploy --local |
--release`; `tests/test_devmode.py`; the smoke test deploys a dev build on Windows.
lurkbox migrated 2026-10-19: `install.ps1 -From <dist> -ImportFrom C:\WORK\wintt-server-render`
over SSH, all clients kept, `pixel-check compare --server` 218/218 identical against a
baseline taken just before, developer mode on for client `alexander` (no expiry,
192.168.1.0/24); the GitHub token that lived in the old checkout's remote URL and `.env`
was removed from both.

- `src/font_render/devmode.py` (state, expiry, `allow_from`), `dev` scope in `auth.py`
  (admin API refuses to grant it), `/dev/*` router, `dev-mode` subcommand, banner and
  switch-off in the web admin, `--deploy` / `--deploy --local` / `--release` in
  `font-render`.
- Tests: 404 when off/expired/outside `allow_from`, 403 without `dev`, admin cannot grant
  `dev`, `--local` file selection excludes ignored files, audit entries.
- Migrate lurkbox: capture a pixel baseline → install with `install.ps1 -ImportFrom
  C:\WORK\wintt-server-render` (same service name, replaces the NSSM entry; copies
  `clients.json`, `.session_key`, `data\`, `.env`) → `pixel-check compare --server`
  → `dev-mode on --client alexander --days 0 --allow-from 192.168.1.0/24`
  over SSH → `FONT_RENDER_DEV_TOKEN` in the local `.env` → keep the old checkout until
  the new install has run for a while, then delete it and the git code paths.
  The bot (`FontRenderBot`, separate checkout) is unaffected — it only talks HTTP.
- Docs: this file's developer mode section moves to `deployment.md` / `admin.md`;
  CLAUDE.md Deployment section (`--deploy` instead of `--update` for our own pushes).

### Step 5 — Client package — done (PyPI publishing waits for step 6)

`client/` (uv workspace member, dependency `requests`), `font_render_client` with
`font-render login | logout` (config file, token checked against `/info`, mode 600);
`tests/test_client.py`; a wheel installed alone renders against lurkbox. The `pypi` job of
the release workflow publishes it by trusted publishing (pending publisher to register:
[releasing.md](releasing.md#4b-pypi-once-for-the-client-package)).

- `clients/` workspace member `font-render-client`, `login` command, config file,
  PyPI trusted publishing in the release workflow.
- Docs: README "Connecting to a server" for studios' users.

### Step 6 — Switch to the public repository — done (2026-10-19)

- Check the tree for anything internal (`CLAUDE.md` infrastructure notes, `TODO.md`,
  host names in docs) and decide what goes public.
- Import the tree into `typedev/font-render-server` as one commit; tag `v1.0.0` there →
  first real release (sign approval, Windows smoke test).
- lurkbox: reinstall with `install.ps1` from the public release (step 4 migration, if not
  done yet); local checkouts: clone the public repository; hintbench and the bot only talk
  HTTP and are unaffected.
- Archive `typedev/wintt-server-render` (read-only, private).

## Decisions

- **Release key custody**: GitHub environment `release` secret with a required reviewer
  (the maintainer), restricted to `v*` tags. Creating, using, rotating the key and what to
  do if it leaks: [releasing.md](releasing.md).
- **Publisher** in "Apps & features": `Alexander Lubovenko`; `URLInfoAbout` = the
  repository page.
- **Developer mode**: default expiry 30 days. Because the development repository is private,
  every dev deploy is an upload, so the planned `--allow-upload` switch is gone: developer
  mode itself is the permission.
- **Public repository** `typedev/font-render-server`: development moves there (fresh
  history) once steps 1–5 are done here; this repository stays private and is archived.
  Its `release` environment exists already (reviewer `typedev`, tags `v*`).
- **Third-party notices**: the repository is MIT (`LICENSE.md`); the release zip adds
  `nssm.exe` (public domain) — list it, uv and the CPython build in `THIRD_PARTY.md`.
- **Code signing** of `install.ps1`: not needed for `irm | iex`; an Authenticode signature
  (e.g. Azure Trusted Signing) only matters if we later ship an `.exe`.
