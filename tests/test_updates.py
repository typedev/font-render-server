"""
Tests for release updates of an installed server: release choice, signature check,
the update pipeline (with a fake GitHub and fake uv/playwright), rollback, pruning,
the /update and /rollback endpoints in the installed layout (docs/distribution.md#updates).

    uv run pytest tests/test_updates.py -v
"""

import asyncio
import base64
import hashlib
import io
import json
import subprocess
import zipfile
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from starlette.testclient import TestClient

from font_render import admin, app as appmod, auth, config, install, localadmin, release_key, updates

REPO = "typedev/font-render-server"


# ---------------------------------------------------------------------------
# Fake GitHub with signed releases
# ---------------------------------------------------------------------------

class FakeGitHub:
    def __init__(self, key: Ed25519PrivateKey):
        self.key = key
        self.files: dict[str, bytes] = {}
        self.releases: list[dict] = []
        self.calls: list[str] = []

    def add(self, version: str, prerelease=False, tamper_zip=False, bad_sig=False):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("pyproject.toml", f'[project]\nversion = "{version}"\n')
            for name in install.BIN_FILES:
                z.writestr(f"deploy/windows/{name}", f"{name} {version}")
        data = buf.getvalue()
        zip_name = f"font-render-{version}.zip"
        sums = f"{hashlib.sha256(data).hexdigest()}  {zip_name}\n".encode()
        signer = Ed25519PrivateKey.generate() if bad_sig else self.key
        sig = json.dumps({"key_id": updates.key_id(_public(self.key)),
                          "signature": base64.b64encode(signer.sign(sums)).decode()}).encode()
        base = f"https://github.com/{REPO}/releases/download/v{version}"
        self.files[f"{base}/{zip_name}"] = data + (b"x" if tamper_zip else b"")
        self.files[f"{base}/SHA256SUMS"] = sums
        self.files[f"{base}/SHA256SUMS.sig"] = sig
        self.releases.insert(0, {
            "tag_name": f"v{version}", "prerelease": prerelease, "draft": False,
            "published_at": "2026-10-19T00:00:00Z", "body": f"notes {version}",
            "html_url": f"https://github.com/{REPO}/releases/tag/v{version}",
            "assets": [{"name": n, "browser_download_url": f"{base}/{n}"}
                       for n in (zip_name, "SHA256SUMS", "SHA256SUMS.sig")]})

    def get(self, url, headers=None, timeout=60):
        self.calls.append(url)
        if url.startswith(f"https://api.github.com/repos/{REPO}/releases"):
            return 200, {"ETag": '"e1"'}, json.dumps(self.releases).encode()
        if url not in self.files:
            raise updates.UpdateError(f"{url}: HTTP 404")
        return 200, {}, self.files[url]


class FilesOnlyRunner(install.Runner):
    """Real file operations; commands (reg) are only recorded."""
    commands: list = []

    def run(self, cmd, *, check=True, env=None):
        self.commands.append([str(c) for c in cmd])
        return subprocess.CompletedProcess(cmd, 0, "", "")


def _public(key: Ed25519PrivateKey) -> bytes:
    return key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


@pytest.fixture
def key(monkeypatch):
    k = Ed25519PrivateKey.generate()
    monkeypatch.setattr(release_key, "TRUSTED_KEYS", [base64.b64encode(_public(k)).decode()])
    return k


@pytest.fixture
def gh(key, monkeypatch):
    fake = FakeGitHub(key)
    monkeypatch.setattr(updates, "_http_get", fake.get)
    updates._cache.update(etag=None, releases=[], checked=0.0, error=None)
    return fake


@pytest.fixture
def lay(tmp_path, monkeypatch):
    """An installed layout running 1.0.0; fake uv/playwright/selfcheck, recorded."""
    lay = install.Layout(app=tmp_path / "app", home=tmp_path / "home")
    (lay.versions / "1.0.0").mkdir(parents=True)
    (lay.app / "bin").mkdir()
    lay.home.mkdir()
    (lay.app / "current").write_text("1.0.0\n")
    (lay.home / "install.json").write_text(json.dumps({"version": "1.0.0", "channel": "stable"}))
    monkeypatch.setattr(config, "APP", lay.app)
    monkeypatch.setattr(config, "HOME", lay.home)
    monkeypatch.setattr(config, "SERVICE_DIR", lay.versions / "1.0.0")
    monkeypatch.setattr(config, "INSTALL_FILE", lay.home / "install.json")
    monkeypatch.setattr(config, "AUDIT_LOG", lay.home / "logs" / "audit.log")
    monkeypatch.setattr(config, "UPDATE_REPO", REPO)
    monkeypatch.setattr(updates, "Runner", FilesOnlyRunner)
    lay.ran = []
    lay.fail = None

    def run(cmd, cwd, env):
        lay.ran.append([Path(str(cmd[0])).name, *map(str, cmd[1:3])])
        if lay.fail and lay.fail in " ".join(map(str, cmd)):
            raise updates.UpdateError(f"{lay.fail} failed")
    monkeypatch.setattr(updates, "_run", run)
    updates.job.__init__()
    return lay


