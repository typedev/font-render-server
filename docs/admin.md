# Web admin

`http://lurkbox.local:8000/admin/` — a browser page for running the server remotely.

## Logging in

Paste an admin token:

- an admin-scope client token (`font-render-clients add ops --scopes render,admin`), or
- the legacy `FONT_RENDER_TOKEN` while `legacy_admin_token` is on.

The server answers with a signed session cookie (`HttpOnly`, `SameSite=Strict`, 12 h,
`FONT_RENDER_SESSION_TTL_S`). It is signed with `.session_key` in the project root (created on
first login, not in git), so sessions survive restarts and updates. A session ends when it
expires, on *Log out*, or when its credential changes: rotating, disabling or removing that
client, changing `FONT_RENDER_TOKEN`, or turning off `legacy_admin_token`. Deleting
`.session_key` ends all sessions.

After 5 failed logins from one IP in 5 minutes, logins from it are refused for the rest of the
window (`429`).

The connection is plain HTTP on the LAN, the same as every other client: the token crosses
the network once at login, the cookie afterwards.

## Tabs

| Tab | Content |
|-----|---------|
| Overview | Version, uptime, render version; load (renders in flight, queue, per-client, render latency p50/p95); memory of the server and browser processes, CPU, RAM, disk; font cache; each browser in the pool (version, renders until recycle, cached pages); per-client activity since restart |
| Load | Verdict for the range (headroom / busy at times / overloaded), utilization of the render slots, peak renders in flight, minutes at full capacity, peak queue, longest queue wait, rejected (429) and timed-out (504) renders, latency; overload episodes; charts over 1 h / 6 h / 24 h / 7 d: renders in flight vs capacity, queue, queue wait, requests, latency p50/p95, traffic, CPU, memory (browsers + free), server process memory |
| Traffic | Bytes in (uploads) and out (responses) per client: today, 7 days, 30 days, total, with request and render counts; stacked daily volume for 30 days |
| Clients | Clients with scopes, status, request/error counts, last seen and IP; add, rotate, disable/enable, remove (new tokens are shown once); access settings (anonymous rendering, legacy admin token) |
| Requests | The last 500 requests (in memory): time, client, IP, request, status, duration; filter by client or errors |
| Logs | Tail of `logs\service.log` with a filter and auto-refresh |
| Maintenance | Check for updates — installed: newer releases of the update channel with their notes, the channel (stable / beta); checkout: pending commits on `origin/main`, recent history, local changes; update (installed: progress of download, signature check, install, self-check), roll back, restart; recycle browsers; clear the font cache; install a browser; configuration (editable, see below) |

While developer mode is on, a red banner on every tab says since when, until when, who
switched it on and why, which clients may deploy, and the running build; **Switch off**
ends it for everyone (it can only be switched on again on the server). Maintenance →
**Audit log** lists installs, admin token resets, updates, rollbacks and developer-mode
events.

Destructive buttons ask for a second click. Update, rollback and restart show an overlay
until the server answers again with a new start time.

## Statistics

Like the log, statistics hold only time, IP, client id, method, path, status, duration and
byte counts — never font names, hashes or contents.

- **Traffic** — request and response body bytes per client, per day (kept 400 days) and in
  total. Admin page reads count toward the admin client's traffic.
- **Load** — one bucket per minute (kept 7 days): requests, renders, errors, rejected and
  throttled renders, timeouts, bytes, render latency p50/p95/max, longest queue wait, peak and
  average renders in flight and peak queue (the pool is sampled every second), CPU, and memory
  sampled every 15 s: peak RSS of the server process and of its browsers (Playwright driver +
  Chromium; Windows counts shared pages in every process, so this overstates), lowest free
  memory of the machine. The server process lives until a restart, so steady growth of its
  chart over days is a leak; browsers drop back when they are recycled.
- **Overload episodes** — an episode starts on the first overload event and closes after
  60 s without one. Events: a render rejected because the queue is full (429), a timeout (504),
  a server error, or a queue wait of at least `OVERLOAD_WAIT_S` (default 5 s). Timeouts, errors
  and full-queue rejects make an episode *critical*, a slow queue alone a *warning*. Each
  episode keeps its event counts, affected clients, peak queue, peak renders in flight and
  longest wait (last 500 episodes). Start and end are also logged as one `WARNING` line each.
  A client that hits its own limit (`RENDER_CLIENT_MAX`, 429) is counted as *throttled*, not
  as an overload.

