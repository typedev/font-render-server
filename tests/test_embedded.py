"""
Tests for embedded mode (embedded.py): the handshake, the one in-memory client, no admin
surface, and exit when the parent closes stdin. Runs a real server process.

    uv run pytest tests/test_embedded.py -v
"""

import json
import subprocess
import sys

import pytest
import requests


@pytest.fixture
def server(tmp_path):
    proc = subprocess.Popen([sys.executable, "-m", "font_render", "embedded", "--home", str(tmp_path)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        hello = json.loads(proc.stdout.readline())
        yield proc, hello
    finally:
        proc.kill()
        proc.wait(timeout=10)


def test_handshake_and_token(server):
    _, hello = server
    url, token = hello["url"], hello["token"]
    assert url.startswith("http://127.0.0.1:") and len(token) > 30
    assert isinstance(hello["render_version"], int)
    assert requests.get(f"{url}/health", timeout=30).status_code == 200
    assert requests.get(f"{url}/info", timeout=30).status_code == 401
    assert requests.get(f"{url}/info", headers={"Authorization": f"Bearer {token}"}, timeout=30).status_code == 200
    assert requests.post(f"{url}/render", timeout=30).status_code == 401  # no anonymous rendering
    assert requests.post(f"{url}/render", headers={"Authorization": "Bearer wrong"},
                         timeout=30).status_code == 401
    # authorized: past the auth check (a request without fonts is the endpoint's own 4xx)
    r = requests.post(f"{url}/render", headers={"Authorization": f"Bearer {token}"}, timeout=30)
    assert r.status_code not in (401, 403)


def test_no_admin_surface(server):
    _, hello = server
    url, auth = hello["url"], {"Authorization": f"Bearer {hello['token']}"}
    for path in ("/admin/", "/admin/api/status", "/dev/status"):
        assert requests.get(f"{url}{path}", headers=auth, timeout=30).status_code == 404, path
    for path in ("/admin/login", "/update", "/rollback", "/browsers/install"):
        assert requests.post(f"{url}{path}", headers=auth, timeout=30).status_code == 404, path


def test_exits_when_stdin_closes(server):
    proc, _ = server
    proc.stdin.close()
    assert proc.wait(timeout=30) == 0
