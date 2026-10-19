"""Developer mode: deploy uploaded code to an installed server (docs/distribution.md#developer-mode).

Off by default. Only a local administrator switches it on (`font-render-server dev-mode on`,
which writes HOME\\devmode.json — writable by administrators only); there is no HTTP
endpoint that enables, extends or widens it. Any admin can switch it off (web admin), and it
expires (default 30 days). While it is on, the clients it names get the `dev` scope, which
the admin API cannot grant, and may call /dev/*. Off, expired or from an address outside
`allow_from`, /dev/* answers 404 as if it did not exist.
"""

import asyncio
import ipaddress
import json
import time

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse

from . import auth, config, updates
from .localadmin import audit, local_user, require_elevated

router = APIRouter()

DEFAULT_DAYS = 30
MAX_UPLOAD = 50 * 1024 * 1024
DEV_SCOPE = "dev"
_TIME = "%Y-%m-%dT%H:%M:%S"


def load() -> dict:
    try:
        return json.loads(config.DEVMODE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(data: dict) -> None:
    config.DEVMODE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = config.DEVMODE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(config.DEVMODE_FILE)


def _set_dev_scope(registry: auth.ClientRegistry, clients: list[str], on: bool) -> dict[str, str]:
    """Add (creating missing clients) or remove the dev scope; returns tokens of new clients."""
    tokens = {}
    all_clients = registry.clients()
    for client_id in (clients if on else list(all_clients)):
        c = all_clients.get(client_id)
        if c is None:
            if not on:
                continue
            c = all_clients[client_id] = {"scopes": [], "created": time.strftime(_TIME), "disabled": False}
            tokens[client_id] = auth.new_token(c)
        scopes = [s for s in c.get("scopes", []) if s != DEV_SCOPE]
        c["scopes"] = scopes + [DEV_SCOPE] if on else scopes
    registry.path.parent.mkdir(parents=True, exist_ok=True)
    registry.save()
    return tokens


def active() -> dict | None:
    """The developer mode state while it is on and not expired, else None (expiry is recorded)."""
    data = load()
    if not data.get("enabled"):
        return None
    until = data.get("until")
    if until and time.strftime(_TIME) >= until:
        data.update(enabled=False, expired=True)
        _save(data)
        _set_dev_scope(auth.registry, [], on=False)
        audit("dev-mode-expired", since=data.get("since"), until=until)
        return None
    return data


def view() -> dict:
    data = active()
    if data is None:
        last = load()
        return {"enabled": False, **({"expired": last.get("until")} if last.get("expired") else {})}
    return {"enabled": True, **{k: data.get(k) for k in ("since", "until", "by", "reason", "clients", "allow_from")}}


def enable(registry: auth.ClientRegistry, clients: list[str], reason: str, days: float,
           allow_from: list[str], by: str) -> dict[str, str]:
    for cidr in allow_from:
        ipaddress.ip_network(cidr, strict=False)  # ValueError on a typo
    now = time.time()
    data = {"enabled": True, "since": time.strftime(_TIME, time.localtime(now)),
            "until": time.strftime(_TIME, time.localtime(now + days * 86400)) if days > 0 else None,
            "by": by, "reason": reason, "clients": clients, "allow_from": allow_from}
    _set_dev_scope(registry, list(registry.clients()), on=False)
    tokens = _set_dev_scope(registry, clients, on=True)
    _save(data)
    audit("dev-mode-on", by=by, reason=reason, clients=clients, until=data["until"], allow_from=allow_from)
    return tokens


def disable(registry: auth.ClientRegistry, by: str) -> bool:
    data = load()
    was_on = bool(data.get("enabled"))
    data.update(enabled=False, expired=False, disabled_by=by, disabled=time.strftime(_TIME))
    _save(data)
    _set_dev_scope(registry, [], on=False)
    if was_on:
        audit("dev-mode-off", by=by)
    return was_on


def _peer_allowed(data: dict, host: str | None) -> bool:
    nets = data.get("allow_from") or []
    if not nets:
        return True
    try:
        ip = ipaddress.ip_address(host or "")
    except ValueError:
        return False
    return any(ip in ipaddress.ip_network(n, strict=False) for n in nets)


def _gate(request: Request) -> tuple[str, dict] | JSONResponse:
    """(client id, state) of an allowed dev call, or the error response."""
    data = active() if config.APP is not None else None
    peer = request.client.host if request.client else None
    if data is None or not _peer_allowed(data, peer):
        return JSONResponse(status_code=404, content={"detail": "Not Found"})
    client = request.scope["state"].get("client")
    if client is None:
        return JSONResponse(status_code=401, content={"error": "unauthorized"})
    if DEV_SCOPE not in client[1] or client[0] not in (data.get("clients") or []):
        return JSONResponse(status_code=403, content={"error": "forbidden"})
    return client[0], data


@router.get("/dev/status")
async def dev_status(request: Request):
    gate = _gate(request)
    if isinstance(gate, JSONResponse):
        return gate
    lay = updates.layout()
    return {"dev_mode": view(), "current": lay.read_pointer("current"),
            "previous": lay.read_pointer("previous"), "job": updates.job.view()}


@router.post("/dev/deploy")
async def dev_deploy(request: Request, archive: UploadFile | None = File(None),
                     commit: str = Form(""), dirty: bool = Form(False)):
    # Fields are optional so that the gate answers first (a 422 would reveal the endpoint)
    gate = _gate(request)
    if isinstance(gate, JSONResponse):
        return gate
    client_id, _ = gate
    if archive is None or not commit:
        return JSONResponse(status_code=400, content={"error": "archive and commit are required"})
    data = await archive.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        return JSONResponse(status_code=413, content={"error": f"archive larger than {MAX_UPLOAD} bytes"})
    label = updates.dev_version(data, commit, dirty)
    try:
        view_ = await updates.start_job(label, client_id, updates.install_upload, data, commit, dirty, client_id)
    except updates.UpdateError as e:
        return JSONResponse(status_code=409, content={"error": str(e)})
    return JSONResponse(status_code=202, content={"status": "started", "old_version": config.SERVICE_DIR.name,
                                                  "new_version": label, "job": view_})


@router.post("/dev/release")
async def dev_release(request: Request):
    """Back to a release: the newest one on disk, or install the newest of the channel."""
    gate = _gate(request)
    if isinstance(gate, JSONResponse):
        return gate
    client_id, _ = gate
    from . import admin
    lay = updates.layout()
    installed = updates.newest_installed_release(lay)
    if installed and installed != lay.read_pointer("current"):
        updates.switch_to(installed, "dev-release", client_id)
        asyncio.get_event_loop().call_later(1.5, admin._shutdown)
        return {"status": "switched", "version": installed, "restarting": True}
    try:
        release = await updates.find_release(None)
        view_ = await updates.start_update(release, client_id)
    except updates.UpdateError as e:
        return JSONResponse(status_code=409, content={"error": str(e)})
    return JSONResponse(status_code=202, content={"status": "started", "old_version": config.SERVICE_DIR.name,
                                                  "new_version": release["version"], "job": view_})


# ---------------------------------------------------------------------------
# Command line: font-render-server dev-mode on | off | status (local administrator)
# ---------------------------------------------------------------------------

def cmd_dev_mode(args) -> int:
    registry = auth.ClientRegistry(config.CLIENTS_FILE)
    auth.registry = registry  # expiry handling in active() uses the module registry
    if args.action == "status":
        print(json.dumps(view(), indent=2))
        return 0
    require_elevated("dev-mode")
    if args.action == "off":
        print("Developer mode switched off." if disable(registry, local_user()) else "Developer mode was off.")
        return 0
    if not args.client or not args.reason:
        raise SystemExit("dev-mode on needs --client and --reason")
    try:
        tokens = enable(registry, args.client, args.reason, args.days, args.allow_from or [], local_user())
    except ValueError as e:
        raise SystemExit(f"--allow-from: {e}")
    state = view()
    print(f"Developer mode on until {state['until'] or 'switched off'} for: {', '.join(args.client)}")
    for client_id, token in tokens.items():
        print(f"New client '{client_id}' (dev scope), token shown once:\n\n  {token}\n")
    if config.APP is None:
        print("Note: this is a checkout; /dev/* works only on an installed server.")
    return 0


def add_parser(sub) -> None:
    p = sub.add_parser("dev-mode", help="developer mode: on / off / status (local administrator)")
    p.add_argument("action", choices=("on", "off", "status"))
    p.add_argument("--client", action="append", help="client that may deploy (repeatable; created if missing)")
    p.add_argument("--reason", help="why (shown in the web admin and the audit log)")
    p.add_argument("--days", type=float, default=DEFAULT_DAYS, help=f"expiry (default {DEFAULT_DAYS}; 0 = never)")
    p.add_argument("--allow-from", action="append", metavar="CIDR", help="only from these networks (repeatable)")
    p.set_defaults(func=cmd_dev_mode)
