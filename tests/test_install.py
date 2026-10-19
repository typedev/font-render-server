"""
Tests for the installed layout: FONT_RENDER_HOME, install/repair/uninstall plans,
reset-admin, the service launcher (docs/distribution.md).

    uv run pytest tests/test_install.py -v
"""

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from font_render import __main__ as cli, auth, config, install, localadmin

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def _config_paths(cwd: Path, env: dict, config_py: Path) -> dict:
    code = ("import json, importlib.util as u; s = u.spec_from_file_location('c', r'%s'); "
            "c = u.module_from_spec(s); s.loader.exec_module(c); "
            "print(json.dumps({k: str(getattr(c, k)) for k in "
            "('HOME', 'APP', 'CLIENTS_FILE', 'LOG_FILE', 'STATS_FILE', 'SETTINGS_FILE', "
            "'SESSION_KEY_FILE', 'AUDIT_LOG', 'PORT')}))") % config_py
    clean = {k: v for k, v in os.environ.items() if not k.startswith("FONT_RENDER_")}
    out = subprocess.run([sys.executable, "-c", code], cwd=cwd, env={**clean, **env},
                         capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def test_checkout_paths_unchanged(tmp_path):
    p = _config_paths(tmp_path, {}, ROOT / "src" / "font_render" / "config.py")
    assert p["HOME"] == str(ROOT) and p["APP"] == "None"
    assert p["CLIENTS_FILE"] == str(ROOT / "clients.json")
    assert p["LOG_FILE"] == str(ROOT / "logs" / "service.log")
    assert p["STATS_FILE"] == str(ROOT / "data" / "stats.json")


def test_home_from_environment(tmp_path):
    (tmp_path / ".env").write_text("FONT_RENDER_PORT=8123\n")
    p = _config_paths(tmp_path, {"FONT_RENDER_HOME": str(tmp_path)},
                      ROOT / "src" / "font_render" / "config.py")
    assert p["HOME"] == str(tmp_path)
    assert p["CLIENTS_FILE"] == str(tmp_path / "clients.json")
    assert p["SETTINGS_FILE"] == str(tmp_path / "data" / "settings.json")
    assert p["SESSION_KEY_FILE"] == str(tmp_path / ".session_key")
    assert p["AUDIT_LOG"] == str(tmp_path / "logs" / "audit.log")
    assert p["PORT"] == "8123"  # .env is read from HOME


def test_installed_version_finds_home_without_environment(tmp_path):
    app, home = tmp_path / "app", tmp_path / "data"
    pkg = app / "versions" / "1.2.0" / "src" / "font_render"
    pkg.mkdir(parents=True)
    shutil.copy(ROOT / "src" / "font_render" / "config.py", pkg / "config.py")
    (app / "home").write_text(str(home) + "\n")
    p = _config_paths(tmp_path, {}, pkg / "config.py")
    assert p["APP"] == str(app) and p["HOME"] == str(home)
    assert p["CLIENTS_FILE"] == str(home / "clients.json")


# ---------------------------------------------------------------------------
# install / repair / uninstall
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _program_data(tmp_path, monkeypatch):
    """Start menu shortcuts go under %ProgramData%: keep them in the temp dir."""
    monkeypatch.setenv("ProgramData", str(tmp_path / "ProgramData"))


@pytest.fixture
def layout(tmp_path):
    lay = install.Layout(app=tmp_path / "app", home=tmp_path / "home")
    lay.versions.mkdir(parents=True)
    return lay


@pytest.fixture
def source(layout):
    src = layout.versions / "1.2.0"
    (src / "deploy" / "windows").mkdir(parents=True)
    for name in install.BIN_FILES:
        (src / "deploy" / "windows" / name).write_text("x")
    (src / "bin").mkdir()
    (src / "bin" / "nssm.exe").write_text("x")
    return src


def _install(layout, source, runner=None, **opts):
    runner = runner or install.DryRunner(quiet=True)
    result = install.install(layout, source, install.InstallOptions(**opts), runner,
                             launcher_python=Path("py.exe"), playwright_python=Path("venv-py.exe"))
    return result, runner


def _apply_writes(runner):
    """Make a dry run's writes real, to set up the next scenario."""
    for op in runner.ops:
        if op[0] == "write":
            Path(op[1]).parent.mkdir(parents=True, exist_ok=True)
            Path(op[1]).write_text(op[2])


def _has(runner, *words) -> bool:
    return any(tuple(cmd[:len(words)]) == words or all(w in cmd for w in words) for cmd in runner.commands())


def test_fresh_install_plan(layout, source):
    result, r = _install(layout, source, port=8080)
    assert result.fresh and result.version == "1.2.0" and result.healthy
    # first admin, anonymous rendering and the legacy token closed
    data = json.loads(r.written(layout.home / "clients.json"))
    assert data["settings"] == {"allow_anonymous_render": False, "legacy_admin_token": False}
    admin = data["clients"]["admin"]
    assert admin["token_sha256"] == hashlib.sha256(result.token.encode()).hexdigest()
    assert set(admin["scopes"]) == {"render", "admin"} and not admin["disabled"]
    # pointers and configuration
    assert r.written(layout.app / "current") == "1.2.0\n"
    assert r.written(layout.app / "home") == f"{layout.home}\n"
    assert "FONT_RENDER_PORT=8080" in r.written(layout.home / ".env")
    assert json.loads(r.written(layout.home / "install.json"))["version"] == "1.2.0"
    # data dir ACL before any secret is written
    ops = [op[0] + ":" + (op[1][0] if op[0] == "run" else op[1]) for op in r.ops]
    assert ops.index("run:icacls") < ops.index(f"write:{layout.home / 'clients.json'}")
    acl = [c for c in r.commands() if c[0] == "icacls"]
    assert "/T" not in acl[0] and acl[1][1:3] == [str(layout.home / "*"), "/reset"]
    # service runs the stable launcher with HOME/APP/browsers in its environment
    nssm = str(layout.nssm)
    assert [nssm, "install", install.SERVICE, "py.exe", f'"{layout.bin / "launcher.py"}"'] in r.commands()
    env = next(c for c in r.commands() if c[:4] == [nssm, "set", install.SERVICE, "AppEnvironmentExtra"])
    assert f"FONT_RENDER_HOME={layout.home}" in env and f"PLAYWRIGHT_BROWSERS_PATH={layout.browsers}" in env
    assert ["venv-py.exe", "-m", "playwright", "install", "chromium"] in r.commands()
    assert _has(r, "netsh", "add", "localport=8080", "remoteip=localsubnet")
    assert _has(r, "reg", "add", "Publisher", "Alexander Lubovenko")
    assert [nssm, "start", install.SERVICE] in r.commands()
    assert any(op[0] == "append" and '"event": "install"' in op[2] for op in r.ops)


def test_no_firewall_and_no_start(layout, source):
    result, r = _install(layout, source, firewall=False, start=False)
    assert not _has(r, "netsh", "add")
    assert _has(r, "netsh", "delete")  # a rule from an earlier install is removed
    assert not any(c[1:2] == ["start"] for c in r.commands())
    assert not result.healthy


def test_existing_service_is_replaced(layout, source):
    r = install.DryRunner(quiet=True, results={(str(layout.nssm), "status"): 3})
    _install(layout, source, r)
    assert not any(c[1:2] == ["remove"] for c in r.commands())  # status != 0: no service yet
    _, r = _install(layout, source)
    assert [str(layout.nssm), "remove", install.SERVICE, "confirm"] in r.commands()


def test_repair_keeps_clients(layout, source):
    _, first = _install(layout, source)
    _apply_writes(first)
    before = (layout.home / "clients.json").read_text()
    result, r = _install(layout, source)
    assert not result.fresh and result.token is None
    assert r.written(layout.home / "clients.json") is None
    assert (layout.home / "clients.json").read_text() == before


def test_repair_without_admin_creates_one_keeps_settings(layout, source):
    layout.home.mkdir(parents=True)
    (layout.home / "clients.json").write_text(json.dumps({
        "settings": {"allow_anonymous_render": True},
        "clients": {"bot": {"token_sha256": "x", "scopes": ["render"]}}}))
    result, r = _install(layout, source)
    assert result.token
    data = json.loads(r.written(layout.home / "clients.json"))
    assert data["settings"]["allow_anonymous_render"] is True  # existing choice kept
    assert set(data["clients"]) == {"bot", "admin"}


def test_repair_keeps_env_lines_and_sets_previous(layout, source):
    layout.home.mkdir(parents=True)
    (layout.home / ".env").write_text("# mine\nRENDER_POOL_SIZE=3\nFONT_RENDER_PORT=8000\n")
    (layout.app / "current").write_text("1.1.0\n")
    _, r = _install(layout, source, port=9000)
    env = r.written(layout.home / ".env")
    assert env == "# mine\nRENDER_POOL_SIZE=3\nFONT_RENDER_PORT=9000\n"
    assert r.written(layout.app / "previous") == "1.1.0\n"
    assert r.written(layout.app / "current") == "1.2.0\n"


def test_import_from_checkout(layout, source, tmp_path):
    old = tmp_path / "checkout"
    (old / "data").mkdir(parents=True)
    (old / "clients.json").write_text(json.dumps({
        "settings": {"allow_anonymous_render": False, "legacy_admin_token": False},
        "clients": {"admin": {"token_sha256": "x", "scopes": ["render", "admin"]}}}))
    (old / ".env").write_text("FONT_RENDER_TOKEN=old\nRENDER_POOL_SIZE=3\n")
    (old / ".session_key").write_bytes(b"k" * 32)
    (old / "data" / "stats.json").write_text("{}")
    result, r = _install(layout, source, import_from=old)
    copies = {op[1] for op in r.ops if op[0] == "copy"}
    for name in (".env", "clients.json", ".session_key", "data/stats.json"):
        assert str(old / name) in copies, name
    assert result.token is None and r.written(layout.home / "clients.json") is None  # clients kept
    assert r.written(layout.home / ".env") == "FONT_RENDER_TOKEN=old\nRENDER_POOL_SIZE=3\nFONT_RENDER_PORT=8000\n"


def test_import_refuses_existing_home(layout, source, tmp_path):
    layout.home.mkdir(parents=True)
    (layout.home / "clients.json").write_text("{}")
    with pytest.raises(SystemExit, match="already has clients.json"):
        _install(layout, source, import_from=tmp_path)


def test_uninstall_keeps_data_by_default(layout):
    r = install.DryRunner(quiet=True)
    install.uninstall(layout, purge=False, runner=r)
    assert [str(layout.nssm), "remove", install.SERVICE, "confirm"] in r.commands()
    assert _has(r, "netsh", "delete") and ["reg", "delete", install.UNINSTALL_KEY, "/f"] in r.commands()
    assert ("rmtree", str(layout.home)) not in r.ops
    assert ("rmtree", str(install.start_menu_dir())) in r.ops


def test_uninstall_removes_all_but_the_running_interpreter(layout):
    for d in ("bin", "browsers", "cache", "python", "versions/1.1.0", "versions/1.2.0"):
        (layout.app / d).mkdir(parents=True)
    (layout.app / "current").write_text("1.2.0\n")
    r = install.DryRunner(quiet=True)
    install.uninstall(layout, purge=False, runner=r)
    gone = {op[1] for op in r.ops if op[0] in ("rmtree", "remove")}
    for name in ("bin", "browsers", "cache", "current", "versions/1.1.0"):
        assert str(layout.app / name) in gone, name
    # uninstall.cmd removes these once the process has exited
    assert str(layout.app / "python") not in gone and str(layout.app / "versions" / "1.2.0") not in gone


def test_uninstall_purge_deletes_home(layout):
    r = install.DryRunner(quiet=True)
    install.uninstall(layout, purge=True, runner=r)
    assert ("rmtree", str(layout.home)) in r.ops


def test_cli_dry_run(layout, source, capsys):
    assert cli.main(["install", "--dry-run", "--app", str(layout.app), "--home", str(layout.home),
                     "--source", str(source)]) == 0
    out = capsys.readouterr().out
    assert "dry run: not saved" in out and not layout.home.exists()
    assert cli.main(["uninstall", "--dry-run", "--quiet", "--app", str(layout.app),
                     "--home", str(layout.home)]) == 0


def test_set_env_line():
    assert install.set_env_line("", "A", "1") == "A=1\n"
    assert install.set_env_line("#A=0\nA=2\n", "A", "1") == "#A=0\nA=1\n"


# ---------------------------------------------------------------------------
# reset-admin
# ---------------------------------------------------------------------------

@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUDIT_LOG", tmp_path / "audit.log")
    reg = auth.ClientRegistry(tmp_path / "clients.json")
    monkeypatch.setattr(auth, "registry", reg)
    monkeypatch.setattr(config, "SESSION_KEY_FILE", tmp_path / ".session_key")
    return reg


def test_reset_admin_creates_client(registry):
    token = localadmin.reset_admin(registry)
    found = auth.ClientRegistry(registry.path).lookup(token)
    assert found and found[0] == "admin" and "admin" in found[1]["scopes"]
    entry = json.loads(config.AUDIT_LOG.read_text())
    assert entry["event"] == "reset-admin" and entry["created"] is True
    assert token not in config.AUDIT_LOG.read_text()


def test_reset_admin_rotates_and_reenables(registry):
    old = localadmin.reset_admin(registry)
    session = auth.issue_session("admin")
    assert auth.read_session(session) == "admin"
    client = registry.clients()["admin"]
    client.update(disabled=True, scopes=["render"])
    registry.save()
    new = localadmin.reset_admin(registry)
    assert registry.lookup(old) is None
    found = registry.lookup(new)
    assert found and not found[1]["disabled"] and "admin" in found[1]["scopes"]
    assert auth.read_session(session) is None  # web admin sessions of the old token end


def test_reset_admin_other_client(registry):
    token = localadmin.reset_admin(registry, "ops")
    assert registry.lookup(token)[0] == "ops"


# ---------------------------------------------------------------------------
# Launcher
# ---------------------------------------------------------------------------

@pytest.fixture
def launcher(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("launcher", ROOT / "deploy" / "windows" / "launcher.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setenv("FONT_RENDER_HOME", str(tmp_path / "home"))
    app = tmp_path / "app"
    app.mkdir()
    (app / "current").write_text("1.3.0\n")
    (app / "previous").write_text("1.2.0\n")
    return mod, app


def test_launcher_starts_current(launcher):
    mod, app = launcher
    assert mod.choose_version(app) == "1.3.0"
    assert not (app / "trial").exists()


def test_launcher_reverts_after_failed_trial_starts(launcher, tmp_path):
    mod, app = launcher
    (app / "trial").write_text("")
    for _ in range(mod.MAX_TRIAL_STARTS):
        assert mod.choose_version(app) == "1.3.0"
    assert mod.choose_version(app) == "1.2.0"
    assert (app / "current").read_text().strip() == "1.2.0"
    assert (app / "previous").read_text().strip() == "1.3.0"
    assert not (app / "trial").exists()
    entry = json.loads((tmp_path / "home" / "logs" / "audit.log").read_text())
    assert entry["event"] == "auto-revert" and entry["failed"] == "1.3.0"
    assert mod.choose_version(app) == "1.2.0"  # stays there


# ---------------------------------------------------------------------------
# Addresses shown to admins (netinfo)
# ---------------------------------------------------------------------------

def test_addresses_skip_loopback_link_local_and_down(monkeypatch):
    import socket
    from types import SimpleNamespace as NS

    from font_render import netinfo
    addrs = {
        "Ethernet": [NS(family=socket.AF_INET, address="192.168.1.23")],
        "Loopback": [NS(family=socket.AF_INET, address="127.0.0.1")],
        "Wi-Fi": [NS(family=socket.AF_INET, address="169.254.10.2")],  # no DHCP answer
        "Down": [NS(family=socket.AF_INET, address="10.0.0.5")],
        "v6only": [NS(family=socket.AF_INET6, address="fe80::1")],
    }
    stats = {n: NS(isup=n != "Down") for n in addrs}
    monkeypatch.setattr(netinfo.psutil, "net_if_addrs", lambda: addrs)
    monkeypatch.setattr(netinfo.psutil, "net_if_stats", lambda: stats)
    monkeypatch.setattr(netinfo.socket, "gethostname", lambda: "STUDIO-PC")
    a = netinfo.addresses(8000)
    assert a["by_name"] == ["http://STUDIO-PC:8000", "http://studio-pc.local:8000"]
    assert a["by_ip"] == [{"interface": "Ethernet", "url": "http://192.168.1.23:8000"}]
