"""
Tests for the client package settings: config file written by `font-render login`, its
precedence below the environment and .env files, logout (font_render_client).

    uv run pytest tests/test_client.py -v
"""

import os
import stat
import tomllib
from pathlib import Path

import pytest

from font_render_client import _env, login


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setattr(_env.sys, "platform", "linux")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(_env, "PROJECT_ROOT", None)
    for var in _env.CONFIG_KEYS.values():
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(login, "check", lambda server, token: f"client-of-{token[:3]}")
    yield tmp_path / "cfg" / "font-render" / "config.toml"
    for var in _env.CONFIG_KEYS.values():  # load_env sets them directly, not via monkeypatch
        os.environ.pop(var, None)


def test_login_saves_and_load_env_reads(cfg, capsys):
    assert login.main(["login", "http://s:8000/", "--token", "tok-render-123"]) == 0
    assert tomllib.loads(cfg.read_text()) == {"server": "http://s:8000", "token": "tok-render-123"}
    assert stat.S_IMODE(cfg.stat().st_mode) == 0o600
    _env.load_env()
    assert os.environ["FONT_RENDER_SERVER"] == "http://s:8000"
    assert os.environ["FONT_RENDER_CLIENT_TOKEN"] == "tok-render-123"
    assert "client 'client-of-tok'" in capsys.readouterr().out


def test_precedence_env_then_dotenv_then_config(cfg, tmp_path, monkeypatch):
    login.main(["login", "http://from-config:8000", "--token", "config-token"])
    (tmp_path / ".env").write_text("FONT_RENDER_SERVER=http://from-dotenv:8000\n")
    monkeypatch.setenv("FONT_RENDER_CLIENT_TOKEN", "env-token")
    _env.load_env()
    assert os.environ["FONT_RENDER_CLIENT_TOKEN"] == "env-token"
    assert os.environ["FONT_RENDER_SERVER"] == "http://from-dotenv:8000"


def test_adding_admin_token_keeps_server_and_token(cfg):
    login.main(["login", "http://s:8000", "--token", "tok-render-123"])
    login.main(["login", "--admin-token", "adm-token-456"])
    assert tomllib.loads(cfg.read_text()) == {"server": "http://s:8000", "token": "tok-render-123",
                                              "admin_token": "adm-token-456"}


def test_other_server_drops_old_tokens(cfg):
    login.main(["login", "http://a:8000", "--token", "tok-a-1234567", "--admin-token", "adm-a-1234567"])
    login.main(["login", "http://b:8000", "--token", "tok-b-1234567"])
    assert tomllib.loads(cfg.read_text()) == {"server": "http://b:8000", "token": "tok-b-1234567"}


def test_show_masks_tokens(cfg, capsys):
    login.main(["login", "http://s:8000", "--token", "abcdefghijklmnop"])
    capsys.readouterr()
    login.main(["login", "--show"])
    out = capsys.readouterr().out
    assert "abcdefghijklmnop" not in out and "abcd…mnop" in out and "http://s:8000" in out


def test_prompts_for_token(cfg, monkeypatch):
    monkeypatch.setattr(login.getpass, "getpass", lambda prompt: "typed-token-1")
    login.main(["login", "http://s:8000"])
    assert tomllib.loads(cfg.read_text())["token"] == "typed-token-1"


def test_logout(cfg):
    login.main(["login", "http://s:8000", "--token", "tok-render-123"])
    assert login.main(["logout"]) == 0 and not cfg.exists()


def test_quotes_in_values_survive(cfg):
    login.main(["login", 'http://s:8000/a"b\\c', "--token", "tok-render-123"])
    assert tomllib.loads(cfg.read_text())["server"] == 'http://s:8000/a"b\\c'


def test_client_and_server_versions_match():
    root = Path(__file__).resolve().parents[1]
    server = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    client = tomllib.loads((root / "client" / "pyproject.toml").read_text())["project"]["version"]
    assert server == client, "bump client/pyproject.toml together with pyproject.toml"
