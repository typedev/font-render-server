# font-render-client

Command-line client of the [Font Render Service](https://github.com/typedev/font-render-server):
send fonts (TTF, OTF, WOFF, WOFF2) to a Windows server that renders them with DirectWrite in
headless Chromium, get PNG waterfalls back.

```bash
uv tool install font-render-client          # or: pipx install font-render-client
font-render login http://studio-pc:8000      # asks for your token (from the server's admin)
font-render MyFont.ttf                       # waterfall 9-24 ppem -> ./renders/
font-render A.ttf B.ttf --ppem 9-40          # compare fonts
font-render MyFont.ttf --plain --zoom 4      # text only, 4x nearest-neighbour
font-render --info                           # server and which client you are
```

Fonts are sent to the server and kept in its memory only; the server never writes them to
disk.

| Command | |
|---|---|
| `font-render login [SERVER] [--token T] [--admin-token T] [--dev-token T]` | Check the token against the server and save it (`~/.config/font-render/config.toml`, `%APPDATA%\font-render\config.toml` on Windows) |
| `font-render login --show` / `font-render logout` | Show (tokens masked) / delete the saved settings |
| `font-render FONT... [--ppem 9-24] [--text ...] [--scale 1.5] [--plain] [--zoom N] [--invert]` | Render |
| `font-render-batch DIR_A DIR_B` | Render font pairs across browsers |
| `font-render-clients list\|add\|rotate\|disable\|enable\|remove` | Manage client tokens (admin token) |
| `font-render --update` / `--rollback` | Update the server to the newest release / go back (admin token) |

Settings can also come from the environment or a `.env` file in the current directory:
`FONT_RENDER_SERVER`, `FONT_RENDER_CLIENT_TOKEN`, `FONT_RENDER_ADMIN_TOKEN`,
`FONT_RENDER_DEV_TOKEN`; they take precedence over the saved settings.
