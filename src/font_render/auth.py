"""Client auth: per-client Bearer tokens with scopes, stored as sha256 hashes in
clients.json (outside git, reloaded on change):

    {"settings": {"allow_anonymous_render": true, "legacy_admin_token": true},
     "clients": {"<id>": {"token_sha256": "...", "scopes": ["render"],
                          "created": "...", "disabled": false}}}
"""

import base64
import hashlib
import hmac
import json
import secrets
import time
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import urlsplit

from fastapi.responses import JSONResponse

from . import config
from .stats import stats

DEFAULT_SETTINGS = {"allow_anonymous_render": True, "legacy_admin_token": True}
# Scopes the admin API may grant; "dev" is only given by `font-render-server dev-mode on`
SCOPES = ("render", "admin")


class ClientRegistry:
    def __init__(self, path: Path):
        self.path = path
        self._stamp: tuple[int, int] | None = None
        self.data: dict = {"settings": dict(DEFAULT_SETTINGS), "clients": {}}

    def _reload(self):
        try:
            st = self.path.stat()
            stamp = (st.st_mtime_ns, st.st_size)
        except FileNotFoundError:
            stamp = None
        if stamp == self._stamp:
            return
        data = {"settings": dict(DEFAULT_SETTINGS), "clients": {}}
        if stamp is not None:
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                data["settings"].update(raw.get("settings", {}))
                data["clients"] = raw.get("clients", {})
            except (OSError, ValueError) as e:
                # Keep the last good state on a broken file
                config.log.error("clients file unreadable, keeping previous state: %s", e)
                return
        self.data = data
        self._stamp = stamp

    def settings(self) -> dict:
        self._reload()
        return self.data["settings"]

    def clients(self) -> dict:
        self._reload()
        return self.data["clients"]

    def lookup(self, token: str) -> tuple[str, dict] | None:
        """Find the enabled client for a Bearer token (constant-time compare)."""
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        found = None
        for client_id, c in self.clients().items():
            if hmac.compare_digest(digest, c.get("token_sha256", "")) and not c.get("disabled"):
                found = (client_id, c)
        return found

    def dumps(self) -> str:
        return json.dumps(self.data, indent=2, sort_keys=True)

    def save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(self.dumps(), encoding="utf-8")
        tmp.replace(self.path)
        st = self.path.stat()
        self._stamp = (st.st_mtime_ns, st.st_size)


registry = ClientRegistry(config.CLIENTS_FILE)


def new_token(client: dict) -> str:
    """Give a client a fresh token; returns it in clear (shown once), stores its hash."""
    token = secrets.token_urlsafe(32)
    client["token_sha256"] = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return token

# Paths open to everyone; admin paths decide in the endpoint (legacy form token)
PUBLIC_PATHS = {"/health"}
DEV_PATH_PREFIX = "/dev/"
ADMIN_PATH_PREFIXES = ("/update", "/rollback", "/browsers/install", "/admin/")


def is_admin_path(path: str) -> bool:
    return path == "/admin" or path.startswith(ADMIN_PATH_PREFIXES)


# ---------------------------------------------------------------------------
# Web admin sessions: a signed, stateless cookie (survives service restarts).
# The payload carries a fingerprint of the credential used to log in, so
# rotating/disabling that client or changing the legacy token ends the session.
# ---------------------------------------------------------------------------
SESSION_COOKIE = "frs_admin"
LEGACY_SUBJECT = "(legacy)"  # not a valid client id, so it can't collide
_session_keys: dict[Path, bytes] = {}


def _session_key() -> bytes:
    path = config.SESSION_KEY_FILE
    key = _session_keys.get(path)
    if key is None:
        try:
            key = path.read_bytes()
        except FileNotFoundError:
            key = b""
        if len(key) < 32:
            key = secrets.token_bytes(32)
            path.write_bytes(key)
        _session_keys[path] = key
    return key


def _fingerprint(subject: str) -> str | None:
    """Current fingerprint of an admin login subject, or None if it can't log in now."""
    if subject == LEGACY_SUBJECT:
        if not config.UPDATE_TOKEN or not registry.settings().get("legacy_admin_token", True):
            return None
        return hashlib.sha256(config.UPDATE_TOKEN.encode("utf-8")).hexdigest()[:16]
    c = registry.clients().get(subject)
    if not c or c.get("disabled") or "admin" not in c.get("scopes", []):
        return None
    return c.get("token_sha256", "")[:16] or None


def login_subject(token: str) -> str | None:
    """Who an admin token logs in as: an admin-scope client id or LEGACY_SUBJECT."""
    if not token:
        return None
    found = registry.lookup(token)
    if found and "admin" in found[1].get("scopes", []):
        return found[0]
    if (config.UPDATE_TOKEN and registry.settings().get("legacy_admin_token", True)
            and hmac.compare_digest(token.encode("utf-8"), config.UPDATE_TOKEN.encode("utf-8"))):
        return LEGACY_SUBJECT
    return None


def _sign(payload: bytes) -> str:
    return hmac.new(_session_key(), payload, hashlib.sha256).hexdigest()


