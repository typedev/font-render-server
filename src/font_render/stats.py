"""Request statistics for the web admin: recent requests, per-client traffic,
per-minute load and overload episodes.

Only what the access log already records — time, peer IP, client id, method,
path, status, duration — plus byte counts. Never font names, hashes or contents.
Traffic, minutes and episodes are saved to config.STATS_FILE (data/stats.json)
so they survive restarts; recent requests and session activity are in memory.
"""

import asyncio
import json
import time
from collections import OrderedDict, deque

from . import config

MINUTES_KEEP = 7 * 24 * 60
DAYS_KEEP = 400
EPISODES_KEEP = 500
EPISODE_GAP_S = 60  # overload events closer than this belong to one episode
CRITICAL = ("queue_full", "timeout", "error")


def _pct(sorted_ms: list[float], q: float) -> float:
    return sorted_ms[min(len(sorted_ms) - 1, int(q * len(sorted_ms)))]


def _new_minute() -> dict:
    return {"req": 0, "renders": 0, "err": 0, "rejected": 0, "client_limit": 0, "timeouts": 0,
            "in": 0, "out": 0, "busy_max": 0, "queue_max": 0, "wait_max": 0.0,
            "busy_sum": 0, "samples": 0, "cpu_sum": 0.0, "cpu_n": 0, "lat": [],
            # bytes: peak of the server process and of its children (Playwright driver +
            # browsers), lowest free memory of the machine; None = not sampled
            "mem_py": None, "mem_br": None, "mem_avail": None}


def _finish_minute(m: dict) -> dict:
    """Closed minute: render latencies -> p50/p95/max, busy samples -> average."""
    lat = sorted(m.pop("lat", []))
    if lat:
        m["p50"], m["p95"], m["lat_max"] = _pct(lat, 0.5), _pct(lat, 0.95), lat[-1]
    m["busy_avg"] = round(m.pop("busy_sum", 0) / m["samples"], 2) if m.get("samples") else 0
    cpu_n = m.pop("cpu_n", 0)
    m["cpu"] = round(m.pop("cpu_sum", 0.0) / cpu_n, 1) if cpu_n else None
    m.pop("samples", None)
    return m


