"""
Manage client tokens of the Font Render Service.

Talks to the server's /admin endpoints, authenticated with an admin Bearer
token (FONT_RENDER_ADMIN_TOKEN) or the legacy FONT_RENDER_TOKEN form token.
A new token is printed once and never stored in clear text on the server.

Usage:
    uv run font-render-clients list
    uv run font-render-clients add hintbench                 # scope: render
    uv run font-render-clients add ops --scopes render,admin
    uv run font-render-clients rotate hintbench
    uv run font-render-clients disable|enable|remove hintbench
    uv run font-render-clients settings --anonymous off      # require tokens for rendering
    uv run font-render-clients settings --legacy-token off   # admin only via Bearer

Server URL from --server or FONT_RENDER_SERVER (.env is auto-loaded).
"""

import argparse
import os
import sys

import requests

from ._env import load_env




def _on_off(value: str) -> str:
    v = value.lower()
    if v not in ("on", "off", "true", "false"):
        raise argparse.ArgumentTypeError("use on/off")
    return "true" if v in ("on", "true") else "false"


def main():
    load_env()
    parser = argparse.ArgumentParser(description="Manage Font Render Service client tokens")
    parser.add_argument("--server", default=os.environ.get("FONT_RENDER_SERVER", "http://localhost:8000"))
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    p = sub.add_parser("add")
    p.add_argument("client_id")
    p.add_argument("--scopes", default="render", help="comma-separated: render, admin")
    for name in ("rotate", "disable", "enable", "remove"):
        sub.add_parser(name).add_argument("client_id")
    p = sub.add_parser("settings")
    p.add_argument("--anonymous", type=_on_off, help="allow_anonymous_render on/off")
    p.add_argument("--legacy-token", type=_on_off, help="accept the legacy form token for admin")
    args = parser.parse_args()

    session = requests.Session()
    admin_token = os.environ.get("FONT_RENDER_ADMIN_TOKEN", "")
    if admin_token:
        session.headers["Authorization"] = f"Bearer {admin_token}"
    data = {"token": os.environ.get("FONT_RENDER_TOKEN", "")}

    if args.cmd == "list":
        url = "/admin/clients/list"
    elif args.cmd == "add":
        url = "/admin/clients/add"
        data.update(client_id=args.client_id, scopes=args.scopes)
    elif args.cmd == "settings":
        url = "/admin/settings"
        if args.anonymous:
            data["allow_anonymous_render"] = args.anonymous
        if args.legacy_token:
            data["legacy_admin_token"] = args.legacy_token
    else:
        url = f"/admin/clients/{args.cmd}"
        data["client_id"] = args.client_id

    resp = session.post(args.server.rstrip("/") + url, data=data, timeout=15)
    if resp.status_code != 200:
        sys.exit(f"Error {resp.status_code}: {resp.text}")
    result = resp.json()

    if args.cmd == "list":
        for c in result["clients"]:
            state = "disabled" if c["disabled"] else "active"
            print(f"  {c['client_id']:<24} {','.join(c['scopes']):<14} {state:<9} {c['created']}")
        if not result["clients"]:
            print("  (no clients)")
        print(f"  settings: {result['settings']}")
    elif args.cmd == "settings":
        print(f"  settings: {result['settings']}")
    else:
        print(f"  {result['client_id']}: scopes={','.join(result['scopes'])} disabled={result['disabled']}")
        if "token" in result:
            print(f"\n  token (shown once, store it in the client's .env):\n  {result['token']}\n")


if __name__ == "__main__":
    main()
