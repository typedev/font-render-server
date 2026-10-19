"""Install, repair and uninstall the Windows service in the installed layout.

    APP  (C:\\Program Files\\FontRender)   bin\\, python\\, versions\\<v>\\, browsers\\, current
    HOME (C:\\ProgramData\\FontRender)     .env, clients.json, data\\, logs\\  (docs/distribution.md)

install.ps1 unpacks a release into APP\\versions\\<v>, runs `uv sync` there and then
`<that venv>\\python.exe -m font_render install`, which does the rest. Every side effect
goes through a Runner, so `--dry-run` prints the plan and tests check it on any OS.
"""

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from . import auth, config
from .localadmin import audit_line, copy_to_clipboard, local_user, require_elevated

SERVICE = "FontRenderService"
DISPLAY_NAME = "Font Render Service"
DESCRIPTION = "Renders fonts with DirectWrite in headless Chromium over HTTP"
PUBLISHER = "Alexander Lubovenko"
PROJECT_URL = "https://github.com/typedev/font-render-server"
FIREWALL_RULE = "Font Render Service"
UNINSTALL_KEY = r"HKLM\Software\Microsoft\Windows\CurrentVersion\Uninstall\FontRenderService"
START_MENU_FOLDER = "Font Render"
# Well-known SIDs, so the ACL works on any Windows language
SID_SYSTEM = "*S-1-5-18"
SID_ADMINS = "*S-1-5-32-544"
# Files copied from the version's deploy/windows into APP\bin
BIN_FILES = ("launcher.py", "reset-admin.cmd", "uninstall.cmd", "font-render-server.cmd")
LOG_ROTATE_BYTES = 5 * 1024 * 1024
HEALTH_TIMEOUT_S = 180


@dataclass
class Layout:
    app: Path
    home: Path

    @property
    def bin(self) -> Path:
        return self.app / "bin"

    @property
    def versions(self) -> Path:
        return self.app / "versions"

    @property
    def browsers(self) -> Path:
        return self.app / "browsers"

    @property
    def nssm(self) -> Path:
        return self.bin / "nssm.exe"

    def venv_python(self, version: str) -> Path:
        return self.versions / version / ".venv" / "Scripts" / "python.exe"

    def read_pointer(self, name: str) -> str | None:
        """Contents of APP\\current / APP\\previous, or None."""
        try:
            return (self.app / name).read_text(encoding="utf-8").strip() or None
        except FileNotFoundError:
            return None


def default_home() -> Path:
    return Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "FontRender"


def start_menu_dir() -> Path:
    base = Path(os.environ.get("ProgramData", r"C:\ProgramData"))
    return base / "Microsoft" / "Windows" / "Start Menu" / "Programs" / START_MENU_FOLDER


def _decode(data: bytes) -> str:
    """Command output as text: NSSM writes UTF-16LE, other tools the console code page."""
    if data[1:2] == b"\0" or data[:2] == b"\xff\xfe":
        return data.decode("utf-16-le", errors="replace").lstrip("\ufeff")
    return data.decode(errors="replace")


class Runner:
    """Performs side effects; DryRunner prints them instead."""

    def run(self, cmd: list, *, check: bool = True, env: dict | None = None) -> subprocess.CompletedProcess:
        cmd = [str(c) for c in cmd]
        raw = subprocess.run(cmd, capture_output=True, stdin=subprocess.DEVNULL,
                             env={**os.environ, **env} if env else None)
        r = subprocess.CompletedProcess(cmd, raw.returncode, _decode(raw.stdout), _decode(raw.stderr))
        if check and r.returncode != 0:
            raise SystemExit(f"failed ({r.returncode}): {subprocess.list2cmdline(cmd)}\n"
                             f"{(r.stderr or r.stdout).strip()}")
        return r


    def mkdir(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)

    def write_text(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)

    def append_text(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(text)

    def copy(self, src: Path, dst: Path) -> None:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)

    def remove(self, path: Path) -> None:
        try:
            os.chmod(path, 0o600)
            path.unlink()
        except OSError:
            pass

    def rmtree(self, path: Path) -> None:
        def clear_readonly(func, p, _exc):  # uv and Chromium leave read-only files
            try:
                os.chmod(p, 0o700)
                func(p)
            except OSError:
                pass
        if path.exists():
            shutil.rmtree(path, onexc=clear_readonly)

    def wait_health(self, port: int) -> bool:
        deadline = time.monotonic() + HEALTH_TIMEOUT_S
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as r:
                    if r.status == 200:
                        return True
            except OSError:
                pass
            time.sleep(2)
        return False

    def say(self, text: str) -> None:
        print(text, flush=True)


