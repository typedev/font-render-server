"""Settings of the command-line tools: environment, .env files, the login config file.

Precedence (first wins): the environment, ./.env, the repository's .env (when run from a
checkout of the server repository), the config file written by `font-render login`
(~/.config/font-render/config.toml; %APPDATA%\\font-render\\config.toml on Windows).
"""

import os
import sys
import tomllib
from pathlib import Path

# client/src/font_render_client/_env.py -> parents[3]: the repository root in a checkout
_REPO = Path(__file__).resolve().parents[3]
PROJECT_ROOT = _REPO if (_REPO / "client" / "pyproject.toml").is_file() else None

# config file key -> environment variable
CONFIG_KEYS = {
    "server": "FONT_RENDER_SERVER",
    "token": "FONT_RENDER_CLIENT_TOKEN",
    "admin_token": "FONT_RENDER_ADMIN_TOKEN",
    "dev_token": "FONT_RENDER_DEV_TOKEN",
}


def config_path() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "font-render" / "config.toml"


def read_config() -> dict:
    try:
        return tomllib.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


def load_env():
    """Fill os.environ (without overriding it) from .env files and the config file."""
    _load_dotenv(Path.cwd() / ".env")
    if PROJECT_ROOT is not None:
        _load_dotenv(PROJECT_ROOT / ".env")
    for key, var in CONFIG_KEYS.items():
        value = read_config().get(key)
        if isinstance(value, str) and value:
            os.environ.setdefault(var, value)
