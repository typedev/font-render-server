#!/usr/bin/env python3
"""
Font Render Client — send TTF fonts to the render service and get PNG screenshots.

Usage:
    # Single font waterfall
    uv run font-render font.ttf

    # Two-font comparison
    uv run font-render font_a.ttf font_b.ttf

    # Multiple fonts
    uv run font-render a.ttf b.ttf c.ttf d.ttf --ppem 9-40

    # Custom settings
    uv run font-render font_a.ttf font_b.ttf --ppem 9-20 --text "Hello World"

    # Custom server address
    uv run font-render font_a.ttf font_b.ttf --server http://192.168.1.100:8000

    # Open result immediately in image viewer
    uv run font-render font_a.ttf font_b.ttf --open

    # Plain layout (no header/labels) — clean output for diff analysis
    uv run font-render font.ttf --plain

    # Nearest-neighbor zoom (each pixel becomes 4x4 block)
    uv run font-render font.ttf --plain --zoom 4

    # Reverse video: white text on black
    uv run font-render font.ttf --invert
"""

import argparse
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    import requests
except ImportError:
    print("Error: 'requests' package required. Install: pip install requests")
    sys.exit(1)


from ._env import load_env

load_env()

DEFAULT_SERVER = "http://localhost:8000"
DEFAULT_OUTPUT_DIR = "./renders"

# Per-client Bearer token issued by the server admin (uv run font-render-clients add <id>)
_CLIENT_TOKEN = os.environ.get("FONT_RENDER_CLIENT_TOKEN", "")
AUTH_HEADERS = {"Authorization": f"Bearer {_CLIENT_TOKEN}"} if _CLIENT_TOKEN else {}
# Admin-scope client token for --update/--rollback/--install-browser; the legacy
# FONT_RENDER_TOKEN form field (--token) is used only when this is not set
_ADMIN_TOKEN = os.environ.get("FONT_RENDER_ADMIN_TOKEN", "")
ADMIN_HEADERS = {"Authorization": f"Bearer {_ADMIN_TOKEN}"} if _ADMIN_TOKEN else {}
# Developer-mode client token for --deploy / --release (docs/distribution.md#developer-mode)
_DEV_TOKEN = os.environ.get("FONT_RENDER_DEV_TOKEN", "")
DEV_HEADERS = {"Authorization": f"Bearer {_DEV_TOKEN}"} if _DEV_TOKEN else {}


def _admin_form(token: str, **fields) -> dict:
    return {**fields, "token": token} if token and not ADMIN_HEADERS else fields


def _error_text(resp) -> str:
    try:
        result = resp.json()
    except ValueError:
        return f"HTTP {resp.status_code}: {resp.text[:200]}"
    msg = result.get("error", result.get("stderr", "Unknown error"))
    if resp.status_code in (401, 403):
        msg += (" — set FONT_RENDER_ADMIN_TOKEN (admin-scope client token) in .env; "
                "the legacy FONT_RENDER_TOKEN works only while legacy_admin_token is on")
    return msg


