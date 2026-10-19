"""
Pixel-identity check for POST /render and POST /glyphs (native engines the target offers).

Renders a fixed parameter matrix and compares the PNG bytes (and geometry
JSON) against a previously captured baseline. Used to verify that server
changes don't alter rendering output.

Usage:
    # capture a baseline from the current code (in-process, local Chromium)
    uv run pixel-check capture --fonts renders/pixel_fonts/*
    # ...change the server code...
    uv run pixel-check compare --fonts renders/pixel_fonts/*

    # same against a running server (e.g. the Windows host)
    uv run pixel-check capture --server http://SERVER:8000 --fonts ...
    uv run pixel-check compare --server http://SERVER:8000 --fonts ...

Baselines are stored as sha256 digests in renders/pixel_baseline/<target>.json
(platform-specific, not committed). Fonts are not part of the repo.
"""

import argparse
import base64
import hashlib
import itertools
import json
import os
import sys
from pathlib import Path

from font_render_client._env import load_env

# Repository root: src/font_render_tools/pixel_check.py -> parents[2]
PROJECT_ROOT = Path(__file__).resolve().parents[2]

BASELINE_DIR = PROJECT_ROOT / "renders" / "pixel_baseline"

TEXT = "Hamburgefontsiv ÀÉÎõü 0123"
PPEM = "9,11,13,16,20"
# /glyphs parameter sets per native engine (case id suffix -> form fields)
GLYPH_MODES = {
    "gdi": {m: {"mode": m} for m in ("bw", "gray", "cleartype", "cleartype_natural")},
    "dwrite": {
        **{m: {"rendering_mode": m} for m in
           ("default", "aliased", "gdi_classic", "gdi_natural", "natural", "natural_symmetric")},
        "gdi_classic+gdi_measure": {"rendering_mode": "gdi_classic", "measuring_mode": "gdi_classic"},
        "natural_symmetric+gray": {"rendering_mode": "natural_symmetric", "antialias": "grayscale"},
        "natural_symmetric+bgr+g2.2": {"pixel_geometry": "bgr", "gamma": "2.2", "contrast": "1"},
    },
}


def _cases(fonts: list[Path], engines: list[str]):
    """Yield (case_id, font_paths, form, path) for the whole matrix."""
    for case_id, fset, form in _render_cases(fonts):
        yield case_id, fset, form, "/render"
    # Variable fonts: several locations of one font in one /render, and per location in dwrite
    for font in (f for f in fonts if _is_variable(f)):
        tags = _axis_tags(font)
        locs = [",".join(p for p in loc.split(",") if p.split("=")[0] in tags) for loc in VF_LOCATIONS]
        spec = ";".join(locs)
        yield f"vf|{font.name}|render|plain|geo=1", [font] * len(VF_LOCATIONS), {
            "ppem": PPEM, "text": TEXT, "layout": "plain", "geometry": "true",
            "variations": spec}, "/render"
        if "dwrite" in engines:
            for loc in locs:
                yield f"vf|{font.name}|dwrite|{loc}", [font], {
                    "engine": "dwrite", "axes": loc, "gids": "0-60", "ppem": PPEM,
                    "geometry": "true"}, "/glyphs"
    for font, engine in itertools.product(fonts, engines):
        modes = GLYPH_MODES.get(engine, {})
        for mode, layout in itertools.product(modes, ("cells", "run")):
            yield f"glyphs|{font.name}|{engine}|{mode}|{layout}", [font], {
                "engine": engine, **modes[mode], "layout": layout, "gids": "0-60", "ppem": PPEM,
                "geometry": "true"}, "/glyphs"


VF_LOCATIONS = ("", "wght=350.5,wdth=87.25", "wght=800,wdth=75")


def _axis_tags(font: Path) -> set[str]:
    from fontTools.ttLib import TTFont
    try:
        f = TTFont(font, lazy=True)
        return {a.axisTag for a in f["fvar"].axes} if "fvar" in f else set()
    except Exception:
        return set()


def _is_variable(font: Path) -> bool:
    return bool(_axis_tags(font))


