# Changelog

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
