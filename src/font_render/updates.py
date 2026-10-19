"""Release updates of an installed server (docs/distribution.md#updates).

Check: GitHub Releases of config.UPDATE_REPO; stable = newest non-prerelease, beta =
newest including pre-releases. Update (a background job): download the release zip,
SHA256SUMS and SHA256SUMS.sig → verify the signature (release_key.TRUSTED_KEYS), then the
zip's hash → unpack into APP\\versions\\<v> → uv sync → Chromium → selfcheck → refresh
APP\\bin and the Apps & features entry → previous = current, current = <v>, trial → exit;
NSSM restarts and the launcher starts the new version. The server deletes APP\\trial once
the new version is healthy (browser warm-up done); after three failed starts the launcher
reverts to previous on its own.

Network and process calls go through _http_get / _run so tests can replace them.
"""

import asyncio
import base64
import hashlib
import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from packaging.version import InvalidVersion, Version

from . import config, release_key
from .install import Layout, Runner, refresh_after_switch
from .localadmin import audit

PYTHON_VERSION = "3.12"  # as in install.ps1
KEEP_VERSIONS = 3  # current, previous and one more
CHECK_CACHE_S = 600  # a manual check within this time reuses the last answer
STEP_TIMEOUT_S = 900


class UpdateError(Exception):
    pass


# ---------------------------------------------------------------------------
# Releases
# ---------------------------------------------------------------------------

