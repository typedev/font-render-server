"""Service launcher of the installed layout (stdlib only; docs/distribution.md).

NSSM runs `APP\\python\\…\\python.exe APP\\bin\\launcher.py` — a command that never changes
between versions. The launcher starts the version named in APP\\current. While APP\\trial
exists (a version just switched to and not yet proven healthy) it counts starts; after
MAX_TRIAL_STARTS it switches back to APP\\previous, so a broken version cannot crash-loop
the service. The server deletes APP\\trial once it is healthy.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
MAX_TRIAL_STARTS = 3


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except FileNotFoundError:
        return None


def _write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text + "\n", encoding="utf-8")
    tmp.replace(path)


def _home() -> Path:
    return Path(os.environ.get("FONT_RENDER_HOME") or _read(APP / "home") or APP)


def _audit(event: str, **fields) -> None:
    entry = {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event, **fields}
    try:
        log = _home() / "logs" / "audit.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def choose_version(app: Path = APP) -> str:
    """The version to start, after counting a trial start (may revert to previous)."""
    current = _read(app / "current")
    if current is None:
        raise SystemExit(f"launcher: {app / 'current'} missing")
    trial = app / "trial"
    if trial.exists():
        try:
            starts = int(_read(trial) or 0)
        except ValueError:
            starts = 0
        previous = _read(app / "previous")
        if starts >= MAX_TRIAL_STARTS and previous and previous != current:
            print(f"launcher: {current} failed {starts} starts, reverting to {previous}", flush=True)
            _write(app / "current", previous)
            _write(app / "previous", current)
            trial.unlink(missing_ok=True)
            _audit("auto-revert", failed=current, to=previous, starts=starts)
            return previous
        _write(trial, str(starts + 1))
    return current


def main() -> int:
    version = choose_version()
    python = APP / "versions" / version / ".venv" / "Scripts" / "python.exe"
    print(f"launcher: starting {version}", flush=True)
    proc = subprocess.Popen([str(python), "-m", "font_render", "serve"], cwd=str(APP))
    while True:
        try:
            return proc.wait()
        except KeyboardInterrupt:
            # NSSM stop sends Ctrl+C to the console: the server got it too, let it finish
            continue


if __name__ == "__main__":
    sys.exit(main())
