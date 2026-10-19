"""Web admin: a static page (/admin/) on top of JSON endpoints.

Login exchanges an admin token (admin-scope client or the legacy token) for a
signed HttpOnly session cookie (auth.issue_session); AuthMiddleware turns the
cookie into an admin client on /admin/* and the other admin paths, and rejects
cookie-authenticated POSTs from another origin. Every endpoint here also accepts
a Bearer admin token, so the CLI can use them.
"""

import asyncio
import json
import os
import platform
import shutil
import time
from collections import deque
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from . import admin, auth, config, devmode, pool, updates, winenv
from .stats import memory_usage, stats

router = APIRouter()

STATIC_DIR = Path(__file__).resolve().parent / "static"
_STATIC_FILES = {"admin.css": "text/css", "admin.js": "text/javascript"}
_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'; base-uri 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
}

LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW_S = 300
_login_failures: dict[str, deque] = {}


def _deny(request: Request) -> JSONResponse | None:
    return auth.check_admin(request.scope["state"], "")


# ---------------------------------------------------------------------------
# Page and session
# ---------------------------------------------------------------------------

@router.get("/admin", include_in_schema=False)
async def admin_redirect():
    return RedirectResponse("/admin/", status_code=307)


@router.get("/admin/", include_in_schema=False)
async def admin_page():
    return FileResponse(STATIC_DIR / "admin.html", media_type="text/html", headers=_SECURITY_HEADERS)


@router.get("/admin/static/{name}", include_in_schema=False)
async def admin_static(name: str):
    if name not in _STATIC_FILES:
        return JSONResponse(status_code=404, content={"error": "not found"})
    return FileResponse(STATIC_DIR / name, media_type=_STATIC_FILES[name], headers=_SECURITY_HEADERS)


@router.post("/admin/login")
async def admin_login(request: Request, token: str = Form("")):
    ip = (request.client.host if request.client else "-")
    now = time.monotonic()
    failures = _login_failures.setdefault(ip, deque())
    while failures and now - failures[0] > LOGIN_WINDOW_S:
        failures.popleft()
    if len(failures) >= LOGIN_MAX_FAILURES:
        resp = JSONResponse(status_code=429, content={"error": "too many failed logins, try later"})
        resp.headers["Retry-After"] = str(int(LOGIN_WINDOW_S - (now - failures[0])) + 1)
        return resp
    subject = auth.login_subject(token.strip())
    if subject is None:
        failures.append(now)
        config.log.warning("web admin: failed login from %s", ip)
        return JSONResponse(status_code=401, content={"error": "not an admin token"})
    failures.clear()
    config.log.info("web admin: login %s from %s", subject, ip)
    resp = JSONResponse(content={"subject": subject})
    resp.set_cookie(auth.SESSION_COOKIE, auth.issue_session(subject), max_age=int(config.SESSION_TTL_S),
                    httponly=True, samesite="strict", path="/")
    return resp


@router.post("/admin/logout")
async def admin_logout():
    resp = JSONResponse(content={"status": "logged out"})
    resp.delete_cookie(auth.SESSION_COOKIE, path="/")
    return resp


@router.get("/admin/api/me")
async def admin_me(request: Request):
    if err := _deny(request):
        return err
    client = request.scope["state"]["client"]
    return {"subject": client[0], "via": "session" if request.scope["state"].get("session") else "bearer"}


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

_git_cache: tuple[float, dict] | None = None


async def _git_info_cached() -> dict:
    global _git_cache
    if _git_cache is None or time.monotonic() - _git_cache[0] > 60:
        _git_cache = (time.monotonic(), await admin.git_info())
    return _git_cache[1]


def _process_stats() -> dict | None:
    mem = memory_usage()
    if mem is None:
        return None
    import psutil
    return {**mem, "cpu_percent": psutil.cpu_percent(interval=None)}


