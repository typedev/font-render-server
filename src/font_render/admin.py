"""Admin endpoints: client tokens, server settings, browser install, self-update."""

import asyncio
import os
import py_compile
import re
import subprocess
import sys
import time
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse

from . import auth, config

router = APIRouter()

# ---------------------------------------------------------------------------
# Client management (admin scope, or the legacy form token while enabled)
# ---------------------------------------------------------------------------
_CLIENT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")


def _admin_clients(clients: dict, without: str | None = None) -> list[str]:
    return [k for k, c in clients.items()
            if k != without and not c.get("disabled") and "admin" in c.get("scopes", [])]


def _lockout_error() -> JSONResponse:
    return JSONResponse(status_code=409, content={
        "error": "would lock out admin access: keep the legacy admin token enabled "
                 "or at least one enabled admin client"})


def _client_view(client_id: str, c: dict) -> dict:
    return {"client_id": client_id, "scopes": c.get("scopes", []),
            "created": c.get("created"), "disabled": bool(c.get("disabled"))}


@router.post("/admin/clients/list")
async def admin_clients_list(request: Request, token: str = Form("")):
    if err := auth.check_admin(request.scope["state"], token):
        return err
    return {"clients": [_client_view(k, v) for k, v in sorted(auth.registry.clients().items())],
            "settings": auth.registry.settings()}


@router.post("/admin/clients/add")
async def admin_clients_add(
    request: Request,
    client_id: str = Form(...),
    scopes: str = Form("render", description="Comma-separated: render, admin"),
    token: str = Form(""),
):
    if err := auth.check_admin(request.scope["state"], token):
        return err
    scope_list = [x.strip() for x in scopes.split(",") if x.strip()]
    if not _CLIENT_ID_RE.match(client_id) or not scope_list or any(x not in auth.SCOPES for x in scope_list):
        return JSONResponse(status_code=400, content={"error": "invalid client_id or scopes"})
    clients = auth.registry.clients()
    if client_id in clients:
        return JSONResponse(status_code=409, content={"error": "client exists"})
    client = {"scopes": scope_list, "created": time.strftime("%Y-%m-%dT%H:%M:%S"), "disabled": False}
    new_token = auth.new_token(client)
    clients[client_id] = client
    auth.registry.save()
    config.log.info("client added: %s scopes=%s", client_id, ",".join(scope_list))
    return {**_client_view(client_id, client), "token": new_token}


@router.post("/admin/clients/{action}")
async def admin_clients_action(
    request: Request, action: str, client_id: str = Form(...), token: str = Form(""),
):
    if err := auth.check_admin(request.scope["state"], token):
        return err
    if action not in ("rotate", "disable", "enable", "remove"):
        return JSONResponse(status_code=404, content={"error": "unknown action"})
    clients = auth.registry.clients()
    client = clients.get(client_id)
    if client is None:
        return JSONResponse(status_code=404, content={"error": "unknown client"})
    if (action in ("disable", "remove") and not auth.registry.settings().get("legacy_admin_token", True)
            and not _admin_clients(clients, without=client_id)):
        return _lockout_error()
    result = {}
    if action == "rotate":
        result["token"] = auth.new_token(client)
    elif action == "remove":
        del clients[client_id]
    else:
        client["disabled"] = action == "disable"
    auth.registry.save()
    config.log.info("client %s: %s", action, client_id)
    return {**_client_view(client_id, client), **result}


@router.post("/admin/settings")
async def admin_settings(
    request: Request,
    allow_anonymous_render: bool | None = Form(None),
    legacy_admin_token: bool | None = Form(None),
    token: str = Form(""),
):
    if err := auth.check_admin(request.scope["state"], token):
        return err
    settings = auth.registry.settings()
    if legacy_admin_token is False and not _admin_clients(auth.registry.clients()):
        return _lockout_error()
    if allow_anonymous_render is not None:
        settings["allow_anonymous_render"] = allow_anonymous_render
    if legacy_admin_token is not None:
        settings["legacy_admin_token"] = legacy_admin_token
    auth.registry.save()
    config.log.info("settings: %s", settings)
    return {"settings": settings}


# ---------------------------------------------------------------------------
# Auto-update helpers
# ---------------------------------------------------------------------------