Traffic, minutes and episodes are saved to `data\stats.json` (`FONT_RENDER_STATS`) every
minute and on restart, so they survive updates; a busy week is about 1.5 MB. Recent requests
and the "since restart" activity are in memory only. The page's own polling (`GET /admin/*`)
and `/health` are left out of the request list, the load and the log.

## Configuration

Maintenance → Configuration edits the server limits (`RENDER_*`, `FONT_CACHE_*`, `MAX_FONTS`,
`MAX_FONT_SIZE`, `MAX_PPEM_VALUES`, `OVERLOAD_WAIT_S`, `FONT_RENDER_SESSION_TTL_S`). Changes
are validated against a range and saved on the server in `data\settings.json`, which takes
precedence over `.env` and the service environment. *Reset* removes the override, so the
`.env` or built-in value applies again.

All of them apply at once, except `RENDER_CONCURRENCY`: it is shown as *after restart* until
the service restarts. Lowering `RENDER_POOL_SIZE` takes effect as browsers are recycled
(Recycle browsers does it now). Lowering `FONT_CACHE_MAX_BYTES` evicts least recently used
fonts at once. Changing `MAX_FONTS`, `MAX_FONT_SIZE` or `MAX_PPEM_VALUES` changes what clients
see in `/info` limits. Each change is logged with the admin's id.

## Lockout protection

The server refuses (`409`) to:

- turn off `legacy_admin_token` while no enabled admin client exists;
- disable or remove the last enabled admin client while `legacy_admin_token` is off.

## Security

- The cookie works only on admin paths (`/admin/*`, `/update`, `/rollback`,
  `/browsers/install`), never for `/render` or `/fonts`.
- A `POST` authenticated by the cookie must carry an `Origin` (or `Referer`) of this server,
  otherwise `403` (CSRF).
- The page is served with a strict Content-Security-Policy (`default-src 'self'`, no
  framing). It inserts all server data as text, never as HTML.

## Endpoints

These endpoints back the page. Each also accepts an admin Bearer token, so scripts can call
them too.

| Endpoint | Effect |
|----------|--------|
| `GET /admin/` | The page (public; data needs a session) |
| `POST /admin/login` (`token`) / `POST /admin/logout` | Start / end a session |
| `GET /admin/api/me` | Who is logged in |
| `GET /admin/api/status` | Everything shown on Overview, Clients and Maintenance; `windows_text` = the service account's font smoothing (on lurkbox: ClearType, RGB, contrast 1200) |
| `GET /admin/api/requests?limit=&client=` | Recent requests, newest first |
| `GET /admin/api/logs?lines=&q=` | Log tail; `q` filters (case-insensitive) |
| `GET /admin/api/load?minutes=` | Per-minute load (max 7 days), overload episodes |
| `GET /admin/api/traffic` | Bytes and counts per client: today, 7 / 30 days, total, per day |
| `GET /admin/api/config` / `POST /admin/api/config` (`NAME=value`, empty = reset) | Editable settings |
| `GET /admin/api/firefox-prefs` / `POST` (JSON `{"prefs": {...}}`) | Firefox `firefox_user_prefs` (`data\firefox_prefs.json`); Firefox is recycled on change |
| `POST /admin/api/check-updates` | Installed: `{"mode": "release", current, previous, channel, available: [{version, prerelease, published, notes, url}], error, job}`. Checkout: `git fetch`; `{"mode": "git", pending, recent, dirty}` |
| `GET /admin/api/update-status` | The update job: `{state: idle\|running\|failed\|restarting, version, step, error, log}` |
| `POST /admin/api/update-channel` (`channel`) | Installed: `stable` or `beta` (kept in `install.json`) |
| `POST /admin/api/dev-mode/off` | Switch developer mode off (there is no endpoint to switch it on) |
| `GET /admin/api/audit?lines=` | Audit log entries, newest first |
| `POST /admin/restart` | Exit; NSSM restarts the service |
| `POST /admin/browsers/recycle` | Retire all browsers; new ones start on demand |
| `POST /admin/fonts/clear` | Drop every client's cached fonts |
