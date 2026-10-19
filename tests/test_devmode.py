"""
Tests for developer mode: local-only switch-on, expiry, the dev scope the admin API cannot
grant, /dev/* answering 404 while off or from outside allow_from, deploy of an uploaded
tree (fonts refused), back to a release (docs/distribution.md#developer-mode).

    uv run pytest tests/test_devmode.py -v
"""

import asyncio
import io
import json
import subprocess
import time
import zipfile
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from font_render import admin, app as appmod, auth, config, devmode, install, localadmin, updates


class FilesOnlyRunner(install.Runner):
    def run(self, cmd, *, check=True, env=None):
        return subprocess.CompletedProcess(cmd, 0, "", "")


@pytest.fixture
def lay(tmp_path, monkeypatch):
    lay = install.Layout(app=tmp_path / "app", home=tmp_path / "home")
    (lay.versions / "1.0.0").mkdir(parents=True)
    (lay.app / "bin").mkdir()
    lay.home.mkdir()
    (lay.app / "current").write_text("1.0.0\n")
    for name, value in (("APP", lay.app), ("HOME", lay.home), ("SERVICE_DIR", lay.versions / "1.0.0"),
                        ("INSTALL_FILE", lay.home / "install.json"), ("DEVMODE_FILE", lay.home / "devmode.json"),
                        ("AUDIT_LOG", lay.home / "logs" / "audit.log"),
                        ("SESSION_KEY_FILE", tmp_path / ".session_key")):
        monkeypatch.setattr(config, name, value)
    monkeypatch.setattr(updates, "Runner", FilesOnlyRunner)
    monkeypatch.setattr(updates, "_run", lambda cmd, cwd, env: None)
    monkeypatch.setattr(admin, "_shutdown", lambda: None)
    reg = auth.ClientRegistry(tmp_path / "clients.json")
    monkeypatch.setattr(auth, "registry", reg)
    lay.registry = reg
    lay.admin_token = localadmin.reset_admin(reg)
    updates.job.__init__()
    return lay


@pytest.fixture
def c(lay):
    with TestClient(appmod.app) as client:
        yield client