def _audit(lay) -> list[dict]:
    path = lay.home / "logs" / "audit.log"
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


# ---------------------------------------------------------------------------
# Choosing a release, signatures
# ---------------------------------------------------------------------------

def test_newer_releases_by_channel():
    rows = [{"version": v, "prerelease": pre} for v, pre in
            (("1.0.0", False), ("1.2.0", False), ("1.3.0b1", True), ("1.1.0", False), ("0.9.0", False))]
    assert [r["version"] for r in updates.newer_releases(rows, "1.0.0", "stable")] == ["1.2.0", "1.1.0"]
    assert [r["version"] for r in updates.newer_releases(rows, "1.0.0", "beta")] == ["1.3.0b1", "1.2.0", "1.1.0"]
    assert updates.newer_releases(rows, "1.2.0", "stable") == []


def test_signature(key):
    sums = b"abc  font-render-1.0.0.zip\n"
    sig = json.dumps({"key_id": updates.key_id(_public(key)),
                      "signature": base64.b64encode(key.sign(sums)).decode()}).encode()
    assert updates.verify_signature(sums, sig) == updates.key_id(_public(key))
    with pytest.raises(updates.UpdateError, match="bad release signature"):
        updates.verify_signature(sums + b"x", sig)
    with pytest.raises(updates.UpdateError, match="unknown key"):
        updates.verify_signature(sums, sig, trusted=[])
    with pytest.raises(updates.UpdateError):
        updates.verify_signature(sums, b"not json")


def test_shipped_trusted_key_is_valid():
    assert release_key.TRUSTED_KEYS
    for k in release_key.TRUSTED_KEYS:
        assert len(base64.b64decode(k)) == 32


# ---------------------------------------------------------------------------
# Update pipeline
# ---------------------------------------------------------------------------

def test_update_switches_and_prunes(lay, gh):
    for v in ("0.7.0", "0.8.0", "0.9.0"):  # old versions on disk
        (lay.versions / v).mkdir()
    (lay.app / "previous").write_text("0.9.0\n")
    gh.add("1.1.0")
    release = asyncio.run(updates.find_release(None))
    assert release["version"] == "1.1.0"
    updates.install_release(release, "admin")
    assert lay.read_pointer("current") == "1.1.0" and lay.read_pointer("previous") == "1.0.0"
    assert (lay.app / "trial").read_text().strip() == "0"
    assert (lay.versions / "1.1.0" / "pyproject.toml").is_file()
    assert [r[:2] for r in lay.ran] == [["uv.exe", "sync"], ["python.exe", "-m"], ["python.exe", "-m"]]
    # current, previous and one more release are kept
    assert sorted(d.name for d in lay.versions.iterdir()) == ["0.9.0", "1.0.0", "1.1.0"]
    assert json.loads((lay.home / "install.json").read_text())["version"] == "1.1.0"
    assert (lay.bin / "launcher.py").read_text() == "launcher.py 1.1.0"  # from the new version
    assert any("DisplayVersion" in c and "1.1.0" in c for c in FilesOnlyRunner.commands)
    entry = _audit(lay)[-1]
    assert entry["event"] == "update" and entry["version"] == "1.1.0" and entry["by"] == "admin"
    assert not (lay.home / "staging").exists()


def test_bad_signature_downloads_nothing_else(lay, gh):
    gh.add("1.1.0", bad_sig=True)
    release = asyncio.run(updates.find_release(None))
    with pytest.raises(updates.UpdateError, match="bad release signature"):
        updates.install_release(release, "admin")
    assert not any(c.endswith(".zip") for c in gh.calls)
    assert lay.read_pointer("current") == "1.0.0" and not (lay.versions / "1.1.0").exists()


def test_tampered_zip_is_refused(lay, gh):
    gh.add("1.1.0", tamper_zip=True)
    release = asyncio.run(updates.find_release(None))
    with pytest.raises(updates.UpdateError, match="does not match SHA256SUMS"):
        updates.install_release(release, "admin")
    assert lay.read_pointer("current") == "1.0.0" and not (lay.versions / "1.1.0").exists()


@pytest.mark.parametrize("step", ["sync", "playwright", "selfcheck"])
def test_failed_step_leaves_running_version(lay, gh, step):
    gh.add("1.1.0")
    lay.fail = step
    release = asyncio.run(updates.find_release(None))
    with pytest.raises(updates.UpdateError):
        updates.install_release(release, "admin")
    assert lay.read_pointer("current") == "1.0.0" and lay.read_pointer("previous") is None
    assert not (lay.versions / "1.1.0").exists() and not (lay.app / "trial").exists()


