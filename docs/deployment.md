# Deployment (Windows server)

Two ways to run the server:

- **Installed** (recommended) — one command, no Git or Python needed; program in
  `C:\Program Files\FontRender`, data in `C:\ProgramData\FontRender`, updates from signed
  [releases](https://github.com/typedev/font-render-server/releases)
  ([distribution.md](distribution.md)).
- **Checkout** — a git working copy run by NSSM, updated with `git pull`: for developing on
  Windows ([Checkout install](#checkout-install)).

## Install

Requirements: Windows 10 1809+ or 11 (64-bit), an administrator account, internet access
(the installer downloads the release, uv, Python and Chromium — about 400 MB on disk).

In PowerShell (it asks for administrator rights if it does not have them):

```powershell
irm https://github.com/typedev/font-render-server/releases/latest/download/install.ps1 | iex
```

At the end it prints the web admin address and the **admin token** (shown once, copied to
the clipboard) — keep it in a password manager:

```
Font Render Service 1.0.0 installed and running
  Web admin:   http://STUDIO-PC:8000/admin/
  Admin token: …
  Lost the token? Start > Font Render > Reset admin token
```

Options (pass them through a script block):

```powershell
& ([scriptblock]::Create((irm https://github.com/typedev/font-render-server/releases/latest/download/install.ps1))) -Port 8080 -NoFirewall
```

| Option | Default | Meaning |
|---|---|---|
| `-Version` | the release the script came from | install this version (`1.2.0`) |
| `-Port` | `8000` | HTTP port (written to `C:\ProgramData\FontRender\.env`) |
| `-NoFirewall` | off | do not add the firewall rule |
| `-Channel` | `stable` | update channel (`beta` also offers pre-releases) |
| `-InstallDir`, `-DataDir` | `C:\Program Files\FontRender`, `C:\ProgramData\FontRender` | where program and data go |
| `-Force` | off | allow installing an older version than the installed one |
| `-From` | — | folder with the release assets instead of GitHub (tests, offline copy) |
| `-ImportFrom` | — | a [checkout install](#checkout-install) whose data moves into the new data folder: `.env`, `clients.json` (all tokens keep working), `.session_key`, `data\settings.json`, `stats.json`, `firefox_prefs.json`; only into an empty data folder |

If you prefer not to pipe a script from the internet: download `install.ps1` and
`SHA256SUMS` from the release page, compare `Get-FileHash install.ps1` with the file, read
it, then run `powershell -ExecutionPolicy Bypass -File install.ps1`.

What the installer does: verifies the release archive against `SHA256SUMS`; puts a pinned uv,
Python 3.12, the program and Chromium into `C:\Program Files\FontRender`; creates
`C:\ProgramData\FontRender` readable only by administrators and SYSTEM; registers the
`FontRenderService` service (automatic start, restarts after a crash); adds the firewall
rule; creates the `admin` client with anonymous rendering closed; adds Start menu shortcuts
(web admin, reset admin token, uninstall) and an entry in **Apps & features**.

### Updates

Web admin → **Maintenance** → **Check for updates** shows newer releases of the update
channel (`stable`; `beta` also offers pre-releases) with their notes; **Update to X.Y.Z**
downloads the release, checks its signature and hash, installs it next to the running
version, runs a self-check and restarts into it. **Roll back** returns to the previous
version at once (no download). From a client: `uv run font-render --update` / `--rollback`
with `FONT_RENDER_ADMIN_TOKEN`.

The server looks for new releases every `UPDATE_CHECK_HOURS` (6; `0` = only when asked) and
logs when one is available; it never updates by itself. If a new version fails to start
three times, the service returns to the previous one on its own (audit log:
`auto-revert`). Three versions are kept on disk (current, previous, one more).

A release that needs a different Python version than the installed one can only be
installed by running the install command again.

### Repair, reinstall

Run the same command again. It keeps client tokens, settings and statistics ("Admin token:
unchanged") and replaces the program files.

### Lost admin token

Start → **Font Render** → **Reset admin token** (asks for administrator rights), or in an
elevated console: `"C:\Program Files\FontRender\bin\reset-admin.cmd"`. The admin client gets
a new token, the old one stops working ([auth.md](auth.md#recovery)).

### Uninstall

**Apps & features** → Font Render Service → Uninstall (or Start → Font Render → Uninstall).
It asks whether to delete the data (client tokens, settings, statistics, logs); by default
the data stays, so a reinstall picks the clients up again. From a console:

```cmd
"C:\Program Files\FontRender\bin\uninstall.cmd" --purge --quiet      &:: everything
"C:\Program Files\FontRender\bin\uninstall.cmd" --keep-data --quiet  &:: program only
```

It removes the service, the firewall rule, the shortcuts, the Apps & features entry and the
program folder; with `--purge` also `C:\ProgramData\FontRender`. Nothing else is left.

### Build the assets yourself

```bash
python3 scripts/build_release.py dist        # dist/font-render-<version>.zip, install.ps1, SHA256SUMS
```

Copy `dist` to the Windows machine and run `install.ps1 -From <that folder>`. The release
workflow does the same and runs `scripts/smoke_test.ps1` on a Windows runner
(install → render → repair → reset-admin → uninstall).

## Finding the server

The install summary (and web admin → Overview → *Clients connect to*) lists the addresses:

```
Clients connect with one of these (font-render login <address>):
  http://STUDIO-PC:8000
  http://studio-pc.local:8000
  http://192.168.1.23:8000     (Ethernet)
```

- **By name** — keeps working when DHCP gives the server a new IP: Windows clients resolve
  the bare computer name (`STUDIO-PC`), macOS and Linux `studio-pc.local` (mDNS; Linux needs
  Avahi, installed on most desktops). Some managed networks turn these off; then use the IP.
- **By IP** — always works, but a DHCP lease can change it. Give the server a fixed address
  with a **DHCP reservation** in the router (often under *LAN*, *DHCP*, *Address reservation*
  or *Static leases*: pick this computer's MAC address, keep the IP it has now).
- Interfaces such as `vEthernet (WSL)` or `VirtualBox Host-Only` are virtual — clients on the
  LAN use the address of the real `Ethernet` / `Wi-Fi` adapter.
- Nothing answers? The firewall rule allows only **private** networks from the local subnet:
  Settings → Network & internet → the connection → *Network profile type* = **Private**.

## Developer mode

Lets named clients deploy any commit or a working tree to an installed server — for the
people developing it. Off by default; switched on **only on the server**, in an
elevated console:

```cmd
cd "C:\Program Files\FontRender\bin"
font-render-server dev-mode on --client alexander --reason "Server development" [--days 30] [--allow-from 192.168.1.0/24]
font-render-server dev-mode status
font-render-server dev-mode off
```

(`bin\font-render-server.cmd` runs any `font-render-server` subcommand of the current
version.)

- `--client` (repeatable): who may deploy; a missing client is created and its token
  printed once. They get the `dev` scope, which the admin API cannot grant.
- `--reason` is required; `--days` defaults to 30 (`0` = until switched off);
  `--allow-from` limits `/dev/*` to networks.
- Every admin sees a red banner in the web admin and can **Switch off**; on expiry the
  mode ends by itself. The running build stays until someone switches back.

From the developer's checkout, with the printed token in `.env` as `FONT_RENDER_DEV_TOKEN`
(or saved with `font-render login --dev-token <token>`):

```bash
uv run font-render --deploy              # origin/main (fetched first)
uv run font-render --deploy a1b2c3d      # any commit or branch
uv run font-render --deploy --local      # the working tree, uncommitted changes included
uv run font-render --release             # back to a release
```

A deploy runs the update pipeline (no signature — developer mode is the permission) into
`versions\dev-<commit>`; a build that fails to start three times is reverted like an
update. `--local` packs tracked and untracked, non-ignored files; font files are refused on
both sides. While developer mode is off, `/dev/*` answers 404.

## Web admin

Open `http://SERVER:8000/admin/` ([Finding the server](#finding-the-server)) and log in with
an admin token: status, client tokens, recent requests, the service log, update/rollback/
restart. See [admin.md](admin.md).

## Firewall

The installer adds `Font Render Service`: inbound TCP on the port, **private** network
profile, **local subnet** only — the server speaks plain HTTP, keep it off public networks.
If the machine's network is marked *Public* in Windows, change it to *Private* or adjust the
rule. Checkout installs add the rule by hand:

```cmd
netsh advfirewall firewall add rule name="Font Render Service" dir=in action=allow protocol=tcp localport=8000 profile=private remoteip=localsubnet
```

## Configuration

Environment variables in `.env` of the data folder (`C:\ProgramData\FontRender\.env`; checkout: the
project root), or the service environment:

| Variable | Default | Meaning |
|----------|---------|---------|
| `FONT_RENDER_PORT` | `8000` | HTTP port (`FONT_RENDER_HOST`: bind address, `0.0.0.0`) |
| `FONT_RENDER_HOME` | project root | Data folder (set by the installer) |
| `FONT_RENDER_TOKEN` | — | Legacy admin token (form field `token`; checkout installs only) |
| `FONT_RENDER_CLIENTS` | `clients.json` | Client token file ([auth.md](auth.md)) |
| `RENDER_POOL_SIZE` | `2` | Browsers per (engine, ClearType) |
| `RENDER_CONCURRENCY` | `4` | Renders in flight |
| `RENDER_QUEUE_MAX` | `16` | Renders waiting; beyond → `429` |
| `RENDER_CLIENT_MAX` | `4` | In flight + waiting per client; beyond → `429` |
| `RENDER_TIMEOUT_S` | `60` | Per-render timeout → `504`, browser retired |
| `RENDER_RECYCLE_AFTER` | `500` | Renders before a browser is replaced |
| `RENDER_PAGE_CACHE` | `4` | Loaded pages kept per browser for reuse (`0` = off) |
| `RENDER_PAGE_TTL_S` | `600` | Idle cached pages are closed after this |
| `FONT_CACHE_MAX_BYTES` | `536870912` | Font cache size, all clients (LRU) |
| `FONT_CACHE_TTL_S` | `3600` | Idle cached fonts are dropped after this |
| `FONT_RENDER_LOG` | `logs\service.log` | Log file shown in the web admin |
| `FONT_RENDER_SESSION_TTL_S` | `43200` | Web admin session lifetime |
| `FONT_RENDER_STATS` | `data\stats.json` | Statistics file |
| `FONT_RENDER_SETTINGS` | `data\settings.json` | Settings changed in the web admin |
| `OVERLOAD_WAIT_S` | `5` | Queue wait that counts as an overload event |
| `UPDATE_CHECK_HOURS` | `6` | Installed: look for new releases this often (`0` = only when asked) |
| `FONT_RENDER_UPDATE_REPO` | `typedev/font-render-server` | Installed: where releases come from |
| `FONT_RENDER_APP` | — | Program folder (set for the service by the installer) |
| `FONT_RENDER_SESSION_KEY` | `.session_key` | Web admin session signing key |
| `FONT_RENDER_AUDIT_LOG` | `logs\audit.log` | Audit log |
| `FONT_RENDER_FIREFOX_PREFS` | `data\firefox_prefs.json` | Firefox prefs set in the web admin |

Paths are relative to the data folder.

Changing them in `.env` needs a service restart (Services → Font Render Service → Restart, or
`Restart-Service FontRenderService`). The web admin can change most of them at once and
keeps them in `data\settings.json`, which takes precedence over `.env`
([admin.md](admin.md#configuration)).

## Files on the server

Installed: program in `C:\Program Files\FontRender` (`bin\`, `python\`, `versions\<v>\`,
`browsers\`, `current`), data in `C:\ProgramData\FontRender` — layout in
[distribution.md](distribution.md#on-disk-layout). The data folder holds the same files as
a checkout's project root (where they are not in git), plus `install.json` and, while
developer mode is or was on, `devmode.json`:

| File | Content |
|------|---------|
| `.env` | Configuration |
| `clients.json` | Client token hashes and auth settings |
| `.last_good_commit` | Checkout only: rollback target written by `/update` (installed: `APP\previous`) |
| `.session_key` | Signing key of web admin sessions (delete to end all sessions) |
| `data\settings.json` | Settings changed in the web admin (override `.env`) |
| `data\stats.json` | Traffic per client, per-minute load, overload episodes ([admin.md](admin.md)) |
| `logs\service.log` | Service log: one line per request with the client id, never font names |
| `logs\audit.log` | Installs, admin token resets, updates, rollbacks, developer mode (JSON lines; web admin → Maintenance) |
| `tools\nssm.exe` | NSSM (checkout; installed: `C:\Program Files\FontRender\bin\nssm.exe`) |

The Telegram bot runs on the same host as its own service (`FontRenderBot`, admin port 8001,
its own checkout); it is set up and updated from its repository, not from here.

Fonts are never written to disk: uploads are held in memory, the font cache is RAM-only,
Chromium runs with its disk cache disabled.

## Service management

```cmd
<nssm> status FontRenderService
<nssm> restart FontRenderService
<nssm> get FontRenderService AppParameters
powershell Get-Content <data>\logs\service.log -Tail 20 -Wait
```

`<nssm>` is `"C:\Program Files\FontRender\bin\nssm.exe"` (installed) or `tools\nssm.exe`
(checkout); `<data>` is `C:\ProgramData\FontRender` or the project root.

## Checkout install

For developing on Windows. Servers for real use: [Install](#install).

### Requirements

- Windows 10/11
- Git — https://git-scm.com/download/win
- uv — `powershell -c "irm https://astral.sh/uv/install.ps1 | iex"` (installs Python as needed)

### Steps

```cmd
git clone https://github.com/typedev/font-render-server.git
cd font-render-server
```

Then run **`deploy\windows\setup_service.bat` as administrator** (press Enter when it asks
for a token: the legacy token is not needed). It:

1. checks `git` and `uv`;
2. runs `uv sync` (installs the project and its dependencies);
3. installs Playwright Chromium;
4. downloads NSSM into `tools\`;
5. reads the optional legacy `FONT_RENDER_TOKEN` from `.env`;
6. registers and starts the `FontRenderService` service: `uv run font-render-server` in the
   project root, auto-restart after 5 s, log `logs\service.log` (rotated at 5 MB).

Re-running the script reinstalls the service with the current settings.

### First admin client

A new checkout server allows anonymous rendering until told otherwise. In an elevated
console in the project folder create the admin client; put the printed token into your
client's `.env` as `FONT_RENDER_ADMIN_TOKEN` (or `font-render login --admin-token`), then
create render clients and close anonymous rendering and the legacy token ([auth.md](auth.md)):

```bash
uv run font-render-server reset-admin                     # prints the admin token once
uv run font-render-clients add mylaptop                   # render clients, from any client
uv run font-render-clients settings --anonymous off
uv run font-render-clients settings --legacy-token off
```

For a foreground run (debugging) use `deploy\windows\start_server.bat`, which restarts the
server after it exits.

### Browsers and the SYSTEM account

The service runs as `LocalSystem`, whose Playwright browser cache is
`C:\Windows\System32\config\systemprofile\AppData\Local\ms-playwright` — not the admin
user's. If `/info` shows a browser as not installed, install it from the service context:

```bash
uv run font-render --install-browser chromium
```

### Git access for remote updates

`/update` runs `git pull origin main` as the service; the repository is public, so no
credentials are needed. Whoever can push to the branch the checkout follows runs code on
the host as LocalSystem — prefer the installed layout, whose updates are signed releases.

### Updating a checkout

From the web admin (Maintenance), or from a client checkout with `FONT_RENDER_ADMIN_TOKEN`
(or, while `legacy_admin_token` is on, `FONT_RENDER_TOKEN`) in `.env`:

```bash
uv run font-render --update     # deploy origin/main
uv run font-render --rollback   # back to the previous commit
```

`/update`: save the current commit → `git pull origin main` → `uv sync` → `py_compile` every
module of `src/font_render` (on error: `git reset --hard` to the old commit and report) →
write `.last_good_commit` → exit; NSSM restarts the service; the client polls `/health` until
the new version answers. `/rollback` resets to `.last_good_commit` the same way.

Browsers are warmed up in the background after a restart, so the first requests don't pay
the Chromium start-up cost.

### Migrating a service installed before the package layout

Services installed before October 2026 run `uv run python server.py`, which no longer
exists. Reinstall them with the new entry point:

1. On the server, in the project folder: `git pull`
2. Run `deploy\windows\setup_service.bat` as administrator (it stops, removes and reinstalls
   the service with `uv run font-render-server`)
3. Check: `tools\nssm.exe get FontRenderService AppParameters` → `run font-render-server`,
   and from a client `uv run font-render --info`

## Remote shell (SSH)

OpenSSH Server can give an admin shell from the LAN, for diagnostics and for testing native
(GDI/DirectWrite) code outside the service. Setup, in an elevated PowerShell:

```powershell
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
Start-Service sshd; Set-Service sshd -StartupType Automatic
Set-NetFirewallRule -Name OpenSSH-Server-In-TCP -RemoteAddress LocalSubnet
# admins' keys live here, not in ~/.ssh
Set-Content C:\ProgramData\ssh\administrators_authorized_keys '<public key>'
icacls C:\ProgramData\ssh\administrators_authorized_keys /inheritance:r /grant "Administrators:F" /grant "SYSTEM:F"
# key-only: set "PasswordAuthentication no" in C:\ProgramData\ssh\sshd_config, then
Restart-Service sshd
```

An admin's SSH session is elevated. Do not edit the installed program over SSH (deploy with
`font-render --deploy` in developer mode, or a release); copy code to a scratch folder and run
it with the current version's interpreter
(`C:\Program Files\FontRender\versions\<current>\.venv\Scripts\python.exe`), so a crash in
native code cannot take the service down. The
service account's registry is `HKU\S-1-5-18`. `SystemParametersInfo` set calls have no
effect from an SSH session.