def _audit(lay):
    return [json.loads(x)["event"] for x in (lay.home / "logs" / "audit.log").read_text().splitlines()]


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def _tree(extra: dict | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("pyproject.toml", "[project]\nversion = '1.1.0'\n")
        for name, data in (extra or {}).items():
            z.writestr(name, data)
    return buf.getvalue()


def _enable(lay, **kw):
    args = dict(clients=["alexander"], reason="development", days=30, allow_from=[], by="LURKBOX\\me")
    args.update(kw)
    return devmode.enable(lay.registry, **args)


def _wait_job():
    for _ in range(200):
        if updates.job.state != "running":
            return updates.job.state
        time.sleep(0.02)
    return updates.job.state


# ---------------------------------------------------------------------------

def test_dev_paths_are_404_while_off(c, lay):
    for method, path in (("get", "/dev/status"), ("post", "/dev/deploy"), ("post", "/dev/release")):
        r = getattr(c, method)(path, headers=_bearer(lay.admin_token))
        assert r.status_code == 404, path
        assert getattr(c, method)(path).status_code == 404, path  # no token, bad token alike
        assert getattr(c, method)(path, headers=_bearer("nope")).status_code == 404, path


def test_enable_grants_dev_scope_to_named_clients_only(c, lay):
    tokens = _enable(lay)
    dev = tokens["alexander"]
    assert lay.registry.lookup(dev)[1]["scopes"] == ["dev"]
    assert c.get("/dev/status", headers=_bearer(dev)).json()["dev_mode"]["enabled"] is True
    assert c.get("/dev/status", headers=_bearer(lay.admin_token)).status_code == 403  # admin is not dev
    assert c.get("/dev/status").status_code == 401
    assert _audit(lay)[-1] == "dev-mode-on"
    # a dev-only token cannot render or use the admin API
    assert c.post("/admin/clients/list", headers=_bearer(dev)).status_code == 403


def test_admin_api_cannot_grant_dev(c, lay):
    r = c.post("/admin/clients/add", data={"client_id": "x", "scopes": "render,dev"},
               headers=_bearer(lay.admin_token))
    assert r.status_code == 400
    _enable(lay)
    r = c.post("/admin/clients/add", data={"client_id": "y", "scopes": "dev"}, headers=_bearer(lay.admin_token))
    assert r.status_code == 400


def test_no_endpoint_enables_dev_mode(lay):
    paths = {r.path for r in appmod.app.routes if hasattr(r, "path")}
    assert {p for p in paths if "dev-mode" in p or "devmode" in p} == {"/admin/api/dev-mode/off"}


def test_admin_switches_off(c, lay):
    dev = _enable(lay)["alexander"]
    r = c.post("/admin/api/dev-mode/off", headers=_bearer(lay.admin_token))
    assert r.json() == {"enabled": False, "was_on": True}
    assert c.get("/dev/status", headers=_bearer(dev)).status_code == 404
    assert "dev" not in lay.registry.lookup(dev)[1]["scopes"]
    assert _audit(lay)[-1] == "dev-mode-off"
    banner = c.get("/admin/api/status", headers=_bearer(lay.admin_token)).json()["server"]["dev_mode"]
    assert banner == {"enabled": False}


def test_expiry(c, lay):
    dev = _enable(lay)["alexander"]
    data = json.loads(config.DEVMODE_FILE.read_text())
    data["until"] = "2000-01-01T00:00:00"
    config.DEVMODE_FILE.write_text(json.dumps(data))
    assert c.get("/dev/status", headers=_bearer(dev)).status_code == 404
    assert "dev" not in lay.registry.lookup(dev)[1]["scopes"]
    assert _audit(lay)[-1] == "dev-mode-expired"
    assert devmode.view() == {"enabled": False, "expired": "2000-01-01T00:00:00"}


def test_no_expiry_with_days_0(lay):
    _enable(lay, days=0)
    assert devmode.view()["until"] is None and devmode.active()


def test_allow_from(c, lay):
    dev = _enable(lay, allow_from=["10.0.0.0/8"])["alexander"]
    assert c.get("/dev/status", headers=_bearer(dev)).status_code == 404  # testclient is not in 10/8
    state = {"allow_from": ["192.168.1.0/24"]}
    assert devmode._peer_allowed(state, "192.168.1.37")
    assert not devmode._peer_allowed(state, "192.168.2.1")
    assert not devmode._peer_allowed(state, "not-an-ip")
    assert devmode._peer_allowed({"allow_from": []}, "8.8.8.8")


def test_deploy_needs_fields_once_allowed(c, lay):
    dev = _enable(lay)["alexander"]
    assert c.post("/dev/deploy", headers=_bearer(dev)).status_code == 400


def test_bad_cidr_rejected(lay):
    with pytest.raises(ValueError):
        _enable(lay, allow_from=["10.0.0.0/33"])


def test_deploy_upload(c, lay):
    dev = _enable(lay)["alexander"]
    r = c.post("/dev/deploy", headers=_bearer(dev), files={"archive": ("t.zip", _tree())},
               data={"commit": "a1b2c3d4e5f6", "dirty": "false"})
    assert r.status_code == 202, r.text
    assert _wait_job() == "restarting", updates.job.view()
    assert lay.read_pointer("current") == "dev-a1b2c3d" and lay.read_pointer("previous") == "1.0.0"
    assert (lay.app / "trial").exists()
    assert "dev-deploy" in _audit(lay)


def test_dirty_deploy_name(lay):
    _enable(lay)
    version = updates.install_upload(_tree(), "a1b2c3d", True, "alexander")
    assert version.startswith("dev-a1b2c3d-dirty-")


def test_upload_with_fonts_refused_without_naming_them(lay):
    _enable(lay)
    with pytest.raises(updates.UpdateError) as e:
        updates.install_upload(_tree({"secret/MyFont-Bold.ttf": b"x"}), "a1b2c3d", True, "alexander")
    assert "1 font file" in str(e.value) and "MyFont" not in str(e.value)
    assert lay.read_pointer("current") == "1.0.0"
    assert not any(d.name.startswith("dev-") for d in lay.versions.iterdir())


def test_back_to_release_on_disk(c, lay):
    dev = _enable(lay)["alexander"]
    updates.install_upload(_tree(), "a1b2c3d", False, "alexander")
    r = c.post("/dev/release", headers=_bearer(dev))
    assert r.json() == {"status": "switched", "version": "1.0.0", "restarting": True}
    assert lay.read_pointer("current") == "1.0.0" and lay.read_pointer("previous") == "dev-a1b2c3d"


def test_dev_builds_pruned_when_not_current_or_previous(lay):
    _enable(lay)
    updates.install_upload(_tree(), "aaaaaaa", False, "alexander")
    updates.install_upload(_tree(), "bbbbbbb", False, "alexander")
    updates.install_upload(_tree(), "ccccccc", False, "alexander")
    assert sorted(d.name for d in lay.versions.iterdir()) == ["1.0.0", "dev-bbbbbbb", "dev-ccccccc"]


def test_info_shows_dev_mode(c, lay):
    _enable(lay, days=0)
    info = c.get("/info", headers=_bearer(lay.admin_token)).json()
    assert info["dev_mode"] == {"enabled": True, "until": None}
    assert "dev_mode" not in c.get("/health").json()


def test_checkout_never_serves_dev(c, lay, monkeypatch):
    dev = _enable(lay)["alexander"]
    monkeypatch.setattr(config, "APP", None)
    assert c.get("/dev/status", headers=_bearer(dev)).status_code == 404


def test_cli_requires_reason_and_client(lay, monkeypatch, capsys):
    from font_render import __main__ as cli
    monkeypatch.setattr(config, "CLIENTS_FILE", lay.registry.path)
    with pytest.raises(SystemExit):
        cli.main(["dev-mode", "on", "--client", "alexander"])
    assert cli.main(["dev-mode", "on", "--client", "alexander", "--reason", "dev", "--days", "0"]) == 0
    out = capsys.readouterr().out
    assert "New client 'alexander'" in out and "until switched off" in out
    assert cli.main(["dev-mode", "status"]) == 0
    assert cli.main(["dev-mode", "off"]) == 0
    assert not devmode.view()["enabled"]


def test_audit_endpoint(c, lay):
    _enable(lay)
    rows = c.get("/admin/api/audit", headers=_bearer(lay.admin_token)).json()["entries"]
    assert rows[0]["event"] == "dev-mode-on" and rows[-1]["event"] == "reset-admin"


# ---------------------------------------------------------------------------
# Client packing (font_render_client.deploy)
# ---------------------------------------------------------------------------

@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    run = lambda *a: subprocess.run(["git", *a], cwd=root, check=True, capture_output=True)  # noqa: E731
    run("init", "-q")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "t")
    (root / ".gitignore").write_text("renders/\n.env\n")
    (root / "a.py").write_text("a = 1\n")
    run("add", ".")
    run("commit", "-q", "-m", "init")
    return root


def test_pack_ref_and_worktree(repo):
    from font_render_client import deploy
    data, commit = deploy.pack_ref("HEAD", cwd=repo)
    assert len(commit) == 40
    assert sorted(zipfile.ZipFile(io.BytesIO(data)).namelist()) == [".gitignore", "a.py"]
    (repo / "a.py").write_text("a = 2\n")  # uncommitted change
    (repo / "new.py").write_text("")  # untracked, not ignored
    (repo / "renders").mkdir()
    (repo / "renders" / "out.png").write_bytes(b"x")  # ignored
    (repo / ".env").write_text("FONT_RENDER_DEV_TOKEN=secret\n")  # ignored
    data, head, dirty, count = deploy.pack_worktree(cwd=repo)
    z = zipfile.ZipFile(io.BytesIO(data))
    assert sorted(z.namelist()) == [".gitignore", "a.py", "new.py"] and count == 3
    assert z.read("a.py") == b"a = 2\n" and dirty and head == commit


def test_pack_refuses_fonts(repo):
    from font_render_client import deploy
    (repo / "Test-Regular.otf").write_bytes(b"x")
    with pytest.raises(deploy.DeployError, match="Test-Regular.otf"):
        deploy.pack_worktree(cwd=repo)