@router.get("/admin/api/status")
async def admin_status(request: Request):
    if err := _deny(request):
        return err
    bpool: pool.BrowserPool = request.app.state.pool
    fcache = request.app.state.font_cache
    git = await _git_info_cached()
    disk = shutil.disk_usage(config.HOME)
    if config.APP is not None:
        rollback = updates.layout().read_pointer("previous")
    else:
        rollback = config.LAST_GOOD_COMMIT.read_text().strip()[:12] if config.LAST_GOOD_COMMIT.exists() else None
    clients = auth.registry.clients()
    return {
        "subject": request.scope["state"]["client"][0],
        "server": {
            "version": git["commit"],
            "branch": git["branch"],
            "commit_date": git["commit_date"],
            "render_version": config.RENDER_VERSION,
            "platform": platform.platform(),
            "python": platform.python_version(),
            "started": stats.started,
            "uptime_s": time.time() - stats.started,
            "rollback_target": rollback,
            "layout": "checkout" if config.APP is None else "installed",
            "dev_mode": devmode.view(),
        },
        "browsers": {"installed": await pool.browsers_installed(), "slots": bpool.slots_info()},
        "pool": bpool.info(),
        "in_flight_per_client": dict(bpool.per_client),
        "font_cache": {**fcache.stats(), "max_bytes": config.FONT_CACHE_MAX_BYTES,
                       "ttl_s": config.FONT_CACHE_TTL_S},
        "render_latency": stats.render_latency(),
        "process": _process_stats(),
        "disk": {"total": disk.total, "free": disk.free},
        "windows": winenv.info(),
        "settings": auth.registry.settings(),
        "clients": [
            {**admin._client_view(k, v), **stats.clients.get(k, {})} for k, v in sorted(clients.items())
        ],
        "activity": stats.clients,  # includes ids no longer registered and "anonymous"
        "config": {x["name"]: x["value"] for x in config.settings_view()},
        "pending_restart": [x["name"] for x in config.settings_view() if x["pending_restart"]],
    }


@router.get("/admin/api/config")
async def admin_config(request: Request):
    if err := _deny(request):
        return err
    return {"settings": config.settings_view()}


@router.post("/admin/api/config")
async def admin_config_set(request: Request):
    """Form fields NAME=value set a setting; an empty value resets it to the env/default."""
    if err := _deny(request):
        return err
    form = await request.form()
    changes = {k: str(v).strip() for k, v in form.items() if k in config.EDITABLE}
    if not changes:
        return JSONResponse(status_code=400, content={"error": "no known settings in the request"})
    try:  # validate everything before changing anything
        for name, raw in changes.items():
            if raw:
                config.coerce_setting(name, raw)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    who = request.scope["state"]["client"][0]
    for name, raw in changes.items():
        before = getattr(config, name)
        config.set_setting(name, raw or None)
        config.log.info("setting %s: %s -> %s by %s%s", name, before, getattr(config, name), who,
                        "" if config.EDITABLE[name][3] else " (after restart)")
    return {"settings": config.settings_view()}


@router.get("/admin/api/firefox-prefs")
async def admin_firefox_prefs(request: Request):
    if err := _deny(request):
        return err
    return {"prefs": pool.firefox_prefs()}


@router.post("/admin/api/firefox-prefs")
async def admin_firefox_prefs_set(request: Request):
    """JSON body {"prefs": {name: bool|int|str}} replaces the prefs; Firefox is restarted."""
    if err := _deny(request):
        return err
    try:
        prefs = (await request.json())["prefs"]
        if not isinstance(prefs, dict) or not all(
                isinstance(k, str) and isinstance(v, (bool, int, str)) for k, v in prefs.items()):
            raise ValueError
    except (ValueError, KeyError, TypeError):
        return JSONResponse(status_code=400, content={"error": 'body must be {"prefs": {name: bool|int|str}}'})
    path = config.FIREFOX_PREFS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(prefs, indent=2, sort_keys=True), encoding="utf-8")
    n = request.app.state.pool.recycle("firefox")
    config.log.info("firefox prefs set by %s (%d prefs), %d browsers recycled",
                    request.scope["state"]["client"][0], len(prefs), n)
    return {"prefs": prefs, "recycled": n}


@router.get("/admin/api/requests")
async def admin_requests(request: Request, limit: int = 200, client: str = ""):
    if err := _deny(request):
        return err
    rows = [r for r in stats.recent if not client or r["client"] == client]
    return {"requests": rows[-max(1, min(limit, len(stats.recent) or 1)):][::-1]}


@router.get("/admin/api/load")
async def admin_load(request: Request, minutes: int = 60):
    """Per-minute load of the last `minutes` (max 7 days) and overload episodes."""
    if err := _deny(request):
        return err
    minutes = max(1, min(minutes, 7 * 24 * 60))
    since = time.time() - minutes * 60
    episodes = stats.episodes_view()
    return {
        "minutes": stats.minutes_view(minutes),
        "concurrency": config.CONCURRENCY,
        "queue_max": config.QUEUE_MAX,
        "overload_wait_s": config.OVERLOAD_WAIT_S,
        "episodes": episodes[:200],
        "episodes_in_range": [e for e in episodes if e["end"] >= since],
    }


@router.get("/admin/api/traffic")
async def admin_traffic(request: Request):
    """Bytes in/out, requests and renders per client: today, 7 and 30 days, total, per day."""
    if err := _deny(request):
        return err
    return {"clients": stats.traffic_view()}


