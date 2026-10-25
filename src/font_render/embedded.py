"""Embedded mode: a private render server for one desktop app on the same machine (hintbench
on Windows), so it renders without a network or an installed service.

    font-render-server embedded [--home DIR] [--port N] [--keep-running]

No service, no admin rights; no web admin, updates or developer mode: the app that starts it
owns it. Listens on 127.0.0.1 only (a free port unless --port), with one client, `local`,
render scope, whose token is made at start and kept in memory. The first stdout line is the
handshake, JSON: {"url", "token", "version", "render_version"}; logs go to stderr. The port
listens before the handshake is written, so requests sent at once wait for the start-up.

The server exits when its stdin closes — the parent exited or closed the pipe — so it never
outlives the app (start it with stdin=PIPE; --keep-running turns this off). Data (settings,
stats) goes to --home, default %LOCALAPPDATA%\\FontRender\\embedded. See docs/embedded.md.
"""

import argparse
import json
import os
import socket
import sys
import threading
from pathlib import Path

CLIENT_ID = "local"


def default_home() -> Path:
    base = os.environ.get("LOCALAPPDATA") or Path.home() / ".local" / "share"
    return Path(base) / "FontRender" / "embedded"


def _watch_stdin(server) -> None:
    """Stop the server once stdin reaches EOF (the parent is gone)."""
    try:
        while sys.stdin.buffer.read(65536):
            pass
    except (OSError, ValueError, AttributeError):
        pass
    print("embedded: stdin closed, exiting", file=sys.stderr, flush=True)
    server.should_exit = True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="font-render-server embedded",
                                     description="Private render server for a desktop app (127.0.0.1)")
    parser.add_argument("--home", type=Path, help="data directory (default: %(default)s)",
                        default=default_home())
    parser.add_argument("--port", type=int, default=0, help="port on 127.0.0.1 (default: a free one)")
    parser.add_argument("--keep-running", action="store_true", help="do not exit when stdin closes")
    args = parser.parse_args(argv)

    if "font_render.config" in sys.modules:
        raise RuntimeError("embedded mode must start before font_render.config is imported")
    args.home.mkdir(parents=True, exist_ok=True)
    os.environ["FONT_RENDER_EMBEDDED"] = "1"
    os.environ["FONT_RENDER_HOME"] = str(args.home)
    os.environ["FONT_RENDER_HOST"] = "127.0.0.1"
    os.environ.setdefault("RENDER_POOL_SIZE", "1")  # one user: one browser per key

    import uvicorn

    from . import __version__, auth, config

    auth.registry = auth.ClientRegistry(None)
    auth.registry.data["settings"].update(allow_anonymous_render=False, legacy_admin_token=False)
    client = auth.registry.data["clients"][CLIENT_ID] = {"scopes": ["render"]}
    token = auth.new_token(client)

    from .app import app

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind((config.HOST, args.port))
    sock.listen(128)
    port = sock.getsockname()[1]
    # access_log off: AuthMiddleware logs one line per request (to stderr here)
    server = uvicorn.Server(uvicorn.Config(app, access_log=False))
    print(json.dumps({"url": f"http://127.0.0.1:{port}", "token": token, "version": __version__,
                      "render_version": config.RENDER_VERSION}), flush=True)
    if not args.keep_running and sys.stdin is not None and not sys.stdin.isatty():
        threading.Thread(target=_watch_stdin, args=(server,), daemon=True).start()
    server.run(sockets=[sock])
    return 0


if __name__ == "__main__":
    sys.exit(main())