async def _run_git(*args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    proc = await asyncio.create_subprocess_exec(
        "git", "-c", f"safe.directory={config.SERVICE_DIR.as_posix()}", *args,
        cwd=config.SERVICE_DIR,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        return subprocess.CompletedProcess(
            args=["git", *args], returncode=1,
            stdout="", stderr="timeout",
        )
    return subprocess.CompletedProcess(
        args=["git", *args], returncode=proc.returncode,
        stdout=stdout.decode(errors="replace"),
        stderr=stderr.decode(errors="replace"),
    )


async def _run_cmd(*args: str, timeout: float = 60) -> subprocess.CompletedProcess:
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=config.SERVICE_DIR,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        return subprocess.CompletedProcess(
            args=list(args), returncode=1,
            stdout="", stderr="timeout",
        )
    return subprocess.CompletedProcess(
        args=list(args), returncode=proc.returncode,
        stdout=stdout.decode(errors="replace"),
        stderr=stderr.decode(errors="replace"),
    )


async def git_info() -> dict:
    """Return current commit, branch, and commit date.

    In an installed layout there is no git: "commit" is the version directory
    (1.2.0, dev-a1b2c3d) and "branch" is "installed".
    """
    if config.APP is not None:
        return {"commit": config.SERVICE_DIR.name, "branch": "installed", "commit_date": "unknown"}
    info = {}
    try:
        r = await _run_git("rev-parse", "--short", "HEAD")
        info["commit"] = r.stdout.strip() if r.returncode == 0 else "unknown"
        r = await _run_git("rev-parse", "--abbrev-ref", "HEAD")
        info["branch"] = r.stdout.strip() if r.returncode == 0 else "unknown"
        r = await _run_git("log", "-1", "--format=%ci")
        info["commit_date"] = r.stdout.strip() if r.returncode == 0 else "unknown"
    except Exception:
        info.setdefault("commit", "unknown")
        info.setdefault("branch", "unknown")
        info.setdefault("commit_date", "unknown")
    return info


def _shutdown():
    """Exit the process. NSSM or start_server.bat will auto-restart."""
    from .stats import stats
    stats.save()  # os._exit skips the lifespan shutdown
    os._exit(0)


def _verify_syntax() -> tuple[bool, str]:
    """Compile every server module via py_compile."""
    for path in sorted(Path(__file__).resolve().parent.glob("*.py")):
        try:
            py_compile.compile(str(path), doraise=True)
        except py_compile.PyCompileError as e:
            return False, str(e)
    return True, ""



# ---------------------------------------------------------------------------
# Browser management
# ---------------------------------------------------------------------------

@router.post("/browsers/install")
async def install_browser(
    request: Request,
    browser: str = Form(..., description="Browser to install: chromium, firefox, or webkit"),
    token: str = Form(""),
):
    """Install a Playwright browser on the server."""
    err = auth.check_admin(request.scope["state"], token)
    if err:
        return err

    if browser not in config.SUPPORTED_BROWSERS:
        return JSONResponse(
            status_code=400,
            content={"error": f"Unsupported browser: {browser}. Use one of {list(config.SUPPORTED_BROWSERS)}"},
        )

    try:
        r = await _run_cmd(sys.executable, "-m", "playwright", "install", browser, timeout=120)
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"status": "error", "browser": browser, "stderr": str(e)},
        )
    if r.returncode != 0:
        return JSONResponse(
            status_code=500,
            content={
                "status": "error",
                "browser": browser,
                "stderr": r.stderr.strip(),
                "stdout": r.stdout.strip(),
            },
        )
    return JSONResponse(content={
        "status": "installed",
        "browser": browser,
        "stdout": r.stdout.strip(),
    })


# ---------------------------------------------------------------------------
# Auto-update endpoints
# ---------------------------------------------------------------------------

def _admin_name(request: Request) -> str:
    client = request.scope["state"].get("client")
    return client[0] if client else "(legacy)"


