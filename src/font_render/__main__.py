"""Run the server: ``uv run font-render-server`` (or ``python -m font_render``).

Subcommands for the machine the server runs on (docs/distribution.md):

    font-render-server [serve]          run the server (default)
    font-render-server install          install or repair the Windows service (install.ps1)
    font-render-server uninstall        remove it [--purge | --keep-data] [--quiet]
    font-render-server reset-admin      new token for the admin client (elevated console)
    font-render-server dev-mode on|off|status   developer mode (elevated console)
    font-render-server selfcheck        import every module, check the browser
    font-render-server version
"""

import argparse
import importlib
import pkgutil
import socket
import sys
from pathlib import Path

from . import __version__


def get_local_ip() -> str:
    """Detect the LAN IP address of this machine."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.168.1.1", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def cmd_serve(args) -> int:
    import uvicorn

    from . import config
    from .app import app

    host = get_local_ip()
    print(f"Starting Font Render Service on http://{host}:{config.PORT}")
    print(f"API docs: http://{host}:{config.PORT}/docs")
    # access_log off: AuthMiddleware logs one line per request with the client id
    uvicorn.run(app, host=config.HOST, port=config.PORT, access_log=False)
    return 0


def cmd_selfcheck(args) -> int:
    """Exit 0 if every module of the package imports and Chromium is installed."""
    import font_render

    failed = []
    for mod in pkgutil.iter_modules(font_render.__path__, "font_render."):
        if mod.name == "font_render.__main__":
            continue
        try:
            importlib.import_module(mod.name)
        except Exception as e:  # noqa: BLE001 — report every broken module
            failed.append(f"{mod.name}: {type(e).__name__}: {e}")
    if not args.no_browser:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            exe = Path(p.chromium.executable_path)
        if not exe.is_file():
            failed.append(f"chromium not installed: {exe}")
    for line in failed:
        print("FAIL", line)
    if not failed:
        print(f"OK {__version__}")
    return 1 if failed else 0


def cmd_version(args) -> int:
    print(__version__)
    return 0


def main(argv: list[str] | None = None) -> int:
    from . import devmode, install, localadmin

    # A Windows console or pipe may use a legacy code page: never crash on a character
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    parser = argparse.ArgumentParser(prog="font-render-server", description="Font Render Service")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("serve", help="run the server (default)").set_defaults(func=cmd_serve)
    install.add_parsers(sub)
    devmode.add_parser(sub)
    p = sub.add_parser("reset-admin", help="issue a new token for the admin client (elevated console)")
    p.add_argument("--client", default="admin", help="client id (default: admin)")
    p.set_defaults(func=localadmin.cmd_reset_admin)
    p = sub.add_parser("selfcheck", help="import every module and check the browser")
    p.add_argument("--no-browser", action="store_true", help="skip the browser check")
    p.set_defaults(func=cmd_selfcheck)
    sub.add_parser("version", help="print the version").set_defaults(func=cmd_version)
    args = parser.parse_args(argv)
    return args.func(args) if args.cmd else cmd_serve(args)


if __name__ == "__main__":
    sys.exit(main())
