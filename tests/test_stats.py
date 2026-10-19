"""
Tests for request statistics: per-client traffic bytes, per-minute load,
overload episodes, persistence, and the /admin/api/load|traffic endpoints.

    uv run pytest tests/test_stats.py -v
"""

import time

import pytest
from starlette.testclient import TestClient

from font_render import app as appmod, auth, config, pool, stats as stats_mod
from font_render.stats import Stats, stats
from test_geometry import _find_font

ADMIN = "admin-secret"


@pytest.fixture
def c(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UPDATE_TOKEN", ADMIN)
    monkeypatch.setattr(auth, "registry", auth.ClientRegistry(tmp_path / "clients.json"))
    with TestClient(appmod.app) as client:
        yield client


def _add(c, client_id, scopes="render"):
    r = c.post("/admin/clients/add", data={"client_id": client_id, "scopes": scopes, "token": ADMIN})
    return r.json()["token"]


def test_traffic_counts_bytes_per_client(c):
    font = _find_font()
    token = _add(c, "renderer")
    r = c.post("/render", files=[("fonts", ("a.ttf", font))], data={"ppem": "12", "layout": "plain", "text": "a"},
               headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    admin = _add(c, "boss", "admin")
    hdr = {"Authorization": f"Bearer {admin}"}
    c.get("/admin/api/traffic", headers=hdr)  # a request is counted after its response
    t = c.get("/admin/api/traffic", headers=hdr).json()["clients"]
    tr = t["renderer"]
    assert tr["today"]["in"] > len(font)  # multipart body includes the font
    assert tr["today"]["out"] == len(r.content)
    assert tr["total"]["renders"] == 1 and tr["week"]["req"] == 1
    assert "boss" in t  # admin reads count as traffic of that client


def test_minutes_and_latency():
    s = Stats()
    s.record("ip", "a", "POST", "/render", 200, 100.0, 1000, 5000)
    s.record("ip", "a", "POST", "/render", 200, 300.0, 1000, 5000)
    s.record("ip", "b", "POST", "/render", 429, 1.0, 10, 20, {"busy": "client_limit"})
    s.sample(busy=3, queued=2, cpu=50.0)
    (m,) = s.minutes_view(5)
    assert (m["req"], m["renders"], m["err"], m["client_limit"]) == (3, 2, 1, 1)
    assert (m["in"], m["out"]) == (2010, 10020)
    assert m["busy_max"] == 3 and m["queue_max"] == 2 and m["cpu"] == 50.0
    assert m["p50"] == 300.0 and m["lat_max"] == 300.0
    assert s.episode is None  # a per-client limit is not a server overload


def test_overload_episode_groups_events(monkeypatch):
    now = [1_000_000.0]
    monkeypatch.setattr(stats_mod.time, "time", lambda: now[0])
    s = Stats()
    s.record("ip", "a", "POST", "/render", 429, 1.0, info={"busy": "queue_full"})
    now[0] += 10
    s.record("ip", "b", "POST", "/render", 200, 9000.0, info={"queue_s": config.OVERLOAD_WAIT_S + 1})
    now[0] += 20
    s.record("ip", "a", "POST", "/render", 504, 60000.0)
    ep = s.episode
    assert ep["severity"] == "critical"
    assert ep["counts"] == {"queue_full": 1, "slow_queue": 1, "timeout": 1}
    assert ep["clients"] == {"a": 2, "b": 1}
    assert ep["max_wait_ms"] >= config.OVERLOAD_WAIT_S * 1000
    # Quiet for longer than the gap: the next sample closes it
    now[0] += stats_mod.EPISODE_GAP_S + 1
    s.sample(0, 0)
    assert s.episode is None and len(s.episodes) == 1
    assert s.episodes[0]["end"] - s.episodes[0]["start"] == 30
    # A slow queue alone is only a warning
    s.record("ip", "a", "POST", "/render", 200, 9000.0, info={"queue_s": config.OVERLOAD_WAIT_S})
    assert s.episode["severity"] == "warning"
    assert [e["ongoing"] for e in s.episodes_view()] == [True, False]


def test_save_and_load_roundtrip():
    s = Stats()
    s.record("ip", "a", "POST", "/render", 200, 100.0, 10, 20)
    s.record("ip", "a", "POST", "/render", 429, 1.0, info={"busy": "queue_full"})
    s.sample(2, 1)
    s.save()
    t = Stats()
    t.load()
    assert t.traffic == s.traffic
    assert t.minutes_view(5)[0]["renders"] == 1
    assert len(t.episodes) == 1  # the open episode is saved too
    # Same minute after a restart keeps counting
    t.record("ip", "a", "POST", "/render", 200, 50.0, 1, 2)
    assert t.minutes_view(5)[0]["renders"] == 2


def test_broken_stats_file_is_ignored():
    config.STATS_FILE.write_text("{not json")
    s = Stats()
    s.load()
    assert s.traffic == {} and not s.minutes


def test_queue_full_is_recorded_from_render(c, monkeypatch):
    font = _find_font()

    class Busy:
        def __init__(self, *a, **k): ...
        async def __aenter__(self): raise pool.PoolBusy(3, "queue_full")
        async def __aexit__(self, *a): return False

    monkeypatch.setattr(appmod._pool, "acquire", lambda *a, **k: Busy())
    r = c.post("/render", files=[("fonts", ("a.ttf", font))], data={"ppem": "12"})
    assert r.status_code == 429
    assert stats.episode["counts"] == {"queue_full": 1}
    admin = _add(c, "boss", "admin")
    d = c.get("/admin/api/load", params={"minutes": 60}, headers={"Authorization": f"Bearer {admin}"}).json()
    assert d["episodes"][0]["ongoing"] is True
    assert d["episodes_in_range"] and sum(m["rejected"] for m in d["minutes"]) == 1
    assert d["concurrency"] == config.CONCURRENCY


def test_sampler_feeds_minutes(c):
    deadline = time.time() + 5
    while time.time() < deadline and not any(m.get("busy_avg") is not None for m in stats.minutes_view(2)):
        time.sleep(0.2)
    assert stats.minutes_view(2)


def test_memory_peak_per_minute_and_persisted():
    s = Stats()
    s.sample(0, 0)
    assert s.minutes_view(5)[0]["mem_py"] is None  # not sampled yet
    s.sample(0, 0, mem={"rss": 100, "children_rss": 900, "mem_available": 5000})
    s.sample(0, 0, mem={"rss": 120, "children_rss": 700, "mem_available": 4000})
    s.sample(0, 0, mem={"rss": 110, "children_rss": 800, "mem_available": 4500})
    (m,) = s.minutes_view(5)
    assert (m["mem_py"], m["mem_br"], m["mem_avail"]) == (120, 900, 4000)
    s.save()
    t = Stats()
    t.load()
    (m,) = t.minutes_view(5)
    assert (m["mem_py"], m["mem_br"], m["mem_avail"]) == (120, 900, 4000)


def test_memory_usage_reports_this_process():
    mem = stats_mod.memory_usage()
    assert mem["rss"] > 0 and mem["mem_total"] >= mem["mem_available"] > 0
