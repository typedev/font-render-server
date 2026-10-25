#!/usr/bin/env python3
"""
Batch Font Render — render all font pairs across browsers and ClearType modes.

Automatically matches fonts between two directories (sources vs output) and
renders every combination of browser × ClearType mode via the render server.

Usage:
    uv run font-render-batch /path/to/ttf_sources /path/to/ttf_output --server http://host:41800
    uv run font-render-batch /path/to/ttf_sources /path/to/ttf_output --dry-run
"""

import argparse
import os
import sys
import time
from pathlib import Path

try:
    import requests
except ImportError:
    print("Error: 'requests' package required. Install: pip install requests")
    sys.exit(1)


from ._env import load_env

load_env()

DEFAULT_SERVER = "http://localhost:41800"
DEFAULT_OUTPUT_DIR = "./renders"
ALL_BROWSERS = ["chromium", "firefox", "webkit"]
CLEARTYPE_MODES = [("cleartype", True), ("grayscale", False)]
SUFFIXES = ["_TT", "_T", "T_P"]

# Per-client Bearer token issued by the server admin (uv run font-render-clients add <id>)
_CLIENT_TOKEN = os.environ.get("FONT_RENDER_CLIENT_TOKEN", "")
AUTH_HEADERS = {"Authorization": f"Bearer {_CLIENT_TOKEN}"} if _CLIENT_TOKEN else {}


def find_pairs(dir_a: Path, dir_b: Path) -> list[tuple[Path, Path]]:
    """Match font files from dir_a to dir_b.

    Matching strategy:
    1. Exact filename match
    2. Try suffixes: name_TT.ttf, name_T.ttf, nameT_P.ttf
    """
    files_a = sorted(f for f in dir_a.iterdir() if f.suffix.lower() == ".ttf")
    files_b_by_name = {f.stem: f for f in dir_b.iterdir() if f.suffix.lower() == ".ttf"}

    pairs = []
    unmatched = []

    for fa in files_a:
        stem = fa.stem
        # 1. Exact match
        if stem in files_b_by_name:
            pairs.append((fa, files_b_by_name[stem]))
            continue
        # 2. Try suffixes
        matched = False
        for suffix in SUFFIXES:
            candidate = stem + suffix
            if candidate in files_b_by_name:
                pairs.append((fa, files_b_by_name[candidate]))
                matched = True
                break
        if not matched:
            unmatched.append(fa.name)

    if unmatched:
        print(f"Warning: {len(unmatched)} unmatched font(s) in {dir_a}:")
        for name in unmatched:
            print(f"  - {name}")

    return pairs


def check_server(server_url: str) -> bool:
    try:
        resp = requests.get(f"{server_url}/health", timeout=5)
        if resp.json().get("status") == "ok":
            return True
    except requests.ConnectionError:
        print(f"Cannot connect to {server_url}")
    except Exception as e:
        print(f"Server error: {e}")
    return False


def render_one(
    server: str,
    font_a: Path,
    font_b: Path,
    browser: str,
    cleartype: bool,
    ppem: str,
    width: int,
    output_path: Path,
) -> bool:
    """Send a single render request and save the PNG. Returns True on success."""
    url = f"{server}/render"
    max_retries = 3
    for attempt in range(max_retries):
        try:
            with open(font_a, "rb") as fa, open(font_b, "rb") as fb:
                files = [
                    ("fonts", (font_a.name, fa, "font/ttf")),
                    ("fonts", (font_b.name, fb, "font/ttf")),
                ]
                data = {
                    "ppem": ppem,
                    "cleartype": str(cleartype).lower(),
                    "width": width,
                    "browser": browser,
                }
                resp = requests.post(url, files=files, data=data, headers=AUTH_HEADERS, timeout=120)

            if resp.status_code != 200:
                print(f" ERROR {resp.status_code}: {resp.text[:200]}")
                return False

            output_path.parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, "wb") as f:
                f.write(resp.content)
            return True

        except (requests.ConnectionError, requests.Timeout) as e:
            if attempt < max_retries - 1:
                wait = 5 * (attempt + 1)
                print(f" connection error, retrying in {wait}s...")
                time.sleep(wait)
            else:
                print(f" FAILED after {max_retries} attempts: {e}")
                return False