class Stats:
    def __init__(self, recent: int = 500):
        self.started = time.time()
        self.recent: deque[dict] = deque(maxlen=recent)
        self.clients: dict[str, dict] = {}  # activity since restart
        # Persistent
        self.traffic: dict[str, dict] = {}  # client -> {"total": {...}, "days": {date: {...}}}
        self.minutes: OrderedDict[int, dict] = OrderedDict()  # epoch minute -> bucket
        self.episodes: deque[dict] = deque(maxlen=EPISODES_KEEP)
        self.episode: dict | None = None  # the open one

    # ------------------------------------------------------------- recording
    def _minute(self, now: float) -> dict:
        key = int(now // 60)
        m = self.minutes.get(key)
        if m is None:
            # Close earlier open minutes
            for k in reversed(self.minutes):
                if "lat" not in self.minutes[k]:
                    break
                _finish_minute(self.minutes[k])
            m = self.minutes[key] = _new_minute()
            while len(self.minutes) > MINUTES_KEEP or (self.minutes and next(iter(self.minutes)) < key - MINUTES_KEEP):
                self.minutes.popitem(last=False)
        elif "lat" not in m:
            # Saved and loaded within the same minute (restart): reopen it
            m.update(lat=[], busy_sum=0, samples=0, cpu_sum=0.0, cpu_n=0)
        return m

    def record(self, ip: str, client: str | None, method: str, path: str, status: int, ms: float,
               bytes_in: int = 0, bytes_out: int = 0, info: dict | None = None):
        info = info or {}
        now = time.time()
        cid = client or "anonymous"
        noise = path in ("/health", "/favicon.ico") or (
            (path == "/admin" or path.startswith("/admin/")) and method in ("GET", "HEAD"))
        render_ok = path in ("/render", "/glyphs") and status == 200

        # Traffic counts everything a client sent or received, admin pages included
        if client or not noise:
            t = self.traffic.setdefault(cid, {"total": {}, "days": {}})
            day = time.strftime("%Y-%m-%d", time.localtime(now))
            for bucket in (t["total"], t["days"].setdefault(day, {})):
                bucket["in"] = bucket.get("in", 0) + bytes_in
                bucket["out"] = bucket.get("out", 0) + bytes_out
                bucket["req"] = bucket.get("req", 0) + 1
                bucket["renders"] = bucket.get("renders", 0) + render_ok
            if len(t["days"]) > DAYS_KEEP:
                for d in sorted(t["days"])[:-DAYS_KEEP]:
                    del t["days"][d]
        if noise:
            return

        self.recent.append({"t": now, "ip": ip, "client": cid, "method": method, "path": path,
                            "status": status, "ms": round(ms, 1), "in": bytes_in, "out": bytes_out})
        c = self.clients.setdefault(cid, {"requests": 0, "errors": 0, "renders": 0,
                                          "last_seen": 0.0, "last_ip": ""})
        c["requests"] += 1
        c["errors"] += status >= 400 or status == 0
        c["renders"] += render_ok
        c["last_seen"] = now
        c["last_ip"] = ip

        m = self._minute(now)
        m["req"] += 1
        m["in"] += bytes_in
        m["out"] += bytes_out
        m["err"] += status >= 400 or status == 0
        wait_s = info.get("queue_s", 0.0)
        m["wait_max"] = max(m["wait_max"], round(wait_s * 1000))
        if render_ok:
            m["renders"] += 1
            m["lat"].append(round(ms, 1))
        busy = info.get("busy")
        if busy == "client_limit":
            m["client_limit"] += 1
        elif busy == "queue_full":
            m["rejected"] += 1
            self._event(now, "queue_full", cid)
        if status == 504:
            m["timeouts"] += 1
            self._event(now, "timeout", cid)
        elif status >= 500 or status == 0:
            self._event(now, "error", cid)
        if wait_s >= config.OVERLOAD_WAIT_S:
            self._event(now, "slow_queue", cid, wait_ms=wait_s * 1000)

    def sample(self, busy: int, queued: int, cpu: float | None = None, mem: dict | None = None):
        """Called once a second with the pool state (CPU and memory every few seconds)."""
        now = time.time()
        m = self._minute(now)
        m["busy_max"] = max(m["busy_max"], busy)
        m["queue_max"] = max(m["queue_max"], queued)
        m["busy_sum"] += busy
        m["samples"] += 1
        if cpu is not None:
            m["cpu_sum"] += cpu
            m["cpu_n"] += 1
        if mem is not None:
            for key, value, pick in (("mem_py", mem["rss"], max), ("mem_br", mem["children_rss"], max),
                                     ("mem_avail", mem["mem_available"], min)):
                m[key] = value if m.get(key) is None else pick(m[key], value)
        if self.episode is not None:
            ep = self.episode
            ep["max_queue"] = max(ep["max_queue"], queued)
            ep["max_busy"] = max(ep["max_busy"], busy)
            if now - ep["end"] > EPISODE_GAP_S:
                self._close_episode()

    # ------------------------------------------------------------- episodes
    def _event(self, now: float, kind: str, client: str, wait_ms: float = 0.0):
        ep = self.episode
        if ep is not None and now - ep["end"] > EPISODE_GAP_S:
            self._close_episode()
            ep = None
        if ep is None:
            ep = self.episode = {"start": now, "end": now, "counts": {}, "clients": {},
                                 "max_queue": 0, "max_busy": 0, "max_wait_ms": 0, "severity": "warning"}
            config.log.warning("overload episode started: %s", kind)
        ep["end"] = now
        ep["counts"][kind] = ep["counts"].get(kind, 0) + 1
        ep["clients"][client] = ep["clients"].get(client, 0) + 1
        ep["max_wait_ms"] = max(ep["max_wait_ms"], round(wait_ms))
        if kind in CRITICAL:
            ep["severity"] = "critical"

    def _close_episode(self):
        ep, self.episode = self.episode, None
        if ep is not None:
            self.episodes.append(ep)
            config.log.warning("overload episode ended: %.0fs, %s", ep["end"] - ep["start"], ep["counts"])

    def episodes_view(self) -> list[dict]:
        """Newest first; the open episode (if any) marked ongoing."""
        out = [{**e, "ongoing": False} for e in self.episodes]
        if self.episode is not None:
            out.append({**self.episode, "ongoing": True})
        return out[::-1]

    # ------------------------------------------------------------- views
    def render_latency(self) -> dict:
        """p50/p95 of successful renders in the recent window (ms)."""
        ms = sorted(r["ms"] for r in self.recent if r["path"] == "/render" and r["status"] == 200)
        if not ms:
            return {"count": 0}
        return {"count": len(ms), "p50": _pct(ms, 0.5), "p95": _pct(ms, 0.95), "max": ms[-1]}

    def minutes_view(self, minutes: int) -> list[dict]:
        """Closed and current minutes of the last `minutes`, oldest first (current one estimated)."""
        since = int(time.time() // 60) - minutes
        out = []
        for k, m in self.minutes.items():
            if k <= since:
                continue
            if "lat" in m:  # still open: summarize a copy
                m = _finish_minute({**m, "lat": list(m["lat"])})
            out.append({"t": k * 60, **m})
        return out

    def traffic_view(self) -> dict:
        today = time.strftime("%Y-%m-%d")
        week = {time.strftime("%Y-%m-%d", time.localtime(time.time() - d * 86400)) for d in range(7)}
        month = {time.strftime("%Y-%m-%d", time.localtime(time.time() - d * 86400)) for d in range(30)}

        def total(days: dict, keys) -> dict:
            out = {"in": 0, "out": 0, "req": 0, "renders": 0}
            for d in keys:
                for k in out:
                    out[k] += days.get(d, {}).get(k, 0)
            return out

        return {
            client: {
                "today": total(t["days"], [today]),
                "week": total(t["days"], week),
                "month": total(t["days"], month),
                "total": {"in": 0, "out": 0, "req": 0, "renders": 0, **t["total"]},
                "days": t["days"],
            }
            for client, t in self.traffic.items()
        }

    # ------------------------------------------------------------- persistence
    def save(self):
        path = config.STATS_FILE
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            minutes = {}
            for k, m in self.minutes.items():
                m = _finish_minute({**m, "lat": list(m["lat"])}) if "lat" in m else m
                minutes[str(k)] = {f: v for f, v in m.items() if v}  # idle minutes stay tiny
            data = {"version": 1, "saved": time.time(), "traffic": self.traffic, "minutes": minutes,
                    "episodes": list(self.episodes) + ([self.episode] if self.episode else [])}
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
            tmp.replace(path)
        except OSError as e:
            config.log.error("stats not saved: %s", e)

    def load(self):
        path = config.STATS_FILE
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            config.log.error("stats file unreadable, starting empty: %s", e)
            return
        self.traffic = data.get("traffic", {})
        closed = {k: v for k, v in _new_minute().items()
                  if k not in ("lat", "busy_sum", "samples", "cpu_sum", "cpu_n")}
        closed.update(busy_avg=0, cpu=None)  # mem_* default to None (not sampled)
        # All loaded minutes are closed; zero fields were dropped on save
        self.minutes = OrderedDict(sorted((int(k), {**closed, **v}) for k, v in data.get("minutes", {}).items()))
        self.episodes = deque(data.get("episodes", []), maxlen=EPISODES_KEEP)

    def reset(self):
        """Fresh state (tests)."""
        self.__init__(self.recent.maxlen)


stats = Stats()


def memory_usage() -> dict | None:
    """RSS of this process and of its children (Playwright driver + browsers), machine memory."""
    try:
        import psutil
    except ImportError:
        return None
    proc = psutil.Process()
    children, children_rss = 0, 0
    for child in proc.children(recursive=True):
        try:
            children_rss += child.memory_info().rss
            children += 1
        except psutil.Error:
            pass
    vm = psutil.virtual_memory()
    return {"pid": proc.pid, "rss": proc.memory_info().rss, "children": children,
            "children_rss": children_rss, "mem_total": vm.total, "mem_available": vm.available}


async def sampler(pool, interval: float = 1.0):
    """Feed stats.sample() from the browser pool every second; save every minute."""
    try:
        import psutil
        psutil.cpu_percent(interval=None)  # first call primes the counter
    except ImportError:
        psutil = None
    n = 0
    while True:
        await asyncio.sleep(interval)
        n += 1
        cpu = psutil.cpu_percent(interval=None) if psutil is not None and n % 5 == 0 else None
        mem = memory_usage() if psutil is not None and n % 15 == 1 else None
        stats.sample(pool.busy, pool.waiting, cpu, mem)
        if n % 60 == 0:
            stats.save()

