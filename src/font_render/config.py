"""Paths, environment and limits of the render server.

Other modules read these as ``config.NAME`` at call time (not ``from config
import NAME``), so tests can monkeypatch them.
"""

import json
import logging
import os
import sys
from pathlib import Path

# Repository root: src/font_render/config.py -> parents[2]. In an installed layout
# (docs/distribution.md) this is APP\versions\<version>, so data must not live here.
SERVICE_DIR = Path(__file__).resolve().parents[2]
# Program directory of an installed layout (C:\Program Files\FontRender); None in a checkout.
# An installed version finds APP, and HOME through the APP\home pointer file, without
# environment variables, so console commands such as reset-admin work from any shell.
_installed = SERVICE_DIR.parents[1] if SERVICE_DIR.parent.name == "versions" else None
if _installed is not None and not (_installed / "home").is_file():
    _installed = None
_app = os.environ.get("FONT_RENDER_APP") or _installed
APP = Path(_app) if _app else None
# Data directory: clients, settings, stats, logs. FONT_RENDER_HOME in an installed layout
# (C:\ProgramData\FontRender); in a checkout, the repository root.
if os.environ.get("FONT_RENDER_HOME"):
    HOME = Path(os.environ["FONT_RENDER_HOME"])
elif _installed is not None:
    HOME = Path((_installed / "home").read_text(encoding="utf-8").strip())
else:
    HOME = SERVICE_DIR
LAST_GOOD_COMMIT = HOME / ".last_good_commit"

# Load .env file (KEY=VALUE lines) if it exists
_env_file = HOME / ".env"
if _env_file.is_file():
    for _line in _env_file.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

# Embedded mode (embedded.py): one desktop app's private server, set before this module loads
EMBEDDED = os.environ.get("FONT_RENDER_EMBEDDED") == "1"

HOST = os.environ.get("FONT_RENDER_HOST", "0.0.0.0")
# 41800: below the dynamic range (Windows/Hyper-V reserve blocks there), not a common dev port
PORT_DEFAULT = 41800
PORT = int(os.environ.get("FONT_RENDER_PORT", str(PORT_DEFAULT)))

# Legacy admin token (form field `token` on admin endpoints)
UPDATE_TOKEN = os.environ.get("FONT_RENDER_TOKEN", "")
CLIENTS_FILE = Path(os.environ.get("FONT_RENDER_CLIENTS", HOME / "clients.json"))
# Signing key of web admin session cookies (generated on first login, not in git)
SESSION_KEY_FILE = Path(os.environ.get("FONT_RENDER_SESSION_KEY", HOME / ".session_key"))
SESSION_TTL_S = float(os.environ.get("FONT_RENDER_SESSION_TTL_S", str(12 * 3600)))
# Service log written by NSSM, shown in the web admin
LOG_FILE = Path(os.environ.get("FONT_RENDER_LOG", HOME / "logs" / "service.log"))
# Security-relevant actions (reset-admin, updates, developer mode), JSON lines
AUDIT_LOG = Path(os.environ.get("FONT_RENDER_AUDIT_LOG", HOME / "logs" / "audit.log"))
# Install facts written by `font-render-server install` (absent in a checkout)
INSTALL_FILE = HOME / "install.json"
# Developer mode state, written only by `font-render-server dev-mode` (devmode.py)
DEVMODE_FILE = HOME / "devmode.json"
# Traffic per client, per-minute load and overload episodes (counts only, not in git)
STATS_FILE = Path(os.environ.get("FONT_RENDER_STATS", HOME / "data" / "stats.json"))
# Firefox prefs (firefox_user_prefs) set from the web admin; applied when Firefox launches
FIREFOX_PREFS_FILE = Path(os.environ.get("FONT_RENDER_FIREFOX_PREFS", HOME / "data" / "firefox_prefs.json"))
# Releases for updates of an installed server (docs/distribution.md#updates)
UPDATE_REPO = os.environ.get("FONT_RENDER_UPDATE_REPO", "typedev/font-render-server")
UPDATE_CHECK_HOURS = float(os.environ.get("UPDATE_CHECK_HOURS", "6"))
# A render that waited this long in the queue counts as an overload event
OVERLOAD_WAIT_S = float(os.environ.get("OVERLOAD_WAIT_S", "5"))

