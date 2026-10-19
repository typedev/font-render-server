"""Uploaded fonts kept in RAM for POST /fonts + font_ids."""

import hashlib
import time
from collections import OrderedDict


class _CachedFont:
    __slots__ = ("data", "filename", "last_used")

    def __init__(self, data: bytes, filename: str):
        self.data = data
        self.filename = filename
        self.last_used = time.monotonic()


class FontCache:
    """Uploaded fonts in RAM, one namespace per client, LRU by total bytes + idle TTL.

    Nothing is written to disk or logged (no names, hashes or sizes).
    """

    def __init__(self, max_bytes: int, ttl_s: float):
        self.max_bytes = max_bytes
        self.ttl_s = ttl_s
        self.entries: OrderedDict[tuple[str, str], _CachedFont] = OrderedDict()
        self.bytes = 0

    def put(self, namespace: str, data: bytes, filename: str) -> str:
        sha = hashlib.sha256(data).hexdigest()
        self._drop((namespace, sha))
        self.entries[(namespace, sha)] = _CachedFont(data, filename)
        self.bytes += len(data)
        while self.bytes > self.max_bytes and len(self.entries) > 1:
            _, evicted = self.entries.popitem(last=False)
            self.bytes -= len(evicted.data)
        return sha

    def get(self, namespace: str, sha: str) -> _CachedFont | None:
        key = (namespace, sha)
        entry = self.entries.get(key)
        if entry is not None:
            if time.monotonic() - entry.last_used > self.ttl_s:
                self._drop(key)
                return None
            entry.last_used = time.monotonic()
            self.entries.move_to_end(key)
        return entry

    def _drop(self, key):
        entry = self.entries.pop(key, None)
        if entry is not None:
            self.bytes -= len(entry.data)

    def drop_namespace(self, namespace: str) -> int:
        keys = [k for k in self.entries if k[0] == namespace]
        for k in keys:
            self._drop(k)
        return len(keys)

    def sweep(self, now: float):
        for key in [k for k, e in self.entries.items() if now - e.last_used > self.ttl_s]:
            self._drop(key)

    def namespace_stats(self, namespace: str) -> dict:
        sizes = [len(e.data) for (ns, _), e in self.entries.items() if ns == namespace]
        return {"fonts": len(sizes), "bytes": sum(sizes)}

    def stats(self) -> dict:
        """Totals and per-namespace (client id) counts — no names or hashes."""
        per: dict[str, dict] = {}
        for (ns, _), e in self.entries.items():
            d = per.setdefault(ns, {"fonts": 0, "bytes": 0})
            d["fonts"] += 1
            d["bytes"] += len(e.data)
        return {"fonts": len(self.entries), "bytes": self.bytes, "namespaces": per}

    def clear(self) -> int:
        n = len(self.entries)
        self.entries.clear()
        self.bytes = 0
        return n

    def resize(self, max_bytes: int):
        """New size limit; evicts least recently used fonts above it."""
        self.max_bytes = max_bytes
        while self.bytes > self.max_bytes and self.entries:
            _, evicted = self.entries.popitem(last=False)
            self.bytes -= len(evicted.data)