def render_fonts(args) -> Path:
    """Send fonts to the server and get a PNG screenshot."""
    url = f"{args.server}/render"

    # Compute short labels: strip common path prefix so files from
    # different directories get distinguishable names (e.g. v1/Regular.ttf)
    abs_paths = [os.path.abspath(f) for f in args.fonts]
    if len(abs_paths) > 1:
        common = os.path.commonpath([os.path.dirname(p) for p in abs_paths])
        labels = [os.path.relpath(p, common) for p in abs_paths]
    else:
        labels = [os.path.basename(abs_paths[0])]

    # Build multipart files list: all fonts under the "fonts" key
    _mime = {".ttf": "font/ttf", ".otf": "font/otf", ".woff": "font/woff", ".woff2": "font/woff2"}
    files = [
        ("fonts", (label, open(f, "rb"), _mime.get(Path(f).suffix.lower(), "font/ttf")))
        for label, f in zip(labels, args.fonts)
    ]
    data = {
        "ppem": args.ppem,
        "text": args.text,
        "scale": args.scale,
        "cleartype": str(args.cleartype).lower(),
        "width": args.width,
        "colors": args.colors,
        "background": "#000000" if args.invert else args.bg,
        "browser": args.browser,
        "layout": "plain" if args.plain else "full",
        "zoom": args.zoom,
    }

    n = len(args.fonts)
    print(f"Sending {n} font{'s' if n != 1 else ''} to {url} (browser: {args.browser})...")
    t0 = time.time()
    resp = requests.post(url, files=files, data=data, headers=AUTH_HEADERS, timeout=300)
    elapsed = time.time() - t0

    if resp.status_code != 200:
        print(f"Error {resp.status_code}: {resp.text}")
        sys.exit(1)

    # Build output filename using short labels
    os.makedirs(args.output_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    names = [str(Path(l).with_suffix("")).replace(os.sep, "_") for l in labels]

    if len(names) == 1:
        filename = f"{timestamp}_{names[0]}_{args.browser}.png"
    elif len(names) == 2:
        filename = f"{timestamp}_{names[0]}_vs_{names[1]}_{args.browser}.png"
    else:
        filename = f"{timestamp}_compare_{len(names)}_fonts_{args.browser}.png"

    output_path = Path(args.output_dir) / filename

    with open(output_path, "wb") as f:
        f.write(resp.content)

    print(f"Saved: {output_path} ({len(resp.content) / 1024:.1f} KB, {elapsed:.1f}s)")
    return output_path


def open_image(path: Path):
    """Open image with system default viewer."""
    system = sys.platform
    try:
        if system == "linux":
            subprocess.Popen(["xdg-open", str(path)])
        elif system == "darwin":
            subprocess.Popen(["open", str(path)])
        elif system == "win32":
            os.startfile(str(path))
    except Exception as e:
        print(f"Could not open image: {e}")


def _poll_health(server_url: str, timeout: int = 30) -> bool:
    """Wait for the server to come back up after restart."""
    deadline = time.time() + timeout
    # Give the server a moment to shut down
    time.sleep(2)
    while time.time() < deadline:
        try:
            resp = requests.get(f"{server_url}/health", timeout=3)
            if resp.status_code == 200:
                return True
        except (requests.ConnectionError, requests.Timeout):
            pass
        time.sleep(1)
    return False


def update_server(server_url: str, token: str):
    """Send update command to the server."""
    print(f"Requesting update from {server_url}...")
    try:
        resp = requests.post(
            f"{server_url}/update",
            data=_admin_form(token), headers=ADMIN_HEADERS,
            timeout=30,
        )
    except requests.ConnectionError:
        print(f"Cannot connect to {server_url}")
        sys.exit(1)

    if resp.status_code not in (200, 202):
        print(f"Error: {_error_text(resp)}")
        result = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        if "details" in result:
            print(f"  {result['details']}")
        if "rolled_back_to" in result:
            print(f"  Rolled back to: {result['rolled_back_to']}")
        sys.exit(1)

    result = resp.json()
    if result.get("status") == "up-to-date":
        print(f"Already up to date (commit: {result.get('commit', '?')})")
        return
    if result.get("status") == "started":  # installed server: a background job (minutes)
        _follow_release_update(server_url, result)
        return

    print(f"Updated: {result.get('old_commit', '?')} → {result.get('new_commit', '?')}")

    if result.get("restarting"):
        print("Server is restarting...")
        if _poll_health(server_url):
            health = requests.get(f"{server_url}/health", timeout=5).json()
            print(f"Server is back (version: {health.get('version', '?')})")
        else:
            print("Warning: server did not come back within 30s")
            sys.exit(1)


def _follow_release_update(server_url: str, result: dict, timeout: int = 1200,
                           status_path: str = "/admin/api/update-status", headers: dict | None = None):
    """Print the job's steps until it restarts the server, then wait for the new version."""
    version = result["new_version"]
    print(f"Updating {result.get('old_version', '?')} → {version} (download, verify, install)...")
    seen, deadline = 0, time.time() + timeout
    while time.time() < deadline:
        time.sleep(2)
        try:
            job = requests.get(f"{server_url}{status_path}", headers=headers or ADMIN_HEADERS, timeout=10).json()
            job = job.get("job", job)  # /dev/status wraps it
        except (requests.ConnectionError, requests.Timeout, ValueError):
            break  # restarting already
        for line in job.get("log", [])[seen:]:
            print(f"  {line}")
        seen = len(job.get("log", []))
        if job.get("state") == "failed":
            print(f"Update failed: {job.get('error')} — the server keeps running {result.get('old_version')}")
            sys.exit(1)
        if job.get("state") == "restarting":
            break
    print("Server is restarting...")
    while time.time() < deadline:
        time.sleep(2)
        try:
            health = requests.get(f"{server_url}/health", timeout=3).json()
        except (requests.ConnectionError, requests.Timeout, ValueError):
            continue
        if health.get("release") == version or health.get("version") == version:
            print(f"Server is back (version: {version})")
            return
    print(f"Warning: the server did not come back with {version} in time; "
          "it reverts to the previous version by itself after 3 failed starts")
    sys.exit(1)


def _dev_post(server_url: str, path: str, **kwargs):
    try:
        resp = requests.post(f"{server_url}{path}", headers=DEV_HEADERS, timeout=300, **kwargs)
    except requests.ConnectionError:
        print(f"Cannot connect to {server_url}")
        sys.exit(1)
    if resp.status_code == 404:
        print("Developer mode is off on this server (or this address is not allowed): "
              "an administrator switches it on with `font-render-server dev-mode on` on the server")
        sys.exit(1)
    if resp.status_code not in (200, 202):
        print(f"Error: {_error_text(resp)}")
        sys.exit(1)
    return resp.json()


def deploy_server(server_url: str, ref: str | None, local: bool):
    """Developer mode: upload a git ref or the working tree, follow the job."""
    from .deploy import DeployError, pack_ref, pack_worktree
    try:
        if local:
            data, commit, dirty, count = pack_worktree()
            what = f"working tree at {commit[:7]}" + (" with uncommitted changes" if dirty else "")
        else:
            data, commit = pack_ref(ref or "origin/main")
            dirty, count, what = False, None, f"{ref or 'origin/main'} = {commit[:7]}"
    except DeployError as e:
        print(f"Error: {e}")
        sys.exit(1)
    print(f"Deploying {what}: {len(data) / 1024:.0f} KB" + (f", {count} files" if count else ""))
    result = _dev_post(server_url, "/dev/deploy", files={"archive": ("tree.zip", data)},
                       data={"commit": commit, "dirty": "true" if dirty else "false"})
    _follow_release_update(server_url, result, status_path="/dev/status", headers=DEV_HEADERS)


def release_server(server_url: str):
    """Developer mode: back to a release (on disk, or the newest of the channel)."""
    result = _dev_post(server_url, "/dev/release")
    if result.get("status") == "switched":
        print(f"Switching to {result['version']}...")
        if _poll_health(server_url, timeout=120):
            print(f"Server is back (version: {result['version']})")
        return
    _follow_release_update(server_url, result, status_path="/dev/status", headers=DEV_HEADERS)


def rollback_server(server_url: str, token: str):
    """Send rollback command to the server."""
    print(f"Requesting rollback from {server_url}...")
    try:
        resp = requests.post(
            f"{server_url}/rollback",
            data=_admin_form(token), headers=ADMIN_HEADERS,
            timeout=30,
        )
    except requests.ConnectionError:
        print(f"Cannot connect to {server_url}")
        sys.exit(1)

    if resp.status_code != 200:
        print(f"Error: {_error_text(resp)}")
        sys.exit(1)

    result = resp.json()
    print(f"Rolled back to: {result.get('version') or result.get('commit', '?')}")

    if result.get("restarting"):
        print("Server is restarting...")
        if _poll_health(server_url):
            health = requests.get(f"{server_url}/health", timeout=5).json()
            print(f"Server is back (version: {health.get('version', '?')})")
        else:
            print("Warning: server did not come back within 30s")
            sys.exit(1)


def server_info(server_url: str):
    """Fetch and display full server information."""
    try:
        resp = requests.get(f"{server_url}/info", headers=AUTH_HEADERS, timeout=10)
    except requests.ConnectionError:
        print(f"Cannot connect to {server_url}")
        sys.exit(1)

    if resp.status_code != 200:
        print(f"Error {resp.status_code}: {resp.text}")
        sys.exit(1)

    d = resp.json()
    print(f"  Service:  {d.get('service', '?')}")
    print(f"  Version:  {d.get('version', '?')} ({d.get('branch', '?')})")
    print(f"  Commit:   {d.get('commit_date', '?')}")
    print(f"  Platform: {d.get('platform', '?')}")
    print(f"  Python:   {d.get('python', '?')}")
    auth = d.get("auth")
    if auth is not None:
        who = auth.get("client") or "anonymous"
        if not auth.get("client") and AUTH_HEADERS:
            who += " (token not recognized)"
        print(f"  Client:   {who}{' (token required)' if auth.get('required') else ''}")

    browsers = d.get("browsers", {})
    installed = browsers.get("installed", {})
    active = browsers.get("active", {})
    for name in browsers.get("supported", []):
        # active keys look like "chromium(ct=True)"
        is_active = any(k.split("(")[0] == name and ok for k, ok in active.items())
        status = "active" if is_active else ("installed" if installed.get(name) else "not installed")
        print(f"  Browser:  {name} — {status}")

    render = d.get("render", {})
    print(f"  Max fonts: {render.get('max_fonts', '?')}")
    print(f"  PPEM:     {render.get('default_ppem', '?')}")
    print(f"  Width:    {render.get('default_width', '?')}")
    print(f"  Scale:    {render.get('default_scale', '?')}")


def install_browser(server_url: str, browser_name: str, token: str):
    """Install a Playwright browser on the remote server."""
    print(f"Installing {browser_name} on {server_url}...")
    try:
        resp = requests.post(
            f"{server_url}/browsers/install",
            data=_admin_form(token, browser=browser_name), headers=ADMIN_HEADERS,
            timeout=120,
        )
    except requests.ConnectionError:
        print(f"Cannot connect to {server_url}")
        sys.exit(1)

    if resp.status_code != 200:
        print(f"Error: {_error_text(resp)}")
        sys.exit(1)
    result = resp.json()

    print(f"Installed: {result.get('browser', '?')}")
    if result.get("stdout"):
        print(result["stdout"])


def check_server(server_url: str) -> bool:
    """Check if the render server is reachable."""
    try:
        resp = requests.get(f"{server_url}/health", timeout=5)
        data = resp.json()
        if data.get("status") == "ok":
            print(f"Server OK (version: {data.get('version', '?')})")
            return True
    except requests.ConnectionError:
        print(f"Cannot connect to {server_url}")
        print("Make sure the server is running:")
        print("  On Windows: uv run font-render-server (or the FontRenderService service)")
        return False
    except Exception as e:
        print(f"Server error: {e}")
        return False


def main():
    if len(sys.argv) > 1 and sys.argv[1] in ("login", "logout"):
        from .login import main as login_main
        sys.exit(login_main(sys.argv[1:]))
    parser = argparse.ArgumentParser(
        description="Font Render Client — compare TrueType hinting via Windows DirectWrite",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s font.ttf                        # single font waterfall
  %(prog)s font_a.ttf font_b.ttf           # two-font comparison
  %(prog)s a.ttf b.ttf c.ttf d.ttf         # multiple fonts
  %(prog)s a.ttf b.ttf --ppem 9-20 --scale 1.5
  %(prog)s --ping                          # check server status
        """,
    )

    parser.add_argument("fonts", nargs="*", help="TTF font files")

    # Server
    parser.add_argument(
        "--server", "-s",
        default=os.environ.get("FONT_RENDER_SERVER", DEFAULT_SERVER),
        help=f"Server URL (default: {DEFAULT_SERVER}, or FONT_RENDER_SERVER env var)",
    )
    parser.add_argument("--ping", action="store_true", help="Check server status and exit")

    # Server management
    parser.add_argument("--info", action="store_true", help="Show full server info and exit")
    parser.add_argument("--install-browser", metavar="NAME",
                        help="Install a browser on the server (chromium, firefox, webkit)")
    parser.add_argument("--update", action="store_true",
                        help="Update the server: newest release (installed) or git pull (checkout); restarts it")
    parser.add_argument("--rollback", action="store_true", help="Rollback server to previous version")
    parser.add_argument("--deploy", nargs="?", const="origin/main", metavar="REF",
                        help="Developer mode: deploy a git ref (default origin/main) to the server")
    parser.add_argument("--local", action="store_true",
                        help="With --deploy: upload the working tree, uncommitted changes included")
    parser.add_argument("--release", action="store_true", help="Developer mode: back to a release")
    parser.add_argument("--token",
                        default=os.environ.get("FONT_RENDER_TOKEN", ""),
                        help="Legacy admin token for update/rollback/install-browser (or FONT_RENDER_TOKEN); "
                             "FONT_RENDER_ADMIN_TOKEN (Bearer) takes precedence")

    # Render options
    parser.add_argument("--ppem", default="9-24", help="PPEM range: '9-24' or '9,12,16,20' (default: 9-24)")
    parser.add_argument("--text", default="Hamburgefontsiv ABCDEFGHIJKLMNOPQRSTUVWXYZ 0123456789",
                        help="Sample text to render")
    parser.add_argument("--scale", type=float, default=1.0,
                        help="Scale factor: 1.0=96dpi, 1.5=144dpi, 2.0=192dpi (default: 1.0)")
    parser.add_argument("--no-cleartype", dest="cleartype", action="store_false",
                        help="Disable ClearType (use grayscale AA)")
    parser.add_argument("--width", type=int, default=1920, help="Viewport width (default: 1920)")
    parser.add_argument("--browser", choices=["chromium", "firefox", "webkit"],
                        default="chromium", help="Browser engine (default: chromium)")

    # Colors
    parser.add_argument("--colors", default="",
                        help="Comma-separated colors for each font (default: black; white on a dark --bg)")
    parser.add_argument("--bg", default="", metavar="COLOR",
                        help="Page background, #rgb or #rrggbb (default: white)")
    parser.add_argument("--invert", action="store_true",
                        help="White text on black (same as --bg '#000000')")

    # Layout / zoom
    parser.add_argument("--plain", action="store_true",
                        help="Plain layout (text only, no header/labels)")
    parser.add_argument("--zoom", type=int, default=1,
                        help="Nearest-neighbor zoom factor (e.g. 4)")

    # Output
    parser.add_argument("--output-dir", "-o", default=DEFAULT_OUTPUT_DIR,
                        help=f"Output directory (default: {DEFAULT_OUTPUT_DIR})")
    parser.add_argument("--open", action="store_true", help="Open result in image viewer")

    args = parser.parse_args()

    # Ping mode
    if args.ping:
        check_server(args.server)
        return

    # Info mode
    if args.info:
        server_info(args.server)
        return

    # Install browser mode
    if args.install_browser:
        if not args.token and not ADMIN_HEADERS:
            parser.error("--install-browser needs FONT_RENDER_ADMIN_TOKEN (or the legacy --token / FONT_RENDER_TOKEN)")
        install_browser(args.server, args.install_browser, args.token)
        return

    # Update mode
    if args.update:
        if not args.token and not ADMIN_HEADERS:
            parser.error("--update needs FONT_RENDER_ADMIN_TOKEN (or the legacy --token / FONT_RENDER_TOKEN)")
        update_server(args.server, args.token)
        return

    # Developer mode
    if args.deploy or args.release or args.local:
        if not DEV_HEADERS:
            parser.error("--deploy/--release need FONT_RENDER_DEV_TOKEN (a developer-mode client)")
        if args.release:
            release_server(args.server)
        else:
            deploy_server(args.server, None if args.local else args.deploy, args.local)
        return

    # Rollback mode
    if args.rollback:
        if not args.token and not ADMIN_HEADERS:
            parser.error("--rollback needs FONT_RENDER_ADMIN_TOKEN (or the legacy --token / FONT_RENDER_TOKEN)")
        rollback_server(args.server, args.token)
        return

    # Validate inputs
    if not args.fonts:
        parser.error("At least one font file required")

    for path in args.fonts:
        if not os.path.isfile(path):
            print(f"File not found: {path}")
            sys.exit(1)

    # Check server
    if not check_server(args.server):
        sys.exit(1)

    # Render
    path = render_fonts(args)
    if args.open:
        open_image(path)


if __name__ == "__main__":
    main()
