# Embedded mode

A private render server for one desktop app on the same Windows machine (hintbench), so it
renders with the machine's own DirectWrite/GDI without a network, a service or admin rights.
Code: `src/font_render/embedded.py`. The app gets it from PyPI (`font-render-server`, a wheel;
dependency marker `sys_platform == 'win32'`); data of a package install without `--home`:
`%LOCALAPPDATA%\FontRender`.

```
font-render-server embedded [--home DIR] [--port N] [--keep-running]
python -m font_render embedded ...
```

| | Installed server | Embedded |
|---|---|---|
| Runs as | NSSM service, LocalSystem | the user, a child process of the app |
| Listens on | `0.0.0.0:41800` | `127.0.0.1`, a free port (`--port` to fix it) |
| Clients | `clients.json`, tokens issued by an admin | one, `local` (render scope), token made at start, in memory |
| Web admin, `/update`, `/rollback`, `/browsers/install`, `/dev/*` | yes | no (404) |
| Data | `C:\ProgramData\FontRender` | `--home`, default `%LOCALAPPDATA%\FontRender\embedded` |
| Logs | `logs\service.log` (stdout) | stderr |
| Browsers per key (`RENDER_POOL_SIZE`) | 2 | 1, unless set |

## Protocol

1. The app starts the server with `stdin=PIPE` and `stdout=PIPE` (stderr: a log file or
   `DEVNULL`).
2. The first stdout line is the handshake, JSON:
   `{"url": "http://127.0.0.1:PORT", "token": "...", "version": "1.0.2", "render_version": 2}`.
   The port is listening before the line is written: requests sent at once wait while the
   server starts (the browsers warm up in the background, as on a server).
3. The app sends the usual API requests (`docs/api.md`) with `Authorization: Bearer TOKEN`.
4. The server exits when its stdin closes: the app closed the pipe or exited, even if it
   crashed. No detached process is left behind. `--keep-running` turns this off (run by hand).

```python
proc = subprocess.Popen([sys.executable, "-m", "font_render", "embedded"],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
hello = json.loads(proc.stdout.readline())
requests.get(f"{hello['url']}/info", headers={"Authorization": f"Bearer {hello['token']}"})
...
proc.stdin.close(); proc.wait()
```

## Pixels

The rendering code is the server's, so `RENDER_VERSION` holds. Running as the user, not as
LocalSystem, the renders follow that user's ClearType Tuner and font smoothing settings, and the
Windows build and rasterizer DLLs are that machine's: `/info` reports them (`windows`), and
hintbench keys its render cache on them, so renders of different machines never mix.

## Browsers

Chromium comes from Playwright as usual (`python -m playwright install chromium`, in the
user's `%LOCALAPPDATA%\ms-playwright` unless `PLAYWRIGHT_BROWSERS_PATH` is set); the app's
installer does this once.