@router.post("/update")
async def update(request: Request, token: str = Form(""), version: str = Form("")):
    """Installed: update to `version` or the newest release of the channel (background job,
    then restart). Checkout: pull latest code from git, verify, and restart."""
    err = auth.check_admin(request.scope["state"], token)
    if err:
        return err
    if config.APP is not None:
        from . import updates
        try:
            release = await updates.find_release(version or None)
        except updates.UpdateError as e:
            if str(e) == "up-to-date":
                return {"status": "up-to-date", "version": config.SERVICE_DIR.name,
                        "commit": config.SERVICE_DIR.name}
            return JSONResponse(status_code=409, content={"error": str(e)})
        try:
            view = await updates.start_update(release, _admin_name(request))
        except updates.UpdateError as e:
            return JSONResponse(status_code=409, content={"error": str(e)})
        return JSONResponse(status_code=202, content={
            "status": "started", "old_version": config.SERVICE_DIR.name,
            "new_version": release["version"], "job": view})

    # Save current commit for rollback
    r = await _run_git("rev-parse", "HEAD")
    if r.returncode != 0:
        return JSONResponse(status_code=500, content={"error": "Not a git repo"})
    old_commit = r.stdout.strip()

    # Pull latest
    r = await _run_git("pull", "origin", "main")
    if r.returncode != 0:
        return JSONResponse(
            status_code=500,
            content={"error": f"git pull failed: {r.stderr.strip()}"},
        )
    pull_output = r.stdout.strip()

    # Check if anything changed
    r = await _run_git("rev-parse", "HEAD")
    new_commit = r.stdout.strip() if r.returncode == 0 else "unknown"

    if old_commit == new_commit:
        return JSONResponse(content={
            "status": "up-to-date",
            "commit": new_commit[:12],
            "message": pull_output,
        })

    # Run uv sync in case dependencies changed
    try:
        await _run_cmd("uv", "sync", timeout=60)
    except Exception:
        pass  # uv sync failure is not fatal

    # Verify syntax of the server package
    ok, syntax_err = _verify_syntax()
    if not ok:
        # Rollback
        await _run_git("reset", "--hard", old_commit)
        try:
            await _run_cmd("uv", "sync", timeout=60)
        except Exception:
            pass
        return JSONResponse(
            status_code=500,
            content={
                "error": "Syntax error in server code, rolled back",
                "details": syntax_err,
                "rolled_back_to": old_commit[:12],
            },
        )

    # Save old commit for manual rollback
    config.LAST_GOOD_COMMIT.write_text(old_commit)

    # Schedule restart after response is sent
    loop = asyncio.get_event_loop()
    loop.call_later(1.5, _shutdown)

    return JSONResponse(content={
        "status": "updated",
        "old_commit": old_commit[:12],
        "new_commit": new_commit[:12],
        "message": pull_output,
        "restarting": True,
    })


@router.post("/rollback")
async def rollback(request: Request, token: str = Form("")):
    """Rollback to the previous known-good commit."""
    err = auth.check_admin(request.scope["state"], token)
    if err:
        return err
    if config.APP is not None:
        from . import updates
        try:
            target = updates.rollback(_admin_name(request))
        except updates.UpdateError as e:
            return JSONResponse(status_code=409, content={"error": str(e)})
        asyncio.get_event_loop().call_later(1.5, _shutdown)
        return {"status": "rolled_back", "version": target, "commit": target, "restarting": True}

    if not config.LAST_GOOD_COMMIT.exists():
        return JSONResponse(
            status_code=404,
            content={"error": "No rollback commit found (.last_good_commit missing)"},
        )

    target = config.LAST_GOOD_COMMIT.read_text().strip()
    if not target:
        return JSONResponse(
            status_code=404,
            content={"error": "Rollback commit file is empty"},
        )

    # Reset to target commit
    r = await _run_git("reset", "--hard", target)
    if r.returncode != 0:
        return JSONResponse(
            status_code=500,
            content={"error": f"git reset failed: {r.stderr.strip()}"},
        )

    # Run uv sync
    try:
        await _run_cmd("uv", "sync", timeout=60)
    except Exception:
        pass

    # Remove the rollback marker
    config.LAST_GOOD_COMMIT.unlink(missing_ok=True)

    # Schedule restart
    loop = asyncio.get_event_loop()
    loop.call_later(1.5, _shutdown)

    return JSONResponse(content={
        "status": "rolled_back",
        "commit": target[:12],
        "restarting": True,
    })