def _http_get(url: str, headers: dict | None = None, timeout: float = 60) -> tuple[int, dict, bytes]:
    """GET → (status, headers, body); 304 is returned, not raised."""
    req = urllib.request.Request(url, headers={"User-Agent": "font-render-server", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return 304, dict(e.headers), b""
        raise UpdateError(f"{url}: HTTP {e.code}") from None
    except (urllib.error.URLError, OSError) as e:
        raise UpdateError(f"{url}: {e}") from None


def parse_version(v: str) -> Version | None:
    try:
        return Version(v)
    except InvalidVersion:
        return None


def channel() -> str:
    try:
        return json.loads(config.INSTALL_FILE.read_text(encoding="utf-8")).get("channel", "stable")
    except (OSError, ValueError):
        return "stable"


def set_channel(value: str) -> None:
    if value not in ("stable", "beta"):
        raise ValueError("channel must be stable or beta")
    try:
        info = json.loads(config.INSTALL_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        info = {}
    info["channel"] = value
    config.INSTALL_FILE.write_text(json.dumps(info, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _release_view(r: dict) -> dict:
    assets = {a["name"]: a["browser_download_url"] for a in r.get("assets", [])}
    return {"version": r["tag_name"].removeprefix("v"), "prerelease": bool(r.get("prerelease")),
            "published": r.get("published_at"), "notes": r.get("body") or "",
            "url": r.get("html_url"), "assets": assets}


_cache: dict = {"etag": None, "releases": [], "checked": 0.0, "error": None}


async def fetch_releases(force: bool = False) -> list[dict]:
    """Published releases (newest first), cached; a failed check keeps the last good list."""
    if not force and time.time() - _cache["checked"] < CHECK_CACHE_S:
        return _cache["releases"]
    url = f"https://api.github.com/repos/{config.UPDATE_REPO}/releases?per_page=30"
    headers = {"Accept": "application/vnd.github+json"}
    if _cache["etag"]:
        headers["If-None-Match"] = _cache["etag"]
    try:
        status, resp_headers, body = await asyncio.to_thread(_http_get, url, headers, 30)
    except UpdateError as e:
        _cache.update(checked=time.time(), error=str(e))
        raise
    if status != 304:
        rows = [r for r in json.loads(body) if not r.get("draft")]
        _cache["releases"] = [_release_view(r) for r in rows if parse_version(r["tag_name"].removeprefix("v"))]
        _cache["etag"] = resp_headers.get("ETag") or resp_headers.get("Etag")
    _cache.update(checked=time.time(), error=None)
    return _cache["releases"]


def newer_releases(releases: list[dict], current: str, chan: str) -> list[dict]:
    """Releases of the channel newer than `current`, newest first."""
    cur = parse_version(current)
    rows = [r for r in releases if chan == "beta" or not r["prerelease"]]
    rows = [r for r in rows if cur is None or parse_version(r["version"]) > cur]
    return sorted(rows, key=lambda r: parse_version(r["version"]), reverse=True)


# ---------------------------------------------------------------------------
# Signature
# ---------------------------------------------------------------------------

def key_id(public_raw: bytes) -> str:
    return hashlib.sha256(public_raw).hexdigest()[:16]


def verify_signature(sums: bytes, sig_file: bytes, trusted: list[str] | None = None) -> str:
    """Raise UpdateError unless SHA256SUMS.sig signs `sums` with a trusted key; returns the key id."""
    keys = {}
    for k in (release_key.TRUSTED_KEYS if trusted is None else trusted):
        raw = base64.b64decode(k)
        keys[key_id(raw)] = raw
    try:
        sig = json.loads(sig_file)
        public = keys[sig["key_id"]]
        Ed25519PublicKey.from_public_bytes(public).verify(base64.b64decode(sig["signature"]), sums)
    except KeyError:
        raise UpdateError("release signed by an unknown key") from None
    except (ValueError, TypeError, InvalidSignature):
        raise UpdateError("bad release signature") from None
    return sig["key_id"]


def expected_hash(sums: bytes, name: str) -> str:
    for line in sums.decode("ascii", errors="replace").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == name:
            return parts[0].lower()
    raise UpdateError(f"{name} is not listed in SHA256SUMS")


# ---------------------------------------------------------------------------
# Job
# ---------------------------------------------------------------------------

@dataclass
class Job:
    state: str = "idle"  # idle | running | failed | restarting
    version: str | None = None
    step: str = ""
    error: str | None = None
    started: float | None = None
    log: list = field(default_factory=list)
    task: asyncio.Task | None = None

    def view(self) -> dict:
        return {"state": self.state, "version": self.version, "step": self.step,
                "error": self.error, "started": self.started, "log": self.log[-30:]}


job = Job()


def _say(text: str) -> None:
    job.step = text
    job.log.append(f"{time.strftime('%H:%M:%S')} {text}")
    config.log.info("update: %s", text)


def _uv_env(layout: Layout) -> dict:
    return {"UV_PYTHON_INSTALL_DIR": str(layout.app / "python"),
            "UV_CACHE_DIR": str(layout.app / "cache" / "uv"),
            "UV_PYTHON_PREFERENCE": "only-managed", "UV_NO_CONFIG": "1",
            "PLAYWRIGHT_BROWSERS_PATH": str(layout.browsers)}


def _run(cmd: list, cwd: Path, env: dict) -> None:
    r = subprocess.run([str(c) for c in cmd], cwd=cwd, env={**os.environ, **env},
                       capture_output=True, text=True, stdin=subprocess.DEVNULL,
                       timeout=STEP_TIMEOUT_S, errors="replace")
    if r.returncode != 0:
        tail = (r.stderr or r.stdout).strip().splitlines()[-5:]
        raise UpdateError(f"{Path(str(cmd[0])).name} {cmd[1] if len(cmd) > 1 else ''} failed: "
                          + " / ".join(tail))


def layout() -> Layout:
    if config.APP is None:
        raise UpdateError("not an installed server (checkout: updates come from git)")
    return Layout(app=config.APP, home=config.HOME)


def _write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text + "\n", encoding="utf-8")
    tmp.replace(path)


def prune(lay: Layout) -> list[str]:
    """Keep current, previous and the newest other release; drop leftovers of failed runs."""
    keep = {lay.read_pointer("current"), lay.read_pointer("previous")}
    dirs = [d for d in lay.versions.iterdir() if d.is_dir()] if lay.versions.is_dir() else []
    removed = []
    for d in dirs:
        if d.name.endswith((".partial", ".old")):
            shutil.rmtree(d, ignore_errors=True)
            removed.append(d.name)
    for d in dirs:  # developer builds are kept only while current or previous
        if d.exists() and d.name not in keep and parse_version(d.name) is None:
            shutil.rmtree(d, ignore_errors=True)
            removed.append(d.name)
    rest = sorted((d for d in dirs if d.name not in keep and d.exists() and parse_version(d.name)),
                  key=lambda d: parse_version(d.name), reverse=True)
    for d in rest[max(0, KEEP_VERSIONS - len(keep - {None})):]:
        shutil.rmtree(d, ignore_errors=True)
        removed.append(d.name)
    return removed


FONT_EXTENSIONS = (".ttf", ".otf", ".ttc", ".otc", ".woff", ".woff2", ".dfont", ".pfb", ".pfa", ".eot")


def _install_tree(lay: Layout, target: Path, zip_path: Path) -> None:
    """Unpack `zip_path` into `target`, uv sync, Chromium, selfcheck; `target` is removed on failure."""
    try:
        _say("unpacking")
        shutil.rmtree(target, ignore_errors=True)
        with zipfile.ZipFile(zip_path) as z:
            # Fonts are unreleased: never unpack one onto the server's disk (and never name it)
            fonts = [n for n in z.namelist() if n.lower().endswith(FONT_EXTENSIONS)]
            if fonts:
                raise UpdateError(f"the archive contains {len(fonts)} font file(s); remove them")
            z.extractall(target)
        env = _uv_env(lay)
        _say("installing dependencies (uv sync)")
        _run([lay.bin / "uv.exe", "sync", "--frozen", "--no-dev", "--python", PYTHON_VERSION, "--quiet"],
             target, env)
        python = lay.venv_python(target.name)
        _say("installing Chromium")
        _run([python, "-m", "playwright", "install", "chromium"], target, env)
        _say("self-check")
        _run([python, "-m", "font_render", "selfcheck"], target, env)
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)
        raise


def _switch(lay: Layout, version: str, *, trial: bool) -> str | None:
    """Make `version` current (previous = the old current); returns the old current."""
    _say("switching")
    current = lay.read_pointer("current")
    refresh_after_switch(lay, lay.versions / version, Runner())
    if current and current != version:
        _write(lay.app / "previous", current)
    _write(lay.app / "current", version)
    if trial:
        _write(lay.app / "trial", "0")
    else:
        (lay.app / "trial").unlink(missing_ok=True)
    return current


def _staging(lay: Layout) -> Path:
    staging = lay.home / "staging"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    return staging


def install_release(release: dict, by: str) -> None:
    """The whole update, blocking (run in a thread). Raises UpdateError; cleans up after itself."""
    lay = layout()
    version = release["version"]
    if version == lay.read_pointer("current"):
        raise UpdateError(f"{version} is already running")
    zip_name = f"font-render-{version}.zip"
    for name in (zip_name, "SHA256SUMS", "SHA256SUMS.sig"):
        if name not in release["assets"]:
            raise UpdateError(f"release {version} has no {name}")
    staging = _staging(lay)
    try:
        _say(f"downloading {version}")
        sums = _http_get(release["assets"]["SHA256SUMS"])[2]
        sig = _http_get(release["assets"]["SHA256SUMS.sig"])[2]
        kid = verify_signature(sums, sig)
        _say(f"signature ok (key {kid})")
        data = _http_get(release["assets"][zip_name], timeout=300)[2]
        if hashlib.sha256(data).hexdigest() != expected_hash(sums, zip_name):
            raise UpdateError(f"{zip_name} does not match SHA256SUMS")
        (staging / zip_name).write_bytes(data)
        _install_tree(lay, lay.versions / version, staging / zip_name)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    previous = _switch(lay, version, trial=True)
    removed = prune(lay)
    audit("update", version=version, previous=previous, by=by, pruned=removed)


def dev_version(data: bytes, commit: str, dirty: bool) -> str:
    """Version directory of an uploaded tree: dev-<commit[:7]>[-dirty-<sha256[:7]>]."""
    short = "".join(c for c in commit.lower() if c in "0123456789abcdef")[:7] or "unknown"
    return f"dev-{short}" + (f"-dirty-{hashlib.sha256(data).hexdigest()[:7]}" if dirty else "")


def install_upload(data: bytes, commit: str, dirty: bool, by: str) -> str:
    """Developer mode: install an uploaded tree (no signature: dev mode is the permission)."""
    lay = layout()
    version = dev_version(data, commit, dirty)
    commit = version.split("-")[1]
    if version == lay.read_pointer("current"):
        raise UpdateError(f"{version} is already running")
    staging = _staging(lay)
    try:
        (staging / "upload.zip").write_bytes(data)
        _install_tree(lay, lay.versions / version, staging / "upload.zip")
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    previous = _switch(lay, version, trial=True)
    removed = prune(lay)
    audit("dev-deploy", version=version, commit=commit, dirty=dirty, previous=previous, by=by,
          size=len(data), pruned=removed)
    return version


async def start_job(version: str, by: str, fn, *args) -> dict:
    """Run fn(*args) in a thread as the update job; it ends by exiting the process."""
    if job.state in ("running", "restarting"):
        raise UpdateError(f"an update to {job.version} is already {job.state}")
    job.__init__(state="running", version=version, started=time.time())
    _say(f"{version} requested by {by}")

    async def run():
        from . import admin
        try:
            await asyncio.to_thread(fn, *args)
        except Exception as e:  # noqa: BLE001 — report every failure in the admin
            job.state, job.error = "failed", str(e)
            _say(f"failed: {e}")
            audit("update-failed", version=version, error=str(e), by=by)
            return
        job.state = "restarting"
        _say("restarting into the new version")
        asyncio.get_event_loop().call_later(1.5, admin._shutdown)

    job.task = asyncio.create_task(run())
    return job.view()


async def start_update(release: dict, by: str) -> dict:
    return await start_job(release["version"], by, install_release, release, by)


def switch_to(version: str, event: str, by: str) -> str:
    """Switch to an installed version (no download); returns it."""
    lay = layout()
    if not version or not (lay.versions / version).is_dir():
        raise UpdateError(f"{version or 'that version'} is not installed")
    previous = _switch(lay, version, trial=False)
    audit(event, version=version, previous=previous, by=by)
    return version


def rollback(by: str) -> str:
    """current <-> previous (no download); returns the version switched to."""
    previous = layout().read_pointer("previous")
    if not previous:
        raise UpdateError("no previous version to roll back to")
    try:
        return switch_to(previous, "rollback", by)
    except UpdateError:
        raise UpdateError("no previous version to roll back to") from None


def newest_installed_release(lay: Layout) -> str | None:
    rows = [d.name for d in lay.versions.iterdir() if d.is_dir() and parse_version(d.name)] \
        if lay.versions.is_dir() else []
    return max(rows, key=parse_version) if rows else None


async def mark_healthy(warm_up: asyncio.Task) -> None:
    """After a switch: once the browsers are warm, the new version counts as good."""
    if config.APP is None or not (config.APP / "trial").exists():
        return
    try:
        await warm_up
    except Exception as e:  # noqa: BLE001
        config.log.error("warm-up failed, %s stays on trial: %s", config.SERVICE_DIR.name, e)
        return
    (config.APP / "trial").unlink(missing_ok=True)
    audit("update-healthy", version=config.SERVICE_DIR.name)


async def checker() -> None:
    """Background: refresh the release list every UPDATE_CHECK_HOURS (0 = only on demand)."""
    if config.APP is None:
        return
    await asyncio.sleep(60)
    while True:
        hours = config.UPDATE_CHECK_HOURS
        if hours > 0:
            try:
                rows = newer_releases(await fetch_releases(force=True), config.SERVICE_DIR.name, channel())
                if rows:
                    config.log.info("update available: %s", rows[0]["version"])
            except UpdateError as e:
                config.log.warning("update check failed: %s", e)
        await asyncio.sleep(max(hours, 0.25) * 3600)


async def check(force: bool = False) -> dict:
    """What the admin shows: running version, channel, newer releases, the job."""
    current = config.SERVICE_DIR.name
    chan = channel()
    out = {"mode": "release", "current": current, "channel": chan, "repo": config.UPDATE_REPO,
           "previous": layout().read_pointer("previous"), "job": job.view()}
    try:
        rows = newer_releases(await fetch_releases(force), current, chan)
        out.update(available=[{k: r[k] for k in ("version", "prerelease", "published", "notes", "url")}
                              for r in rows], error=None)
    except UpdateError as e:
        out.update(available=[], error=str(e))
    out["checked"] = _cache["checked"]
    return out


async def find_release(version: str | None) -> dict:
    """The release to install: `version`, or the newest of the channel. UpdateError if none."""
    releases = await fetch_releases(force=True)
    if version:
        for r in releases:
            if r["version"] == version:
                cur = parse_version(config.SERVICE_DIR.name)
                if cur is not None and parse_version(version) <= cur:
                    raise UpdateError(f"{version} is not newer than {cur} (use rollback)")
                return r
        raise UpdateError(f"no release {version}")
    rows = newer_releases(releases, config.SERVICE_DIR.name, channel())
    if not rows:
        raise UpdateError("up-to-date")
    return rows[0]