def issue_session(subject: str) -> str:
    payload = json.dumps({"sub": subject, "exp": int(time.time() + config.SESSION_TTL_S),
                          "fp": _fingerprint(subject)}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=") + "." + _sign(payload)


def read_session(value: str | None) -> str | None:
    """The subject of a valid, unexpired session cookie whose credential is still current."""
    if not value or "." not in value:
        return None
    b64, sig = value.rsplit(".", 1)
    try:
        payload = base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4))
    except ValueError:
        return None
    if not hmac.compare_digest(sig, _sign(payload)):
        return None
    try:
        data = json.loads(payload)
        subject, exp, fp = data["sub"], data["exp"], data["fp"]
    except (ValueError, KeyError, TypeError):
        return None
    if exp < time.time() or fp is None or fp != _fingerprint(subject):
        return None
    return subject


def _cookie(headers: dict, name: str) -> str | None:
    raw = headers.get(b"cookie")
    if not raw:
        return None
    jar = SimpleCookie()
    try:
        jar.load(raw.decode("latin-1"))
    except Exception:
        return None
    morsel = jar.get(name)
    return morsel.value if morsel else None


def _same_origin(headers: dict) -> bool:
    """Origin (or Referer) of a cookie-authenticated POST must be this server (CSRF)."""
    host = headers.get(b"host", b"").decode("latin-1")
    source = (headers.get(b"origin") or headers.get(b"referer") or b"").decode("latin-1")
    return bool(host) and urlsplit(source).netloc == host


def _json_401_403(status: int) -> tuple[int, bytes]:
    return status, json.dumps({"error": "unauthorized" if status == 401 else "forbidden"}).encode()


class AuthMiddleware:
    """Resolve the Bearer client before the body is read; enforce render scope.

    Sets scope["state"]["client"] = (client_id, scopes) or None. Logs one access
    line per request with the client id (never font names or sizes).
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        t0 = time.perf_counter()
        path = scope["path"]
        state = scope.setdefault("state", {})
        headers = dict(scope.get("headers") or [])
        auth = headers.get(b"authorization", b"").decode("latin-1")
        client = None
        bad_token = False
        if auth[:7].lower() == "bearer ":
            found = registry.lookup(auth[7:].strip())
            if found:
                client = (found[0], tuple(found[1].get("scopes", [])))
            else:
                bad_token = True
        admin_path = is_admin_path(path)
        if client is None and not bad_token and admin_path:
            subject = read_session(_cookie(headers, SESSION_COOKIE))
            if subject:
                client = (subject, ("admin",))
                state["session"] = True
        state["client"] = client
        status_holder = {"status": 0}
        sizes = {"in": 0, "out": 0}  # body bytes, for per-client traffic

        async def receive_wrapper():
            message = await receive()
            if message["type"] == "http.request":
                sizes["in"] += len(message.get("body", b""))
            return message

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
            elif message["type"] == "http.response.body":
                sizes["out"] += len(message.get("body", b""))
            await send(message)

        denied = None
        if path.startswith(DEV_PATH_PREFIX):
            pass  # devmode decides: 404 while off, then 401 / 403
        elif path in PUBLIC_PATHS or admin_path:
            if bad_token and path not in PUBLIC_PATHS:
                denied = 401
            elif state.get("session") and scope["method"] not in ("GET", "HEAD") \
                    and not _same_origin(headers):
                denied = 403
        else:
            anonymous_ok = registry.settings().get("allow_anonymous_render", True)
            if client is None:
                # While migrating, an unknown token is treated as anonymous
                if not anonymous_ok:
                    denied = 401
            elif "render" not in client[1] and "admin" not in client[1]:
                denied = 403
        try:
            if denied:
                status, body = _json_401_403(denied)
                await send_wrapper({"type": "http.response.start", "status": status,
                                    "headers": [(b"content-type", b"application/json")]})
                await send_wrapper({"type": "http.response.body", "body": body})
            else:
                await self.app(scope, receive_wrapper, send_wrapper)
        finally:
            peer = scope.get("client") or ("-", 0)
            ms = (time.perf_counter() - t0) * 1000
            stats.record(peer[0], client[0] if client else None, scope["method"], path,
                         status_holder["status"], ms, sizes["in"], sizes["out"],
                         {"queue_s": state.get("queue_s", 0.0), "busy": state.get("busy")})
            # The web admin polls GET /admin/api/* — keep its successful reads out of the log
            if not (admin_path and scope["method"] in ("GET", "HEAD")
                    and status_holder["status"] < 400):
                config.log.info('%s %s "%s %s" %s %.0fms', peer[0], client[0] if client else "-",
                                scope["method"], path, status_holder["status"], ms)


def check_admin(state: dict, token: str) -> JSONResponse | None:
    """Admin endpoints: Bearer client with admin scope, or the legacy form token."""
    client = state.get("client")
    if client and "admin" in client[1]:
        return None
    legacy_err = None
    if token and registry.settings().get("legacy_admin_token", True):
        legacy_err = check_token(token)
        if legacy_err is None:
            return None
    if client:
        return JSONResponse(status_code=403, content={"error": "forbidden"})
    return legacy_err or JSONResponse(status_code=401, content={"error": "unauthorized"})


def check_token(token: str) -> JSONResponse | None:
    """Validate the update token. Returns error response or None if OK."""
    if not config.UPDATE_TOKEN:
        return JSONResponse(
            status_code=403,
            content={"error": "UPDATE_TOKEN not configured on server"},
        )
    if not hmac.compare_digest(token.encode("utf-8"), config.UPDATE_TOKEN.encode("utf-8")):
        return JSONResponse(
            status_code=403,
            content={"error": "Invalid token"},
        )
    return None