@dataclass
class DryRunner(Runner):
    """Records every side effect in `ops` (and prints it) without doing it.

    `results` maps a command prefix (tuple of its first words) to a return code, so tests
    can simulate e.g. a missing service.
    """
    quiet: bool = False
    ops: list = field(default_factory=list)
    results: dict = field(default_factory=dict)

    def _op(self, *op):
        self.ops.append(op)
        if not self.quiet:
            print("  [dry-run]", *(subprocess.list2cmdline(o) if isinstance(o, list) else o for o in op))

    def run(self, cmd, *, check=True, env=None):
        cmd = [str(c) for c in cmd]
        self._op("run", cmd)
        code = next((rc for prefix, rc in self.results.items() if tuple(cmd[:len(prefix)]) == prefix), 0)
        return subprocess.CompletedProcess(cmd, code, "", "")


    def mkdir(self, path):
        self._op("mkdir", str(path))

    def write_text(self, path, text):
        self._op("write", str(path), text)

    def append_text(self, path, text):
        self._op("append", str(path), text)

    def copy(self, src, dst):
        self._op("copy", str(src), str(dst))

    def rmtree(self, path):
        self._op("rmtree", str(path))

    def remove(self, path):
        self._op("remove", str(path))

    def wait_health(self, port):
        self._op("wait-health", str(port))
        return True

    def say(self, text):
        if not self.quiet:
            print(text)

    def commands(self) -> list[list[str]]:
        return [op[1] for op in self.ops if op[0] == "run"]

    def written(self, path: Path) -> str | None:
        """Last text written to `path`, if any."""
        texts = [op[2] for op in self.ops if op[0] == "write" and op[1] == str(path)]
        return texts[-1] if texts else None


DEFAULT_PORT = 8000
PORT_SEARCH = 100  # 8000 busy → the first free one of 8001..8099


def port_owner(port: int, app: Path) -> dict | None:
    """Who listens on TCP `port`: {"pid", "name", "ours"} (ours = this service), or None."""
    import psutil
    try:
        conns = psutil.net_connections(kind="tcp")
    except (psutil.AccessDenied, OSError):
        return None
    for c in conns:
        if c.status != psutil.CONN_LISTEN or not c.laddr or c.laddr.port != port:
            continue
        name, ours = "?", False
        try:
            proc = psutil.Process(c.pid)
            name = proc.name()
            exe = proc.exe() or ""
            ours = exe.lower().startswith(str(app).lower()) or any(
                "font_render" in part or "launcher.py" in part for part in proc.cmdline())
        except (psutil.Error, OSError):
            pass
        return {"pid": c.pid, "name": name, "ours": ours}
    return None


def choose_port(requested: int | None, installed: int | None, app: Path, owner=port_owner,
                say=print) -> int:
    """The port to install on. An explicit port must be free (or ours); otherwise keep the
    installed one or 8000, moving on to the next free one if another program has it."""
    if requested:
        o = owner(requested, app)
        if o and not o["ours"]:
            raise SystemExit(f"port {requested} is in use by {o['name']} (pid {o['pid']}); "
                             f"choose another one with -Port")
        return requested
    start = installed or DEFAULT_PORT
    o = owner(start, app)
    if not o or o["ours"]:
        return start
    for port in range(start + 1, start + PORT_SEARCH):
        p = owner(port, app)
        if not p or p["ours"]:
            say(f"Port {start} is in use by {o['name']} (pid {o['pid']}): using port {port} instead")
            return port
    raise SystemExit(f"ports {start}-{start + PORT_SEARCH - 1} are all in use; choose one with -Port")


@dataclass
class InstallOptions:
    port: int | None = None  # None: keep the installed one, else 8000 (next free if busy)
    firewall: bool = True
    channel: str = "stable"
    start: bool = True
    import_from: Path | None = None  # a checkout whose data moves into a fresh HOME


# Data of a checkout (its root is its HOME) that a fresh installed HOME takes over
IMPORT_FILES = (".env", "clients.json", ".session_key", "data/settings.json", "data/stats.json",
                "data/firefox_prefs.json")


@dataclass
class InstallResult:
    version: str
    fresh: bool
    url: str
    token: str | None
    healthy: bool