log = logging.getLogger("font_render")
if not log.handlers:
    # embedded: stdout carries only the handshake line, the parent need not drain it
    _handler = logging.StreamHandler(sys.stderr if EMBEDDED else sys.stdout)
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(_handler)
    log.setLevel(logging.INFO)
    log.propagate = False

SUPPORTED_BROWSERS = ("chromium", "firefox", "webkit")
MAX_FONTS = 20
MAX_FONT_SIZE = 10 * 1024 * 1024  # 10 MB
MAX_PPEM_VALUES = 100

POOL_SIZE = max(1, int(os.environ.get("RENDER_POOL_SIZE", "2")))  # browsers per key
CONCURRENCY = max(1, int(os.environ.get("RENDER_CONCURRENCY", "4")))  # renders in flight
QUEUE_MAX = max(0, int(os.environ.get("RENDER_QUEUE_MAX", "16")))  # waiting renders
TIMEOUT_S = float(os.environ.get("RENDER_TIMEOUT_S", "60"))
RECYCLE_AFTER = max(1, int(os.environ.get("RENDER_RECYCLE_AFTER", "500")))
WARM_KEYS = [("chromium", True), ("chromium", False)]
# Loaded pages kept per browser for reuse with the same fonts + scale (0 = off)
PAGE_CACHE = max(0, int(os.environ.get("RENDER_PAGE_CACHE", "4")))
PAGE_TTL_S = float(os.environ.get("RENDER_PAGE_TTL_S", "600"))
# Renders in flight + queued per client, so one client can't fill the queue
CLIENT_MAX = max(1, int(os.environ.get("RENDER_CLIENT_MAX", "4")))
# Uploaded-font cache (POST /fonts, font_ids): RAM only, per-client namespaces
FONT_CACHE_MAX_BYTES = int(os.environ.get("FONT_CACHE_MAX_BYTES", str(512 * 1024 * 1024)))
FONT_CACHE_TTL_S = float(os.environ.get("FONT_CACHE_TTL_S", "3600"))

# Bump when a change can alter rendered pixels (launch flags, page HTML/CSS,
# Chromium/Playwright upgrade). Clients key their render caches on it.
RENDER_VERSION = 2


# ---------------------------------------------------------------------------
# Settings editable in the web admin. Overrides live in data/settings.json and
# take precedence over the environment; "live" ones apply at once, the others
# after a restart.
# ---------------------------------------------------------------------------
SETTINGS_FILE = Path(os.environ.get("FONT_RENDER_SETTINGS", HOME / "data" / "settings.json"))

