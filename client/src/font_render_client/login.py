"""`font-render login` / `logout`: keep the server address and tokens in a config file.

    font-render login http://studio-pc:41800             # asks for the token (not echoed)
    font-render login http://studio-pc:41800 --token T   # or pass it
    font-render login --admin-token T                   # add an admin token to the saved server
    font-render login --show                            # what is saved (tokens masked)
    font-render logout                                  # delete the config file

The token is checked against the server (GET /info) before it is saved. The file is
readable by its owner only (on Windows it lives in the user's own %APPDATA%).
"""

import argparse
import getpass
import os
import sys

from ._env import CONFIG_KEYS, config_path, read_config


def _toml_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_config(data: dict) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# font-render settings, written by `font-render login`"]
    lines += [f"{k} = {_toml_string(v)}" for k, v in data.items() if k in CONFIG_KEYS and v]
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, path)


def _mask(token: str) -> str:
    return f"{token[:4]}…{token[-4:]}" if len(token) > 12 else "…"


def check(server: str, token: str) -> str:
    """Ask the server who this token is; returns the client id. Exits on failure."""
    import requests
    try:
        r = requests.get(f"{server}/info", headers={"Authorization": f"Bearer {token}"}, timeout=15)
    except requests.RequestException as e:
        sys.exit(f"Cannot reach {server}: {e}")
    if r.status_code in (401, 403):
        sys.exit("The server does not accept this token")
    if r.status_code != 200:
        sys.exit(f"{server}/info: HTTP {r.status_code}")
    client = (r.json().get("auth") or {}).get("client")
    if not client:
        sys.exit("The server did not recognise this token")
    return client


def main(argv: list[str]) -> int:
    if argv and argv[0] == "logout":
        path = config_path()
        path.unlink(missing_ok=True)
        print(f"Removed {path}")
        return 0
    p = argparse.ArgumentParser(prog="font-render login", description="Save the server and tokens")
    p.add_argument("server", nargs="?", help="e.g. http://studio-pc:41800 (default: the saved one)")
    p.add_argument("--token", help="render token (asked for if missing)")
    p.add_argument("--admin-token", help="admin token (font-render-clients, --update, --rollback)")
    p.add_argument("--dev-token", help="developer-mode token (--deploy, --release)")
    p.add_argument("--show", action="store_true", help="print what is saved")
    args = p.parse_args(argv[1:])
    data = read_config()
    if args.show:
        print(f"{config_path()}:")
        for key in CONFIG_KEYS:
            value = data.get(key)
            print(f"  {key}: {(value if key == 'server' else _mask(value)) if value else '-'}")
        return 0
    server = (args.server or data.get("server") or "").rstrip("/")
    if not server:
        p.error("give the server address, e.g. font-render login http://studio-pc:41800")
    if server != data.get("server"):
        data = {}  # another server: tokens of the old one do not apply
    data["server"] = server
    token = args.token or (None if data.get("token") or args.admin_token or args.dev_token
                           else getpass.getpass("Render token: ").strip())
    for key, value in (("token", token), ("admin_token", args.admin_token)):
        if value:
            print(f"{key.replace('_', ' ')}: client '{check(server, value)}'")
            data[key] = value
    if args.dev_token:
        data["dev_token"] = args.dev_token  # /dev/* answers only while developer mode is on
    write_config(data)
    print(f"Saved to {config_path()}")
    return 0
