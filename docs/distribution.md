# Distribution: install, update, uninstall, developer mode

How the server is distributed and why (status: **implemented**, releases v1.0.0 and v1.0.1 on
2026-10-19; the how-to is [deployment.md](deployment.md)). Goal: a colleague or another type
studio installs the server on a Windows machine with one command, without Git, Python or
manual configuration; updates, rollback and a clean uninstall work from the web admin and
"Apps & features"; a lost admin token can be re-issued locally; and the developers keep full
remote access to their own instance (lurkbox) through an explicit, visible, expiring
developer mode instead of a `git pull` endpoint.

## Two repositories

| Repository | Visibility | Role |
|---|---|---|
| `typedev/wintt-server-render` | private, archived | development until 2026-10-19 |
| `typedev/font-render-server` | **public** | the project since then: code, releases, issues |

The private history names font files, so it is never published: the public repository
started from a fresh copy of the tree (one commit), and the private one was archived.
Releases are built and signed by the public repository's own workflow, from the public
source — anyone can check what a release contains.

## Overview

```
   font-render-server ── tag vX.Y.Z ──► release workflow (test, build, Windows smoke test,
                                         sign after approval, publish, PyPI)
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
    font-render-server.cmd                  any subcommand of the current version
  python\                                   uv-managed CPython (UV_PYTHON_INSTALL_DIR)
  versions\
    1.2.0\                                  release: src\, pyproject.toml, uv.lock, .venv\
    1.3.0\
    dev-a1b2c3d\                            developer-mode build of a commit
    dev-a1b2c3d-dirty-9f8e7d6\              developer-mode build of a working tree with changes
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

Checkout mode (development): with `FONT_RENDER_HOME` unset, HOME is the repository root.

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
succeeded. A build that breaks on import therefore cannot crash-loop the service.

Service name `FontRenderService` (LocalSystem, auto-start, log rotated at 5 MB).

## Releases (CI)

`.github/workflows/release.yml`, on a tag `vX.Y.Z` (or by hand, which stops after the smoke
test):

1. **test** (ubuntu): `uv run pytest`.
2. **build** (ubuntu): `scripts/build_release.py` — `git archive` of the tag + `bin\nssm.exe`
   (pinned, SHA256 checked; archive.org fallback) → `font-render-X.Y.Z.zip`; `install.ps1`
   with the version baked in; `SHA256SUMS`. On a tag: tag = `pyproject.toml` version =
   `client/pyproject.toml` version.
3. **smoke** (windows-2022): `scripts/smoke_test.ps1` — install from the built assets →
   `/health`, render, anonymous 401, data-dir ACL, Apps & features, firewall → repair run
   in-session (as `irm | iex`) keeps the token and the session → reset-admin → developer mode:
   deploy a tree, trial removed, back to the release → `uninstall --purge` → nothing left.
4. **sign** (environment `release`: required reviewer = maintainer, `v*` tags only; public
   repository only): sign `SHA256SUMS` with the Ed25519 release key →
   `SHA256SUMS.sig` (`scripts/release_key.py sign`). The public keys are `TRUSTED_KEYS` in
   `src/font_render/release_key.py`; key handling: [releasing.md](releasing.md).
5. **publish**: GitHub Release with the four assets; a pre-release version (`v1.3.0b1`,
   PEP 440 as in `pyproject.toml`) is published as a pre-release (beta channel).
6. **pypi** (environment `pypi`): `font-render-client` by trusted publishing.

Before tagging ([releasing.md](releasing.md#making-a-release)): `pixel-check compare --server`
on lurkbox running the release candidate as a dev build; bump `RENDER_VERSION` if pixels are
meant to change. CI cannot run pixel-check — the fonts are unreleased.

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

For cautious admins ([deployment.md](deployment.md#install) shows both): download
`install.ps1`, compare its SHA256 with `SHA256SUMS` on the release page, read it, run it.

Parameters: `-Version` (default: the release the script came from), `-Port` (41800),
`-NoFirewall`, `-Channel stable|beta`, `-InstallDir`, `-DataDir`, `-From` (assets from a
folder), `-Force` (allow an older version), `-ImportFrom` (data of a checkout install).

Piped into `iex` the script runs inside the caller's PowerShell session, so it never calls
`exit` there (that would close the window — the 1.0.0 bug): it returns, or throws on an
error; run as a file it exits with a code.

What the script does (kept small; everything else is Python and tested):

1. Not elevated → relaunch elevated with `Start-Process -Verb RunAs` (from its file, or, piped
   into `iex`, from its release's `install.ps1` downloaded to `%TEMP%`).
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
Font Render Service 1.2.0 installed and running
  Web admin:   http://STUDIO-PC:41800/admin/
  Admin token: …   (copied to the clipboard)
               shown only once - keep it in a password manager
  Lost the token? Start > Font Render > Reset admin token
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
  3. extract to `versions\X.Y.Z` (archives with font files are refused), `uv sync --frozen
     --no-dev` there (the project is installed editable, so the venv is built in place);
  4. `playwright install chromium` with that version's Python (no-op if the build is there);
  5. self-check: `python -m font_render selfcheck` in the new venv (imports the app and
     every module, checks the browser executable exists) — any failure removes the
     directory and leaves the running version alone;
  6. copy the new `launcher.py` and wrappers to `APP\bin`, update `install.json` and the
     Apps & features version; `previous = current`, `current = X.Y.Z`, create `trial`;
  7. audit `update`, exit → NSSM restarts → launcher starts the new version.
- **Rollback** (admin scope): `current ↔ previous`, restart. Instant, no network.
- **Retention**: keep `current`, `previous` and the newest other release; developer builds
  only while current or previous. (Browser builds no kept version uses are not pruned yet.)
- Updates install only versions newer than the running one (older: rollback, or
  `install.ps1 -Force`) and never unsigned code.
- A release that needs a different Python than `PYTHON_VERSION` can only be installed with
  `install.ps1`.

`/update` and `/rollback` have the same paths in both layouts ("newest" / "previous"); in a
checkout they still `git pull` / `git reset`.

## Uninstall

"Apps & features" → Font Render Service → Uninstall, or Start → Font Render → Uninstall, or
`APP\bin\uninstall.cmd [--quiet] [--purge | --keep-data]`:

1. elevate; ask "Also delete data (client tokens, settings, statistics, logs)? [y/N]"
   (default keep, so a reinstall picks up the clients);
2. stop and remove the service; remove the firewall rule, Start menu folder and the
   Uninstall registry key;
3. delete `APP` except what the uninstaller runs on (`APP\python`, the current venv);
   `uninstall.cmd` removes the rest with `rd /s /q` once Python has exited, on the same,
   already parsed line (a detached remover did not survive on Windows);
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

It replaces the old recovery advice "delete `clients.json`", which on a new install would
open anonymous rendering and leave no admin.

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
- `--days`: default **30**; `0` = until switched off (explicit, for lurkbox).
- `--allow-from`: CIDRs allowed to call `/dev/*` (default: any address the server accepts).

The server never fetches code for a dev build: every developer deploy is an upload from the
developer's machine (below) — it works for unpushed commits and working trees, and the
server needs no repository access. (It was designed while the repository was private.)
Developer mode is therefore exactly "this client may upload code".

State in `HOME\devmode.json` (reloaded on change, like `clients.json`):

```json
{"enabled": true, "since": "2026-10-20T15:30:00", "until": null,
 "by": "STUDIO-PC\\alexander", "reason": "Server development: deploy from main",
 "clients": ["alexander"], "allow_from": ["192.168.1.0/24"]}
```

### What the `dev` scope allows

| Endpoint | Does |
|---|---|
| `GET /dev/status` | mode, expiry, current / previous build, the job |
| `POST /dev/deploy` (zip + `{commit, dirty}`) | run the update pipeline (steps 3–7, no signature) for an uploaded tree (≤ 50 MB) into `versions\dev-<commit[:7]>` or, for a working tree with changes, `versions\dev-<commit[:7]>-dirty-<sha256[:7]>` |
| `POST /dev/release` | switch back to the newest installed release (or install the latest one) |

Rollback, logs and settings stay on the admin scope. No git on the server, no PAT. `commit`
and `dirty` are what the client says; they name the build directory and appear in the audit
log.

When developer mode is off or expired, or the caller's address is not in `allow_from`,
`/dev/*` answers **404** (as if it did not exist); a client without `dev` gets 403.
Expiry does not switch the running build — a surprise change of pixels is worse; the
version shown everywhere (`dev-…`) tells that a dev build is running, and an admin returns
to a release with **Roll back** or **Update** (developers: `--release`).

### Client side

```bash
# .env: FONT_RENDER_DEV_TOKEN=…   (or: font-render login --dev-token …)
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
  why, clients with `dev`, `allow_from`, running build — with **Switch off** (admin scope).
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
font-render login http://studio-pc:41800          # asks for the token, checks it, saves it in
                                                 # ~/.config/font-render/config.toml
                                                 # (%APPDATA%\font-render on Windows)
font-render FONT.ttf
```

Precedence: command-line flags > environment > `./.env` > repository `.env` (checkout) >
config file. The client package has `font-render`, `font-render-batch` and
`font-render-clients`; the server package keeps `font-render-server` and `pixel-check` and
depends on the client, so a checkout has every command.

## History

Built in six steps on 2026-10-18/19 (in the private repository, then here):

1. Data separated from code (`FONT_RENDER_HOME`), `install` / `uninstall` / `reset-admin`,
   the launcher.
2. `install.ps1`, release build, the Windows smoke test in CI.
3. Updates from the web admin.
4. Developer mode; lurkbox moved from its git checkout to the installed layout
   (`install.ps1 -ImportFrom`, all clients kept, pixel-check 218/218 identical).
5. The client package and `font-render login`.
6. The public repository, v1.0.0; lurkbox updated 0.1.0 → 1.0.0 through the release update
   (10 s, `update-healthy`, pixels identical). A full uninstall + `irm | iex` reinstall on
   lurkbox by hand found the closing-window bug, fixed in v1.0.1.

Windows lessons (each one broke the smoke test or a real install first): NSSM keeps
`AppParameters` as one command line — quote paths with spaces; console output must be ASCII
(cp1252); `nssm start` exits 1 while the service is still `START_PENDING`; `icacls … /T`
with `/inheritance:r` leaves existing files unreadable; detached processes do not outlive
the uninstaller; `exit` in a script piped into `iex` closes the user's window.

Not done: pruning unused browser builds; a Python change between releases needs
`install.ps1`; `--allow-upload` was dropped (see Decisions).

## Decisions

- **Release key custody**: GitHub environment `release` secret with a required reviewer
  (the maintainer), restricted to `v*` tags. Creating, using, rotating the key and what to
  do if it leaks: [releasing.md](releasing.md).
- **Publisher** in "Apps & features": `Alexander Lubovenko`; `URLInfoAbout` = the
  repository page.
- **Developer mode**: default expiry 30 days. Every dev deploy is an upload, so the planned
  `--allow-upload` switch is gone: developer mode itself is the permission.
- **Public repository** `typedev/font-render-server` with a fresh history; the private
  `typedev/wintt-server-render` is archived. Environments: `release` (reviewer `typedev`,
  tags `v*`), `pypi` (tags `v*`).
- **Third-party notices**: the repository is MIT (`LICENSE.md`); what the release and the
  installer bring along is listed in [THIRD_PARTY.md](../THIRD_PARTY.md).
- **Code signing** of `install.ps1`: not needed for `irm | iex`; an Authenticode signature
  (e.g. Azure Trusted Signing) only matters if we later ship an `.exe`.