MB = 1024 * 1024
# name: (type, min, max, live, env var or None, description)
EDITABLE = {
    "POOL_SIZE": (int, 1, 8, True, "RENDER_POOL_SIZE",
                  "Browsers per (engine, ClearType). Extra browsers start on demand; "
                  "lowering takes effect as browsers are recycled"),
    "CONCURRENCY": (int, 1, 32, False, "RENDER_CONCURRENCY", "Renders in flight at once"),
    "QUEUE_MAX": (int, 0, 256, True, "RENDER_QUEUE_MAX", "Renders waiting for a slot; beyond that 429"),
    "CLIENT_MAX": (int, 1, 64, True, "RENDER_CLIENT_MAX",
                   "Renders in flight + waiting per client; beyond that 429 (hintbench keeps 4 in flight)"),
    "TIMEOUT_S": (float, 5, 600, True, "RENDER_TIMEOUT_S", "Per-render timeout → 504, browser retired"),
    "RECYCLE_AFTER": (int, 10, 100000, True, "RENDER_RECYCLE_AFTER", "Renders before a browser is replaced"),
    "PAGE_CACHE": (int, 0, 32, True, "RENDER_PAGE_CACHE", "Loaded pages kept per browser for reuse (0 = off)"),
    "PAGE_TTL_S": (float, 10, 86400, True, "RENDER_PAGE_TTL_S", "Idle cached pages are closed after this"),
    "FONT_CACHE_MAX_BYTES": (int, 16 * MB, 8192 * MB, True, "FONT_CACHE_MAX_BYTES",
                             "Font cache size for all clients (LRU)"),
    "FONT_CACHE_TTL_S": (float, 60, 7 * 86400, True, "FONT_CACHE_TTL_S", "Idle cached fonts are dropped after this"),
    "MAX_FONTS": (int, 1, 50, True, None, "Fonts per request"),
    "MAX_FONT_SIZE": (int, 1 * MB, 50 * MB, True, None, "Size of one font file"),
    "MAX_PPEM_VALUES": (int, 1, 500, True, None, "PPEM sizes per request"),
    "OVERLOAD_WAIT_S": (float, 0.5, 120, True, "OVERLOAD_WAIT_S", "Queue wait that counts as an overload event"),
    "SESSION_TTL_S": (float, 300, 30 * 86400, True, "FONT_RENDER_SESSION_TTL_S",
                      "Web admin session lifetime (new logins)"),
    "UPDATE_CHECK_HOURS": (float, 0, 168, True, "UPDATE_CHECK_HOURS",
                           "Installed server: look for new releases this often (0 = only when asked)"),
}
# Value from the environment / built-in default, before overrides
BASE_VALUES = {name: globals()[name] for name in EDITABLE}
OVERRIDES: dict[str, float | int] = {}
# Called after a live change: name -> fn(value)
APPLY_HOOKS: dict[str, list] = {}


def coerce_setting(name: str, raw) -> int | float:
    """Validate a new value; raises ValueError with a readable message."""
    typ, lo, hi, *_ = EDITABLE[name]
    try:
        value = typ(float(raw)) if typ is int else float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{name}: not a number") from None
    if typ is int and float(raw) != value:
        raise ValueError(f"{name}: must be a whole number")
    if not lo <= value <= hi:
        raise ValueError(f"{name}: must be between {lo} and {hi}")
    return value


def set_setting(name: str, value) -> None:
    """Set (or with value None, reset to BASE_VALUES) an override, persist it, apply it."""
    if value is None:
        OVERRIDES.pop(name, None)
        value = BASE_VALUES[name]
    else:
        value = OVERRIDES[name] = coerce_setting(name, value)
    _save_overrides()
    if EDITABLE[name][3]:
        globals()[name] = value
        for hook in APPLY_HOOKS.get(name, []):
            hook(value)


def _save_overrides():
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(OVERRIDES, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(SETTINGS_FILE)


def load_overrides():
    """Apply data/settings.json over the environment values (at import)."""
    OVERRIDES.clear()
    try:
        raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return
    except (OSError, ValueError) as e:
        log.error("settings file unreadable, using environment values: %s", e)
        return
    for name, value in raw.items():
        if name not in EDITABLE:
            continue
        try:
            OVERRIDES[name] = coerce_setting(name, value)
        except ValueError as e:
            log.error("settings file: %s (ignored)", e)
            continue
        globals()[name] = OVERRIDES[name]


def settings_view() -> list[dict]:
    """Each editable setting: running value, saved value, base value and where it comes from."""
    out = []
    for name, (typ, lo, hi, live, env, desc) in EDITABLE.items():
        saved = OVERRIDES.get(name, BASE_VALUES[name])
        out.append({
            "name": name, "env": env, "description": desc, "type": typ.__name__,
            "min": lo, "max": hi, "live": live,
            "value": globals()[name],  # what the server uses now
            "saved": saved,  # what it will use after a restart
            "base": BASE_VALUES[name],
            "base_source": "env" if env and env in os.environ else "default",
            "source": "admin" if name in OVERRIDES else ("env" if env and env in os.environ else "default"),
            "pending_restart": saved != globals()[name],
        })
    return out


load_overrides()