def make_output_name(font_a: Path, font_b: Path) -> str:
    """Generate output filename from the font pair."""
    return f"{font_a.stem}_vs_{font_b.stem}.png"


def main():
    parser = argparse.ArgumentParser(
        description="Batch render font pairs across browsers and ClearType modes",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s ttf_sources/ ttf_output/ --dry-run
  %(prog)s ttf_sources/ ttf_output/ --server http://studio-pc:41800
  %(prog)s ttf_sources/ ttf_output/ --browsers chromium firefox
  %(prog)s ttf_sources/ ttf_output/ --ppem 12-16
        """,
    )

    parser.add_argument("dir_a", help="Source fonts directory")
    parser.add_argument("dir_b", help="Output fonts directory")
    parser.add_argument(
        "--server", "-s",
        default=os.environ.get("FONT_RENDER_SERVER", DEFAULT_SERVER),
        help=f"Server URL (default: {DEFAULT_SERVER}, or FONT_RENDER_SERVER env var)",
    )
    parser.add_argument(
        "--output-dir", "-o",
        default=DEFAULT_OUTPUT_DIR,
        help=f"Base output directory (default: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument("--ppem", default="9-24", help="PPEM range (default: 9-24)")
    parser.add_argument("--width", type=int, default=1920, help="Viewport width (default: 1920)")
    parser.add_argument(
        "--browsers",
        nargs="+",
        choices=ALL_BROWSERS,
        default=ALL_BROWSERS,
        help="Browsers to render with (default: all three)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Show matched pairs, don't render")

    args = parser.parse_args()

    dir_a = Path(args.dir_a)
    dir_b = Path(args.dir_b)

    if not dir_a.is_dir():
        print(f"Error: directory not found: {dir_a}")
        sys.exit(1)
    if not dir_b.is_dir():
        print(f"Error: directory not found: {dir_b}")
        sys.exit(1)

    # Match font pairs
    pairs = find_pairs(dir_a, dir_b)
    if not pairs:
        print("No matching font pairs found.")
        sys.exit(1)

    # Build job list
    jobs = []
    for font_a, font_b in pairs:
        for browser in args.browsers:
            for mode_name, cleartype in CLEARTYPE_MODES:
                subdir = f"{browser}_{mode_name}"
                filename = make_output_name(font_a, font_b)
                output_path = Path(args.output_dir) / subdir / filename
                jobs.append((font_a, font_b, browser, cleartype, mode_name, output_path))

    total = len(jobs)
    print(f"Matched {len(pairs)} font pair(s), {total} total render(s)")
    print(f"Browsers: {', '.join(args.browsers)}")
    print(f"PPEM: {args.ppem}, width: {args.width}")
    print()

    if args.dry_run:
        print("Font pairs:")
        for font_a, font_b in pairs:
            print(f"  {font_a.name}  <->  {font_b.name}")
        print()
        print("Output directories:")
        seen = set()
        for *_, mode_name, output_path in jobs:
            d = str(output_path.parent)
            if d not in seen:
                seen.add(d)
                print(f"  {d}/")
        return

    # Check server
    if not check_server(args.server):
        print("Make sure the server is running: uv run font-render-server")
        sys.exit(1)
    print()

    # Render
    done = 0
    skipped = 0
    failed = 0
    t_start = time.time()

    for i, (font_a, font_b, browser, cleartype, mode_name, output_path) in enumerate(jobs, 1):
        label = f"{browser}/{mode_name}: {font_a.stem} vs {font_b.stem}"

        if output_path.exists():
            skipped += 1
            print(f"[{i}/{total}] SKIP (exists) {label}")
            continue

        print(f"[{i}/{total}] {label}...", end=" ", flush=True)
        t0 = time.time()
        ok = render_one(
            args.server, font_a, font_b, browser, cleartype,
            args.ppem, args.width, output_path,
        )
        elapsed = time.time() - t0

        if ok:
            done += 1
            size_kb = output_path.stat().st_size / 1024
            print(f"OK ({size_kb:.0f} KB, {elapsed:.1f}s)")
        else:
            failed += 1

    total_time = time.time() - t_start
    print()
    print(f"Done in {total_time:.0f}s: {done} rendered, {skipped} skipped, {failed} failed")


if __name__ == "__main__":
    main()
