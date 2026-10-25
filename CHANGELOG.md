# Changelog

## Unreleased

- Default port 41800 instead of 8000 (8000 is a common development port; 41800 is below the
  range where Windows and Hyper-V reserve ports). Existing installs keep their port on update
  and repair (a checkout imported with `--import-from` stays on 8000 unless its `.env` says
  otherwise); move one with `install.ps1 -Port 41800`. Client defaults follow.
- Web admin login: a Client field (the username for password managers; it must match the
  token, empty = any), so Chrome saves and fills the admin token.
- Embedded mode (`font-render-server embedded`): a private server for a desktop app on
  127.0.0.1, token in memory, handshake on stdout, exits with its parent (docs/embedded.md).

## 1.0.2 — 2026-10-19

- The install summary, the server's console and web admin → Overview list the addresses
  clients can use: the computer name (keeps working under DHCP), `<name>.local`, and the
  current IP of each network adapter. How to find the server and fix its IP:
  docs/deployment.md#finding-the-server.
- Port: a repair keeps the installed port (it used to reset it to 8000); if 8000 is taken by
  another program the installer takes the next free port and says which program has it; an
  explicit `-Port` that is taken stops the install before any change, and a refused repair
  restarts the service it stopped.
- Documentation checked against the code: `.env.example` no longer sets a placeholder
  legacy admin token, the checkout install bootstraps with `reset-admin`, examples use
  neutral server names, THIRD_PARTY.md lists what a release brings along.

## 1.0.1 — 2026-10-19

- `install.ps1` run with `irm | iex` no longer closes the PowerShell window when it finishes
  (the summary with the admin token stayed invisible) or fails; started from a
  non-administrator PowerShell it now relaunches itself elevated correctly.

## 1.0.0 — 2026-10-19

First public release.

- One-command install on Windows (`install.ps1`): service, firewall rule, first admin token,
  Start menu, Apps & features entry; repair by running it again; clean uninstall.
- Updates from the web admin: signed releases (Ed25519), stable / beta channels, rollback,
  automatic revert of a version that fails to start.
- `reset-admin` for a lost admin token; audit log.
- Developer mode: local-only switch-on, expiring, deploys of a commit or a working tree.
- `font-render-client` package on PyPI with `font-render login`.
- Rendering: `/render` (Chromium, DirectWrite), `/glyphs` (GDI, DirectWrite), font cache,
  per-client tokens, web admin with load and traffic statistics.