def _render_cases(fonts: list[Path]):
    """Yield (case_id, font_paths, form) of /render."""
    fonts = [f for f in fonts if not _is_variable(f)]  # VF cases are separate (case ids stay)
    font_sets = [[f] for f in fonts] + [fonts]
    for fset, ct, scale, layout, geometry in itertools.product(
        font_sets, (True, False), (1.0, 1.5), ("plain", "full"), (False, True)
    ):
        names = "+".join(f.name for f in fset)
        case_id = f"{names}|ct={int(ct)}|s={scale}|{layout}|geo={int(geometry)}"
        form = {
            "ppem": PPEM,
            "text": TEXT,
            "cleartype": str(ct).lower(),
            "scale": str(scale),
            "layout": layout,
            "geometry": str(geometry).lower(),
        }
        yield case_id, fset, form
    # Reverse video (white on black); keeps the cases above unchanged for old baselines
    for fset, ct, layout in itertools.product([fonts], (True, False), ("plain", "full")):
        names = "+".join(f.name for f in fset)
        yield f"{names}|ct={int(ct)}|s=1.0|{layout}|geo=0|bg=000", fset, {
            "ppem": PPEM,
            "text": TEXT,
            "cleartype": str(ct).lower(),
            "layout": layout,
            "geometry": "false",
            "background": "#000000",
        }


def _digest(content: bytes, geometry: bool) -> str:
    if geometry:
        payload = json.loads(content)
        png = base64.b64decode(payload["image"])
        geo = json.dumps(payload["geometry"], sort_keys=True).encode()
        return hashlib.sha256(png).hexdigest()[:16] + ":" + hashlib.sha256(geo).hexdigest()[:16]
    return hashlib.sha256(content).hexdigest()[:16]


class _Target:
    def __init__(self, server: str | None):
        self.server = server
        if server:
            import requests
            self._session = requests.Session()
            token = os.environ.get("FONT_RENDER_CLIENT_TOKEN", "")
            if token:
                self._session.headers["Authorization"] = f"Bearer {token}"
        else:
            from starlette.testclient import TestClient
            from font_render.app import app
            self._client = TestClient(app)
            self._client.__enter__()

    def engines(self) -> list[str]:
        """Native engines of the target (/glyphs), from /info."""
        if self.server:
            info = self._session.get(f"{self.server}/info", timeout=30).json()
        else:
            info = self._client.get("/info").json()
        return [e for e in info.get("engines", []) if e != "chromium"]

    def render(self, fonts: list[Path], form: dict, path: str = "/render") -> bytes:
        files = [("fonts", (f.name, f.read_bytes())) for f in fonts]
        if self.server:
            r = self._session.post(f"{self.server}{path}", files=files, data=form, timeout=120)
        else:
            r = self._client.post(path, files=files, data=form)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
        return r.content

    def close(self):
        if not self.server:
            self._client.__exit__(None, None, None)


def main():
    parser = argparse.ArgumentParser(description="Pixel-identity check for /render and /glyphs")
    parser.add_argument("mode", choices=("capture", "compare"))
    parser.add_argument("--fonts", nargs="+", required=True, type=Path)
    parser.add_argument("--server", help="Server URL (default: in-process local render)")
    parser.add_argument("--name", help="Baseline name (default: derived from target)")
    args = parser.parse_args()

    if args.server:
        load_env()
    name = args.name or ("local" if not args.server else args.server.split("//")[-1].replace(":", "_"))
    baseline_file = BASELINE_DIR / f"{name}.json"

    target = _Target(args.server)
    results = {}
    try:
        for case_id, fset, form, path in _cases(sorted(args.fonts), target.engines()):
            results[case_id] = _digest(target.render(fset, form, path), form["geometry"] == "true")
    finally:
        target.close()

    if args.mode == "capture":
        BASELINE_DIR.mkdir(parents=True, exist_ok=True)
        baseline_file.write_text(json.dumps(results, indent=1, sort_keys=True))
        print(f"Captured {len(results)} cases -> {baseline_file}")
        return

    if not baseline_file.is_file():
        sys.exit(f"No baseline at {baseline_file}; run 'capture' first")
    baseline = json.loads(baseline_file.read_text())
    diffs = [c for c in baseline if results.get(c) != baseline[c]]
    missing = [c for c in results if c not in baseline]
    for c in diffs:
        print(f"DIFF  {c}: {baseline[c]} -> {results.get(c)}")
    for c in missing:
        print(f"NEW   {c} (not in baseline)")
    print(f"{len(baseline) - len(diffs)}/{len(baseline)} identical")
    sys.exit(1 if diffs else 0)


if __name__ == "__main__":
    main()