def _tail(path: Path, lines: int, max_bytes: int = 4 * 1024 * 1024) -> list[str]:
    """Last `lines` lines of a file, reading backwards in blocks."""
    with path.open("rb") as f:
        f.seek(0, os.SEEK_END)
        pos = f.tell()
        data = b""
        while pos > 0 and data.count(b"\n") <= lines and len(data) < max_bytes:
            step = min(64 * 1024, pos)
            pos -= step
            f.seek(pos)
            data = f.read(step) + data
    return data.decode("utf-8", errors="replace").splitlines()[-lines:]


@router.get("/admin/api/logs")
async def admin_logs(request: Request, lines: int = 300, q: str = ""):
    if err := _deny(request):
        return err
    lines = max(1, min(lines, 5000))
    path = config.LOG_FILE
    if not path.is_file():
        return {"file": str(path), "exists": False, "lines": []}
    st = path.stat()
    # Filter over a wider window so a search still finds older matches
    out = await asyncio.to_thread(_tail, path, lines * 10 if q else lines)
    if q:
        needle = q.lower()
        out = [x for x in out if needle in x.lower()][-lines:]
    return {"file": str(path), "exists": True, "size": st.st_size, "mtime": st.st_mtime, "lines": out}


# ---------------------------------------------------------------------------
# Maintenance
# ---------------------------------------------------------------------------

_LOG_FORMAT = "--format=%h%x09%ci%x09%an%x09%s"


def _parse_log(out: str) -> list[dict]:
    rows = []
    for line in out.splitlines():
        parts = line.split("\t", 3)
        if len(parts) == 4:
            rows.append(dict(zip(("commit", "date", "author", "subject"), parts)))
    return rows


@router.post("/admin/api/check-updates")
async def admin_check_updates(request: Request):
    if err := _deny(request):
        return err
    if config.APP is not None:
        return await updates.check(force=True)
    r = await admin._run_git("fetch", "origin", "main")
    if r.returncode != 0:
        return JSONResponse(status_code=502, content={"error": f"git fetch failed: {r.stderr.strip()}"})
    pending = await admin._run_git("log", _LOG_FORMAT, "HEAD..origin/main")
    recent = await admin._run_git("log", "-10", _LOG_FORMAT, "HEAD")
    dirty = await admin._run_git("status", "--porcelain", "--untracked-files=no")
    return {
        "mode": "git",
        "pending": _parse_log(pending.stdout),
        "recent": _parse_log(recent.stdout),
        "dirty": [x for x in dirty.stdout.splitlines() if x.strip()],
    }


@router.get("/admin/api/update-status")
async def admin_update_status(request: Request):
    if err := _deny(request):
        return err
    return updates.job.view()


@router.post("/admin/api/dev-mode/off")
async def admin_dev_mode_off(request: Request):
    """Any admin may switch developer mode off; switching it on is local only."""
    if err := _deny(request):
        return err
    was_on = devmode.disable(auth.registry, request.scope["state"]["client"][0])
    return {"enabled": False, "was_on": was_on}


@router.get("/admin/api/audit")
async def admin_audit(request: Request, lines: int = 100):
    if err := _deny(request):
        return err
    try:
        rows = config.AUDIT_LOG.read_text(encoding="utf-8").splitlines()[-max(1, min(lines, 1000)):]
    except FileNotFoundError:
        rows = []
    out = []
    for row in reversed(rows):
        try:
            out.append(json.loads(row))
        except ValueError:
            continue
    return {"entries": out}


@router.post("/admin/api/update-channel")
async def admin_update_channel(request: Request, channel: str = Form(...)):
    if err := _deny(request):
        return err
    if config.APP is None:
        return JSONResponse(status_code=409, content={"error": "checkout: updates come from git"})
    try:
        updates.set_channel(channel)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    config.log.info("update channel: %s", channel)
    return {"channel": channel}


@router.post("/admin/restart")
async def admin_restart(request: Request):
    if err := _deny(request):
        return err
    config.log.info("restart requested by %s", request.scope["state"]["client"][0])
    asyncio.get_event_loop().call_later(1.0, admin._shutdown)
    return {"status": "restarting"}


@router.post("/admin/browsers/recycle")
async def admin_recycle(request: Request):
    if err := _deny(request):
        return err
    n = request.app.state.pool.recycle()
    config.log.info("browsers recycled: %d", n)
    return {"recycled": n}


@router.post("/admin/fonts/clear")
async def admin_fonts_clear(request: Request):
    if err := _deny(request):
        return err
    n = request.app.state.font_cache.clear()
    config.log.info("font cache cleared")
    return {"deleted": n}
