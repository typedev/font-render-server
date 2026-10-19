"""Local administration: commands run on the server machine by a Windows administrator.

They edit files in config.HOME directly (no HTTP), which only administrators can write
in an installed layout (docs/distribution.md), so they work when every token is lost.
"""

import ctypes
import getpass
import json
import os
import socket
import subprocess
import time

from . import auth, config


def is_elevated() -> bool:
    """True in an elevated (administrator) process on Windows, as root elsewhere."""
    if os.name == "nt":
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except (AttributeError, OSError):
            return False
    return os.geteuid() == 0


def require_elevated(what: str) -> None:
    """Exit with a hint unless elevated (Windows only: a checkout elsewhere has no ACLs to match)."""
    if os.name == "nt" and not is_elevated():
        raise SystemExit(f"{what} needs an elevated console: right-click > Run as administrator")


def local_user() -> str:
    try:
        user = getpass.getuser()
    except OSError:
        user = "?"
    return f"{socket.gethostname()}\\{user}"


def audit_line(event: str, **fields) -> str:
    """One audit log line (JSON): time, event, fields — never font names or tokens."""
    entry = {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event, **fields}
    return json.dumps(entry, ensure_ascii=False) + "\n"


def audit(event: str, **fields) -> None:
    """Append one line to config.AUDIT_LOG."""
    try:
        config.AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(config.AUDIT_LOG, "a", encoding="utf-8") as f:
            f.write(audit_line(event, **fields))
    except OSError as e:
        config.log.error("audit log not writable: %s", e)
    config.log.info("audit: %s", event)


def reset_admin(registry: auth.ClientRegistry, client_id: str = "admin") -> str:
    """Give `client_id` a new token, enabled, with the admin scope (created if missing).

    The old token stops working at once: the server reloads clients.json on change, and
    web admin sessions end because their fingerprint is the token hash.
    """
    clients = registry.clients()
    client = clients.get(client_id)
    created = client is None
    if created:
        client = clients[client_id] = {"scopes": ["render", "admin"],
                                       "created": time.strftime("%Y-%m-%dT%H:%M:%S")}
    client["disabled"] = False
    for scope in ("render", "admin"):
        if scope not in client.setdefault("scopes", []):
            client["scopes"].append(scope)
    token = auth.new_token(client)
    registry.path.parent.mkdir(parents=True, exist_ok=True)
    registry.save()
    audit("reset-admin", client=client_id, created=created, by=local_user())
    return token


def copy_to_clipboard(text: str) -> bool:
    """Best effort (Windows clip.exe); False if it did not work."""
    if os.name != "nt":
        return False
    try:
        # clip.exe reads UTF-16LE from stdin
        subprocess.run(["clip"], input=text.encode("utf-16-le"), check=True, timeout=10)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def cmd_reset_admin(args) -> int:
    require_elevated("reset-admin")
    registry = auth.ClientRegistry(config.CLIENTS_FILE)
    token = reset_admin(registry, args.client)
    copied = copy_to_clipboard(token)
    print(f"New token of admin client '{args.client}' (shown only once):\n\n  {token}\n")
    if copied:
        print("Copied to the clipboard.")
    print(f"The old token no longer works. Web admin: http://{socket.gethostname()}:{config.PORT}/admin/")
    return 0
