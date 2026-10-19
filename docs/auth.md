# Client tokens

Every client (a person's hintbench, the Telegram bot, your CLI) gets its own token. The
server stores only a sha256 hash of each token in `clients.json` (in the server's project
root, not in git) and identifies the client by it — clients never send a user name.

Tokens are managed remotely from any checkout with `uv run font-render-clients`. It talks to
the server's admin endpoints using `FONT_RENDER_SERVER` and either `FONT_RENDER_ADMIN_TOKEN`
(an admin-scope client token) or the legacy `FONT_RENDER_TOKEN` from `.env`. `font-render
--update/--rollback/--install-browser` work the same way (`FONT_RENDER_ADMIN_TOKEN` first).

## Scopes

| Scope | Allows |
|-------|--------|
| `render` | `/render`, `/fonts`, `/info`, `/docs` |
| `admin` | everything above + `/update`, `/rollback`, `/browsers/install`, `/admin/*` |
| `dev` | `/dev/*` only, while developer mode is on and the client is named in it. Not grantable through the admin API: `font-render-server dev-mode on --client <id>` on the server gives it, switching the mode off removes it ([deployment.md](deployment.md#developer-mode)) |

## Commands

```bash
uv run font-render-clients list                           # clients, scopes, status, settings
uv run font-render-clients add hintbench-anna             # new render client, prints the token ONCE
uv run font-render-clients add ops --scopes render,admin  # admin client
uv run font-render-clients rotate hintbench-anna          # new token, the old one stops working
uv run font-render-clients disable hintbench-anna         # temporarily block
uv run font-render-clients enable hintbench-anna
uv run font-render-clients remove hintbench-anna
uv run font-render-clients settings --anonymous off       # rendering requires a token
uv run font-render-clients settings --legacy-token off    # admin only via an admin Bearer token
```

A lost token cannot be recovered (only its hash is stored) — `rotate` it.

## Giving a token to a client

| Client | Where the token goes |
|--------|----------------------|
| This repo's tools | `.env`: `FONT_RENDER_CLIENT_TOKEN=<token>` — check with `uv run font-render --info` (`Client: <id>`) |
| hintbench | ⚙ Settings → token; it shows "signed in as <id>" |
| font-render-bot | its `.env` on lurkbox: `FONT_RENDER_CLIENT_TOKEN=<token>`, then restart the bot (`uv run python bot_admin.py --restart` in the bot repo, or `nssm restart FontRenderBot`) |

One token per person is recommended (`hintbench-<name>`): it can be revoked alone, the
server log shows who did what, and each token has its own font-cache namespace, so one
user can't reference (or probe) another user's unreleased fonts.

## Settings

`font-render-clients list` prints them:

- `allow_anonymous_render` — when `true`, requests without a recognized token are served
  as anonymous (unknown tokens are ignored). Turn it off once every client is recognized.
- `legacy_admin_token` — when `true`, the shared `FONT_RENDER_TOKEN` form field still grants
  admin rights. Before turning it off, create an admin
  client and put its token into `.env` as `FONT_RENDER_ADMIN_TOKEN`, then confirm
  `font-render-clients list` still works — otherwise you lose remote admin access.

The server refuses (`409`) a change that would leave no admin access: turning off
`legacy_admin_token` with no enabled admin client, or disabling/removing the last admin client
while it is off.

Clients and settings can also be managed in the web admin, `/admin/` ([admin.md](admin.md)).

Changes take effect immediately (the server reloads `clients.json` when it changes).

## Verifying

The server log (`logs\service.log` on the server) has one line per request with the client
id, never font names or sizes:

```
2026-10-14 22:28:35 INFO 127.0.0.1 telegram-bot "POST /render" 200 30318ms
```

`-` in place of the id means the request came without a recognized token.

## Recovery

If something breaks after closing anonymous access: `font-render-clients settings
--anonymous on`, fix the token, close again.

If the admin token is lost, issue a new one **on the server machine**, in an elevated
console (or Start → Font Render → *Reset admin token* on an installed server):

```
font-render-server reset-admin [--client admin]
```

The client (default `admin`) gets a new token, is enabled and has the `admin` scope; it is
created if missing. The old token stops working at once and its web admin sessions end.
The token is printed once (and copied to the clipboard on Windows); the audit log
(`logs/audit.log`) records the reset, not the token. In a checkout run it as
`uv run font-render-server reset-admin`.

Deleting `clients.json` also "recovers" access, but falls back to the defaults (anonymous
rendering on, legacy token accepted) and drops every client — prefer `reset-admin`.
