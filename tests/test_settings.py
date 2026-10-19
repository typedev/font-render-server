"""
Tests for settings editable in the web admin (config.EDITABLE, data/settings.json).

    uv run pytest tests/test_settings.py -v
"""

import json

import pytest
from starlette.formparsers import MultiPartParser
from starlette.testclient import TestClient

from font_render import app as appmod, auth, config

ADMIN = "admin-secret"


@pytest.fixture
def c(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UPDATE_TOKEN", ADMIN)
    monkeypatch.setattr(auth, "registry", auth.ClientRegistry(tmp_path / "clients.json"))
    with TestClient(appmod.app) as client:
        r = client.post("/admin/clients/add", data={"client_id": "boss", "scopes": "admin", "token": ADMIN})
        client.headers["Authorization"] = f"Bearer {r.json()['token']}"
        yield client


def _view(c):
    return {x["name"]: x for x in c.get("/admin/api/config").json()["settings"]}


def test_live_setting_applies_and_persists(c):
    r = c.post("/admin/api/config", data={"QUEUE_MAX": "32", "TIMEOUT_S": "90"})
    assert r.status_code == 200, r.text
    assert config.QUEUE_MAX == 32 and config.TIMEOUT_S == 90.0
    v = _view(c)["QUEUE_MAX"]
    assert v["value"] == 32 and v["source"] == "admin" and not v["pending_restart"]
    assert json.loads(config.SETTINGS_FILE.read_text()) == {"QUEUE_MAX": 32, "TIMEOUT_S": 90.0}
    # Reset -> back to the env/default value, override removed from the file
    c.post("/admin/api/config", data={"QUEUE_MAX": ""})
    assert config.QUEUE_MAX == config.BASE_VALUES["QUEUE_MAX"]
    assert "QUEUE_MAX" not in json.loads(config.SETTINGS_FILE.read_text())


def test_restart_only_setting_is_pending(c):
    before = config.CONCURRENCY
    c.post("/admin/api/config", data={"CONCURRENCY": str(before + 2)})
    assert config.CONCURRENCY == before  # the running semaphore is unchanged
    v = _view(c)["CONCURRENCY"]
    assert v["saved"] == before + 2 and v["pending_restart"]
    assert c.get("/admin/api/status").json()["pending_restart"] == ["CONCURRENCY"]
    # A restart reads the file
    config.load_overrides()
    assert config.CONCURRENCY == before + 2


def test_validation_is_all_or_nothing(c):
    before = config.QUEUE_MAX
    for bad in ({"QUEUE_MAX": "20", "CLIENT_MAX": "0"}, {"QUEUE_MAX": "abc"}, {"QUEUE_MAX": "2.5"}):
        r = c.post("/admin/api/config", data=bad)
        assert r.status_code == 400
    assert config.QUEUE_MAX == before
    assert c.post("/admin/api/config", data={"NOT_A_SETTING": "1"}).status_code == 400


def test_hooks_font_size_and_cache(c):
    c.post("/admin/api/config", data={"MAX_FONT_SIZE": str(20 * config.MB)})
    assert MultiPartParser.spool_max_size > 20 * config.MB  # uploads stay in memory
    fc = appmod.app.state.font_cache
    fc.put("x", b"a" * (20 * config.MB), "a.ttf")
    c.post("/admin/api/config", data={"FONT_CACHE_MAX_BYTES": str(16 * config.MB), "FONT_CACHE_TTL_S": "120"})
    assert fc.max_bytes == 16 * config.MB and fc.bytes <= fc.max_bytes and fc.ttl_s == 120


def test_needs_admin(c):
    del c.headers["Authorization"]
    assert c.get("/admin/api/config").status_code == 401
    assert c.post("/admin/api/config", data={"QUEUE_MAX": "1"}).status_code == 401


def test_broken_or_out_of_range_file_is_ignored(tmp_path):
    config.SETTINGS_FILE.write_text(json.dumps({"QUEUE_MAX": 9999, "PAGE_CACHE": 3, "BOGUS": 1}))
    config.load_overrides()
    assert config.OVERRIDES == {"PAGE_CACHE": 3}
    config.SETTINGS_FILE.write_text("{nope")
    config.load_overrides()
    assert config.OVERRIDES == {}


def test_firefox_prefs(c, tmp_path, monkeypatch):
    from font_render import pool
    monkeypatch.setattr(config, "FIREFOX_PREFS_FILE", tmp_path / "ff.json")
    assert c.get("/admin/api/firefox-prefs").json() == {"prefs": {}}
    prefs = {"gfx.font_rendering.cleartype_params.cleartype_level": 100, "x.flag": True}
    r = c.post("/admin/api/firefox-prefs", json={"prefs": prefs})
    assert r.status_code == 200 and r.json()["prefs"] == prefs
    assert pool.firefox_prefs() == prefs
    assert c.post("/admin/api/firefox-prefs", json={"prefs": {"a": [1]}}).status_code == 400
    assert c.post("/admin/api/firefox-prefs", json=[1]).status_code == 400
    assert c.get("/admin/api/status").json()["windows"] is None  # not Windows here