def set_env_line(text: str, key: str, value: str) -> str:
    """Set KEY=value in .env text, keeping other lines and comments."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip().partition("=")[0].strip() == key and not line.lstrip().startswith("#"):
            lines[i] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")
    return "\n".join(lines) + "\n"


ENV_HEADER = """\
# Font Render Service configuration (docs/deployment.md#configuration).
# Restart the service after editing: Services > Font Render Service > Restart.
"""


def _has_admin(registry: auth.ClientRegistry) -> bool:
    return any(not c.get("disabled") and "admin" in c.get("scopes", [])
               for c in registry.clients().values())


def _dir_size_kb(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                pass
    return total // 1024


def _shortcut_ps(lnk: Path, target: Path, description: str) -> str:
    def q(p):  # PowerShell single-quoted string
        return "'" + str(p).replace("'", "''") + "'"
    return (f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut({q(lnk)}); "
            f"$s.TargetPath = {q(target)}; $s.WorkingDirectory = {q(target.parent)}; "
            f"$s.Description = {q(description)}; $s.Save()")


def _env_port(env_text: str) -> int | None:
    for line in env_text.splitlines():
        key, _, value = line.strip().partition("=")
        if key.strip() == "FONT_RENDER_PORT" and value.strip().isdigit():
            return int(value.strip())
    return None


def install(layout: Layout, source: Path, opts: InstallOptions, runner: Runner,
            launcher_python: Path, playwright_python: Path, port_owner_fn=port_owner) -> InstallResult:
    """Install or repair: idempotent, keeps data. `source` is APP\\versions\\<version>."""
    version = source.name
    home, app = layout.home, layout.app
    clients_file = home / "clients.json"
    fresh = not clients_file.exists()
    audit_file = home / "logs" / "audit.log"
    runner.say(f"Installing Font Render Service {version}" + ("" if fresh else " (repair, data kept)"))

    # 0. Port, before anything changes: an explicit one that is busy stops here
    try:
        installed_port = json.loads((home / "install.json").read_text(encoding="utf-8")).get("port")
    except (OSError, ValueError):
        installed_port = None
    env_source = (opts.import_from if opts.import_from else home) / ".env"
    if not installed_port and env_source.is_file():
        installed_port = _env_port(env_source.read_text(encoding="utf-8"))
    opts.port = choose_port(opts.port, installed_port, app, owner=port_owner_fn, say=runner.say)

    # 1. Data directory, administrators and SYSTEM only (token hashes, session key)
    for d in (home, home / "logs", home / "data", layout.bin):
        runner.mkdir(d)
    imported = []
    if opts.import_from is not None:
        if not fresh:
            raise SystemExit(f"--import-from: {home} already has clients.json; not overwriting it")
        for name in IMPORT_FILES:
            if (opts.import_from / name).is_file():
                runner.copy(opts.import_from / name, home / name)
                imported.append(name)
        if "clients.json" not in imported:
            raise SystemExit(f"--import-from: no clients.json in {opts.import_from}")
        fresh = False  # keep the imported clients and settings; no new admin token
        runner.say(f"Imported from {opts.import_from}: {', '.join(imported)}")
    # ACL on the folder only; its contents inherit it. (/T with /inheritance:r would strip
    # existing files of their inherited ACEs and leave them unreadable for everyone.)
    runner.run(["icacls", home, "/inheritance:r", "/grant:r",
                f"{SID_SYSTEM}:(OI)(CI)F", f"{SID_ADMINS}:(OI)(CI)F", "/Q"])
    runner.run(["icacls", home / "*", "/reset", "/T", "/C", "/Q"], check=False)  # repair: re-inherit
    runner.write_text(app / "home", str(home) + "\n")

    # 2. Install facts and configuration
    try:
        info = json.loads((home / "install.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        info = {}
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    info.update({"version": version, "port": opts.port, "channel": opts.channel,
                 "app": str(app), "home": str(home), "updated": now})
    info.setdefault("installed", now)
    runner.write_text(home / "install.json", json.dumps(info, indent=2, sort_keys=True) + "\n")
    try:
        env_text = (home / ".env").read_text(encoding="utf-8")
    except FileNotFoundError:
        env_text = ENV_HEADER
    if ".env" in imported:
        env_text = (opts.import_from / ".env").read_text(encoding="utf-8")
    runner.write_text(home / ".env", set_env_line(env_text, "FONT_RENDER_PORT", str(opts.port)))

    # 3. Program files: launcher and wrappers, NSSM, version pointer
    for name in BIN_FILES:
        runner.copy(source / "deploy" / "windows" / name, layout.bin / name)
    if (source / "bin" / "nssm.exe").is_file():
        runner.copy(source / "bin" / "nssm.exe", layout.nssm)
    elif not layout.nssm.is_file() and not isinstance(runner, DryRunner):
        raise SystemExit(f"nssm.exe missing: not in {source / 'bin'} nor {layout.bin}")
    current = layout.read_pointer("current")
    if current and current != version:
        runner.write_text(app / "previous", current + "\n")
    runner.write_text(app / "current", version + "\n")

    # 4. Browser for this version's Playwright, inside APP (not the SYSTEM profile)
    runner.say("Installing Chromium...")
    runner.run([playwright_python, "-m", "playwright", "install", "chromium"],
               env={"PLAYWRIGHT_BROWSERS_PATH": str(layout.browsers)})

    # 5. Service: a stable command; the launcher picks the version from APP\current
    nssm = layout.nssm
    if runner.run([nssm, "status", SERVICE], check=False).returncode == 0:
        runner.run([nssm, "stop", SERVICE], check=False)
        runner.run([nssm, "remove", SERVICE, "confirm"])
    log_file = home / "logs" / "service.log"
    # NSSM stores the parameters as one command line: quote the path (C:\Program Files)
    runner.run([nssm, "install", SERVICE, launcher_python, f'"{layout.bin / "launcher.py"}"'])
    for args in (
        ["AppDirectory", app],
        ["DisplayName", DISPLAY_NAME],
        ["Description", DESCRIPTION],
        ["Start", "SERVICE_AUTO_START"],
        ["AppEnvironmentExtra", f"FONT_RENDER_HOME={home}", f"FONT_RENDER_APP={app}",
         f"PLAYWRIGHT_BROWSERS_PATH={layout.browsers}", "PYTHONUNBUFFERED=1"],
        ["AppExit", "Default", "Restart"],
        ["AppRestartDelay", "5000"],
        ["AppStdout", log_file],
        ["AppStderr", log_file],
        ["AppStdoutCreationDisposition", "4"],
        ["AppStderrCreationDisposition", "4"],
        ["AppRotateFiles", "1"],
        ["AppRotateOnline", "1"],
        ["AppRotateBytes", str(LOG_ROTATE_BYTES)],
    ):
        runner.run([nssm, "set", SERVICE, *args])

    # 6. Firewall: this port, private networks, local subnet only (plain HTTP)
    runner.run(["netsh", "advfirewall", "firewall", "delete", "rule", f"name={FIREWALL_RULE}"], check=False)
    if opts.firewall:
        runner.run(["netsh", "advfirewall", "firewall", "add", "rule", f"name={FIREWALL_RULE}",
                    "dir=in", "action=allow", "protocol=TCP", f"localport={opts.port}",
                    "profile=private", "remoteip=localsubnet"])

    # 7. First admin: a fresh install closes anonymous rendering and the legacy token
    token = None
    # (imported: read the source, the copy has the same content and a dry run has no copy)
    registry = auth.ClientRegistry(opts.import_from / "clients.json" if imported else clients_file)
    if fresh or not _has_admin(registry):
        if fresh:
            registry.data["settings"].update(allow_anonymous_render=False, legacy_admin_token=False)
        client = registry.clients().get("admin") or {"created": now}
        client.update(disabled=False, scopes=sorted(set(client.get("scopes", [])) | {"render", "admin"}))
        token = auth.new_token(client)
        registry.data["clients"]["admin"] = client
        runner.write_text(clients_file, registry.dumps())
    runner.append_text(audit_file, audit_line("install", version=version, fresh=fresh,
                                              admin_created=token is not None, imported=imported,
                                              by=local_user()))

    # 8. Start menu
    menu = start_menu_dir()
    runner.mkdir(menu)
    runner.write_text(menu / "Font Render web admin.url",
                      f"[InternetShortcut]\nURL=http://localhost:{opts.port}/admin/\n")
    for name, target, desc in (
        ("Reset admin token.lnk", layout.bin / "reset-admin.cmd", "Issue a new token for the admin client"),
        ("Uninstall Font Render Service.lnk", layout.bin / "uninstall.cmd", "Remove the service"),
    ):
        runner.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                    _shortcut_ps(menu / name, target, desc)])

    # 9. Apps & features entry
    values = [
        ("DisplayName", "REG_SZ", DISPLAY_NAME),
        ("DisplayVersion", "REG_SZ", version),
        ("Publisher", "REG_SZ", PUBLISHER),
        ("URLInfoAbout", "REG_SZ", PROJECT_URL),
        ("InstallLocation", "REG_SZ", str(app)),
        ("DisplayIcon", "REG_SZ", str(launcher_python)),
        ("UninstallString", "REG_SZ", f'"{layout.bin / "uninstall.cmd"}"'),
        ("QuietUninstallString", "REG_SZ", f'"{layout.bin / "uninstall.cmd"}" --quiet --keep-data'),
        ("EstimatedSize", "REG_DWORD", str(_dir_size_kb(app))),
        ("NoModify", "REG_DWORD", "1"),
        ("NoRepair", "REG_DWORD", "1"),
    ]
    for name, typ, data in values:
        runner.run(["reg", "add", UNINSTALL_KEY, "/v", name, "/t", typ, "/d", data, "/f"])

    # 10. Start and wait for /health. `nssm start` fails with "Unexpected status
    # SERVICE_START_PENDING" when the app takes a few seconds to start, so /health decides.
    healthy = False
    if opts.start:
        runner.say("Starting the service...")
        r = runner.run([nssm, "start", SERVICE], check=False)
        if r.returncode != 0:
            runner.say(f"  nssm: {(r.stdout + r.stderr).strip()}")
        healthy = runner.wait_health(opts.port)
    return InstallResult(version=version, fresh=fresh, token=token, healthy=healthy,
                         url=f"http://{socket.gethostname()}:{opts.port}/admin/")


def refresh_after_switch(layout: Layout, source: Path, runner: Runner) -> None:
    """Before switching to the version in `source` (update, rollback): its launcher and
    wrappers into APP\\bin, its number into install.json and Apps & features."""
    for name in BIN_FILES:
        if (source / "deploy" / "windows" / name).is_file():
            runner.copy(source / "deploy" / "windows" / name, layout.bin / name)
    try:
        info = json.loads((layout.home / "install.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        info = {}
    info.update(version=source.name, updated=time.strftime("%Y-%m-%dT%H:%M:%S"))
    runner.write_text(layout.home / "install.json", json.dumps(info, indent=2, sort_keys=True) + "\n")
    runner.run(["reg", "add", UNINSTALL_KEY, "/v", "DisplayVersion", "/t", "REG_SZ", "/d", source.name, "/f"],
               check=False)


def uninstall(layout: Layout, purge: bool, runner: Runner) -> None:
    """Remove service, firewall rule, shortcuts, Apps & features entry and APP; HOME if purge.

    What this process runs on (APP\\python, the current venv) stays: bin\\uninstall.cmd
    removes it after the process has exited."""
    runner.say("Removing Font Render Service" + (" and its data" if purge else " (data kept)"))
    if layout.nssm.is_file() or isinstance(runner, DryRunner):
        runner.run([layout.nssm, "stop", SERVICE], check=False)
        runner.run([layout.nssm, "remove", SERVICE, "confirm"], check=False)
    else:
        runner.run(["sc", "stop", SERVICE], check=False)
        runner.run(["sc", "delete", SERVICE], check=False)
    runner.run(["netsh", "advfirewall", "firewall", "delete", "rule", f"name={FIREWALL_RULE}"], check=False)
    runner.rmtree(start_menu_dir())
    runner.run(["reg", "delete", UNINSTALL_KEY, "/f"], check=False)
    if purge:
        runner.rmtree(layout.home)
    else:
        runner.append_text(layout.home / "logs" / "audit.log", audit_line("uninstall", by=local_user()))
    # This interpreter runs from APP\python and the current version's venv: remove the rest now
    running = {layout.app / "python", layout.versions}
    if layout.app.is_dir():
        for child in layout.app.iterdir():
            if child not in running:
                runner.rmtree(child) if child.is_dir() else runner.remove(child)
        current = layout.read_pointer("current")
        if layout.versions.is_dir():
            for v in layout.versions.iterdir():
                if v.name != current:
                    runner.rmtree(v)
    # APP\python and the current venv are removed by uninstall.cmd once this process has exited


# ---------------------------------------------------------------------------
# Command line (font-render-server install | uninstall)
# ---------------------------------------------------------------------------

def _layout(args) -> Layout:
    app = Path(args.app) if getattr(args, "app", None) else config.APP
    if app is None and config.SERVICE_DIR.parent.name == "versions":
        app = config.SERVICE_DIR.parents[1]
    if app is None:
        raise SystemExit("not an installed version: pass --app (install.ps1 does this)")
    home = Path(args.home) if getattr(args, "home", None) else (
        config.HOME if config.APP is not None else default_home())
    return Layout(app=app, home=home)


def cmd_install(args) -> int:
    if not args.dry_run:
        require_elevated("install")
    layout = _layout(args)
    source = Path(args.source) if args.source else config.SERVICE_DIR
    if not args.dry_run and source.parent != layout.versions:
        raise SystemExit(f"run install from a version in {layout.versions} (install.ps1 does this)")
    runner = DryRunner() if args.dry_run else Runner()
    opts = InstallOptions(port=args.port, firewall=not args.no_firewall, channel=args.channel,
                          start=not args.no_start,
                          import_from=Path(args.import_from) if args.import_from else None)
    result = install(layout, source, opts, runner,
                     launcher_python=Path(sys.base_prefix) / "python.exe",
                     playwright_python=Path(sys.executable))
    print()
    if opts.start and not result.healthy:
        log_file = layout.home / "logs" / "service.log"
        print(f"! The service did not answer /health in {HEALTH_TIMEOUT_S} s; see {log_file}")
        try:
            print("".join(log_file.read_text(encoding="utf-8", errors="replace").splitlines(True)[-40:]))
        except OSError:
            print("  (no service log)")
    else:
        print(f"Font Render Service {result.version} installed" + (" and running" if opts.start else ""))
    print(f"  Web admin:   {result.url}")
    if result.token:
        copied = not args.dry_run and copy_to_clipboard(result.token)
        note = "   (dry run: not saved)" if args.dry_run else ("   (copied to the clipboard)" if copied else "")
        print(f"  Admin token: {result.token}{note}")
        print("               shown only once - keep it in a password manager")
    else:
        print("  Admin token: unchanged (existing clients kept)")
    print("  Lost the token? Start > Font Render > Reset admin token")
    print_addresses(opts.port)
    return 0 if (result.healthy or not opts.start) else 1


def print_addresses(port: int) -> None:
    """How clients reach this server: by name (survives DHCP) or by the current IP."""
    from .netinfo import addresses
    a = addresses(port)
    print()
    print("Clients connect with one of these (font-render login <address>):")
    for url in a["by_name"]:
        print(f"  {url}")
    for row in a["by_ip"]:
        print(f"  {row['url']:<28} ({row['interface']})")
    print("The computer name keeps working when DHCP gives a new IP; for a fixed IP reserve")
    print("one for this computer in the router (docs/deployment.md#finding-the-server).")


def cmd_uninstall(args) -> int:
    if not args.dry_run:
        require_elevated("uninstall")
    layout = _layout(args)
    purge = args.purge
    if not purge and not args.keep_data and not args.quiet:
        answer = input("Also delete data (client tokens, settings, statistics, logs)? [y/N] ")
        purge = answer.strip().lower() in ("y", "yes", "д", "да")
    uninstall(layout, purge, DryRunner() if args.dry_run else Runner())
    print("Font Render Service removed." + ("" if purge else f" Data kept in {layout.home}"))
    return 0


def add_parsers(sub) -> None:
    p = sub.add_parser("install", help="install or repair the Windows service (run by install.ps1)")
    p.add_argument("--app", help="program directory (default: the one this version is in)")
    p.add_argument("--home", help=r"data directory (default: %%ProgramData%%\FontRender)")
    p.add_argument("--source", help=argparse.SUPPRESS)  # tests and --dry-run from a checkout
    p.add_argument("--port", type=int, default=None,
                   help="HTTP port (default: the installed one, else 8000 or the next free one)")
    p.add_argument("--no-firewall", action="store_true", help="do not add the firewall rule")
    p.add_argument("--channel", choices=("stable", "beta"), default="stable")
    p.add_argument("--no-start", action="store_true", help="do not start the service")
    p.add_argument("--import-from", metavar="CHECKOUT",
                   help="move clients, settings, statistics and .env of a checkout install into the new data folder")
    p.add_argument("--dry-run", action="store_true", help="print what would be done")
    p.set_defaults(func=cmd_install)
    p = sub.add_parser("uninstall", help="remove the service and program files")
    p.add_argument("--app", help="program directory")
    p.add_argument("--home", help="data directory")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--purge", action="store_true", help="also delete data (tokens, settings, logs)")
    g.add_argument("--keep-data", action="store_true", help="keep data (default with --quiet)")
    p.add_argument("--quiet", action="store_true", help="no questions")
    p.add_argument("--dry-run", action="store_true", help="print what would be done")
    p.set_defaults(func=cmd_uninstall)