def test_beta_needs_beta_channel(lay, gh):
    gh.add("1.1.0b1", prerelease=True)
    with pytest.raises(updates.UpdateError, match="up-to-date"):
        asyncio.run(updates.find_release(None))
    updates.set_channel("beta")
    updates._cache["checked"] = 0
    assert asyncio.run(updates.find_release(None))["version"] == "1.1.0b1"


def test_older_version_refused(lay, gh):
    gh.add("0.9.0")
    with pytest.raises(updates.UpdateError, match="not newer"):
        asyncio.run(updates.find_release("0.9.0"))


def test_rollback(lay):
    (lay.versions / "0.9.0").mkdir()
    (lay.app / "previous").write_text("0.9.0\n")
    (lay.app / "trial").write_text("1")
    assert updates.rollback("admin") == "0.9.0"
    assert lay.read_pointer("current") == "0.9.0" and lay.read_pointer("previous") == "1.0.0"
    assert not (lay.app / "trial").exists()
    assert _audit(lay)[-1]["event"] == "rollback"


def test_rollback_without_previous(lay):
    with pytest.raises(updates.UpdateError, match="no previous"):
        updates.rollback("admin")


def test_mark_healthy_after_warm_up(lay):
    (lay.app / "trial").write_text("1")

    async def scenario():
        async def warm():
            return None
        await updates.mark_healthy(asyncio.create_task(warm()))
    asyncio.run(scenario())
    assert not (lay.app / "trial").exists()
    assert _audit(lay)[-1]["event"] == "update-healthy"


def test_failed_warm_up_keeps_trial(lay):
    (lay.app / "trial").write_text("1")

    async def scenario():
        async def warm():
            raise RuntimeError("no browser")
        await updates.mark_healthy(asyncio.create_task(warm()))
    asyncio.run(scenario())
    assert (lay.app / "trial").exists()


# ---------------------------------------------------------------------------
# Endpoints in the installed layout
# ---------------------------------------------------------------------------

@pytest.fixture
def client(lay, gh, tmp_path, monkeypatch):
    reg = auth.ClientRegistry(tmp_path / "clients.json")
    monkeypatch.setattr(auth, "registry", reg)
    monkeypatch.setattr(config, "SESSION_KEY_FILE", tmp_path / ".session_key")
    monkeypatch.setattr(admin, "_shutdown", lambda: None)
    token = localadmin.reset_admin(reg)
    with TestClient(appmod.app) as c:
        c.headers["Authorization"] = f"Bearer {token}"
        yield c


def test_update_endpoint_runs_job(client, lay, gh):
    r = client.post("/update")
    assert r.status_code == 200 and r.json()["status"] == "up-to-date"
    gh.add("1.1.0")
    check = client.post("/admin/api/check-updates").json()
    assert check["mode"] == "release" and check["available"][0]["version"] == "1.1.0"
    r = client.post("/update")
    assert r.status_code == 202 and r.json()["new_version"] == "1.1.0"
    for _ in range(100):
        state = client.get("/admin/api/update-status").json()
        if state["state"] != "running":
            break
        asyncio.run(asyncio.sleep(0.05))
    assert state["state"] == "restarting", state
    assert lay.read_pointer("current") == "1.1.0"
    status = client.get("/admin/api/status").json()["server"]
    assert status["layout"] == "installed" and status["rollback_target"] == "1.0.0"


def test_rollback_endpoint(client, lay):
    (lay.versions / "0.9.0").mkdir()
    (lay.app / "previous").write_text("0.9.0\n")
    r = client.post("/rollback")
    assert r.status_code == 200 and r.json()["version"] == "0.9.0"


def test_update_channel_endpoint(client, lay):
    assert client.post("/admin/api/update-channel", data={"channel": "beta"}).json() == {"channel": "beta"}
    assert updates.channel() == "beta"
    assert client.post("/admin/api/update-channel", data={"channel": "nightly"}).status_code == 400


def test_client_follows_job(monkeypatch, capsys):
    from font_render_client import client as cli

    class Resp:
        def __init__(self, data):
            self.data = data

        def json(self):
            return self.data

    answers = iter([
        Resp({"state": "running", "log": ["a downloading 1.1.0"]}),
        Resp({"state": "restarting", "log": ["a downloading 1.1.0", "b switching"]}),
        Resp({"status": "ok", "release": "1.0.0"}),  # still the old process
        Resp({"status": "ok", "release": "1.1.0"}),
    ])
    monkeypatch.setattr(cli.requests, "get", lambda *a, **k: next(answers))
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    cli._follow_release_update("http://x", {"new_version": "1.1.0", "old_version": "1.0.0"})
    out = capsys.readouterr().out
    assert out.count("downloading 1.1.0") == 1 and "switching" in out
    assert "Server is back (version: 1.1.0)" in out
