# HTTP API

Base URL: `http://<server>:8000`. Interactive docs: `GET /docs` (needs a token once anonymous
access is off). `GET /info` reports `"api": 3` (2 = font cache, 3 = engines and Windows render settings).

## Authentication

Send `Authorization: Bearer <token>` on every request. Tokens are issued per client
([auth.md](auth.md)).

| Situation | Response |
|-----------|----------|
| No/unknown token, anonymous rendering allowed | treated as anonymous |
| No/unknown token, anonymous rendering off | `401 {"error":"unauthorized"}` |
| Valid token without the needed scope | `403 {"error":"forbidden"}` |

`GET /health` is always public. Admin endpoints (`/update`, `/rollback`, `/browsers/install`,
`/admin/*`) need a token with the `admin` scope, or the legacy form field `token`
(= `FONT_RENDER_TOKEN`) while `legacy_admin_token` is on. A rejected request is answered
before its body is read.

## POST /render

Renders an interleaved waterfall: for each PPEM, one row per font. Returns `image/png`, or
JSON in geometry mode.

Fonts come from `font_ids` (cached, see below) and/or uploaded `fonts` files, in that
order (ids first, then files). 1–20 fonts per request, up to 10 MB each.

| Field | Default | Description |
|-------|---------|-------------|
| `fonts` | — | Multipart font files (TTF/OTF/WOFF/WOFF2; format is sniffed from the file signature) |
| `font_ids` | `""` | Comma-separated sha256 of fonts cached via `POST /fonts` |
| `ppem` | `9-24` | Range `9-24` or list `9,12,16,20` (max 100 values) |
| `text` | `Hamburgefontsiv ABC…` | Sample text |
| `scale` | `1.0` | Device scale factor (1.0 = 96 DPI, 1.5 = 144, 2.0 = 192) |
| `cleartype` | `true` | ClearType (LCD) vs grayscale antialiasing (Chromium) |
| `width` | `1920` | Initial viewport width; the screenshot is fitted to the content |
| `colors` | all `#000000` | Comma-separated color per font (same order as the fonts); all `#ffffff` on a dark `background` |
| `background` | white | Page background, `#rgb` or `#rrggbb`. White on black (`#000000`) is reverse video: Skia/DirectWrite render light-on-dark text with their own gamma and contrast, so it is not an inverted black-on-white render. On a dark background the separators and labels are dimmed |
| `browser` | `chromium` | `chromium`, `firefox` or `webkit` — see [Browsers](#browsers) |
| `layout` | `full` | `full` (header + PPEM labels) or `plain` (text only, minimal padding) |
| `zoom` | `1` | Nearest-neighbor upscale 1–8, applied after rendering |
| `line_height` | `0` | CSS line-height of rows; `0` = browser default. `2.0` keeps accents/descenders inside their row |
| `geometry` | `false` | Return JSON with glyph geometry (below) |
| `variations` | `""` | Variable-font location per font, `;`-separated in font order: `wght=350.5,wdth=87;;wght=700`. An empty entry renders that font as before. See [Variable fonts](#variable-fonts) |
| `delay_font`, `delay_resize` | `0` | Extra waits in ms (not needed; kept for experiments) |

Errors: `400` invalid parameters or an empty / too large font file,
`409 {"error":"missing_fonts","missing":[sha…]}` when a `font_id` is not cached (upload it
again), `429 {"error":"busy"}` with `Retry-After` when the
queue or the per-client limit is full, `422 {"error":"font_not_loaded","fonts":[i…]}` when the
browser rejects a font (0-based positions in the request; the page never falls back to another
font), `504 {"error":"timeout"}`.

### Geometry mode

With `geometry=true` the response is:

```json
{"image": "<base64 PNG>",
 "geometry": {"device_scale": 1.0, "zoom": 1,
              "rows": [{"ppem": 12, "font": 1, "baseline_y": 31.0, "box": [x0, y0, x1, y1],
                        "glyphs": [{"i": 0, "char": "H", "x0": 12.0, "x1": 20.0, "y0": 20.0, "y1": 35.0}]}]}}
```

`font` is the 1-based position in the request. Coordinates are in final-PNG pixels (already
multiplied by `scale` and `zoom`). Collecting geometry does not change the pixels.
With `variations`, `geometry.fonts` lists each font's resolved location:
`[{"index": 1, "sha256": "…", "variations": {"wdth": 87.0, "wght": 350.5}}]` (`null` for no
location).

### Variable fonts

`variations` renders a variable font at an arbitrary location, without cutting static
instances. Values are user (design) coordinates, may be fractional, and `avar` is applied the
way applications apply it.
- **All axes are set.** Every `fvar` axis goes into `font-variation-settings`: given values are
  clamped to the axis range, missing axes get their `fvar` default. Otherwise Chromium would set
  `wght` from `font-weight`, `wdth` from `font-stretch` and `opsz` from the font size.
- **Unknown axes are rejected.** A tag the font does not have, or a location for a static font,
  answers `400 {"error": "bad_variations"}`.
- **Several locations per request.** List the same sha several times in `font_ids`, one row
  per location. The font is embedded in the page once.

Checked on the maintainers' Windows 11 server: per-glyph advances at a fractional location match a fontTools instance
(avar + HVAR) within 0.03 px in Chromium and exactly in `/glyphs` dwrite.

### Server-Timing

Every response carries `Server-Timing` (ms): `queue` (wait for a render slot), `fonts`
(read + page build), `load` (page load + explicit load of every `@font-face` + fit), `shot` (screenshot +
zoom), `geom`, `total`, and `page;desc="new"|"reuse"` — whether a page with the same fonts
already loaded was reused.

### Browsers

- `chromium` — what Chrome, Edge, Opera, Brave, Vivaldi and other Chromium browsers show on
  Windows (Skia + DirectWrite). The reference engine: pixel identity is checked for it only.
- `firefox` — headless Firefox (needs `font-render --install-browser firefox`). It renders
  **grayscale only**: desktop Firefox uses ClearType, but headless Firefox on Windows does not,
  whatever the system setting or prefs (checked 2026-10-13). It still shows Firefox's own
  weight and contrast (about 4–6 % more ink than Chromium grayscale, fewer gray levels).
  `cleartype` has no effect.
- `webkit` — Playwright's Windows WebKit port; no real browser on Windows renders like it.
  Not useful for judging rendering.

Neither Chromium nor Firefox applies the font's `gasp` table (byte-identical output for any
gasp flags), so bi-level / "follow gasp" proofs are not available.

## POST /glyphs

Glyph ids rendered by a native Windows engine instead of a browser: `engine=gdi` or
`engine=dwrite`. `/info` lists the engines this host runs in `engines`. Multipart form fields:

| Field | Default | Meaning |
|-------|---------|---------|
| `engine` | — | `gdi` or `dwrite` |
| `font_ids` / `fonts` | — | Exactly one font: a sha256 from `POST /fonts`, or one upload |
| `gids` | — | Glyph ids, `12,13,40-60`; at most 512 |
| `ppem` | `9-24` | Device pixels per em, `9-24` or `9,12.5,16`; whole numbers for GDI, fractional allowed for DirectWrite; 1–256. There is no `scale`: send ppem × scale (GDI: rounded) |
| `layout` | `cells` | `cells`: each glyph is drawn alone at an integer pen origin with `gap` around its ink, so its pixels relative to (`x0`, `baseline_y`) do not depend on the other gids in the request. `run`: the engine lays the glyphs out itself in one call (spacing view; ClearType fringes may touch) |
| `gap` | `4` | Pixels between cells, rows and around the image (0–64) |
| `background`, `ink` | `#ffffff`, `#000000` | `#rgb` / `#rrggbb` |
| `mode` (gdi) | `cleartype` | `bw` (NONANTIALIASED_QUALITY), `gray` (ANTIALIASED_QUALITY), `cleartype` (CLEARTYPE_QUALITY, compatible widths), `cleartype_natural` (CLEARTYPE_NATURAL_QUALITY) |
| `rendering_mode` (dwrite) | `natural_symmetric` | `aliased`, `gdi_classic`, `gdi_natural`, `natural`, `natural_symmetric`, or `default`: DirectWrite picks the mode from size and `gasp` (`GetRecommendedRenderingMode`). `gasp` is used only by `default`; the mode it picked is returned as `rows[].rendering_mode`. A font with neither `fpgm` nor `prep` is treated as unhinted: DirectWrite then recommends `natural_symmetric` whatever `gasp` says (a one-instruction `prep` is enough to change that; glyph programs alone are not) |
| `measuring_mode` (dwrite) | `natural` | `natural`, `gdi_classic`, `gdi_natural`: the advances used for `layout=run` and returned as `advance` |
| `antialias` (dwrite) | `cleartype` | `cleartype` or `grayscale` (ClearType Gray) |
| `pixel_geometry` (dwrite) | `rgb` | `rgb`, `bgr`, `flat` |
| `gamma` (dwrite) | `1.8` | 1.0–3.0 |
| `contrast` (dwrite) | `0.5` | Enhanced contrast, 0–10 |
| `grayscale_contrast` (dwrite) | `1.0` | Enhanced contrast for grayscale, 0–10 |
| `cleartype_level` (dwrite) | `1.0` | 0–1 |
| `grid_fit` (dwrite) | `default` | `default`, `disabled`, `enabled` |
| `axes` (dwrite) | — | Variable font location in user coordinates, `wght=350.5,wdth=80`. It goes through `IDWriteFontResource::CreateFontFace`, with avar applied. Every `fvar` axis is set (clamped, defaults for missing ones) and returned in `params.axes`. A tag not in `fvar` → `400`. GDI cannot render arbitrary locations (`400`): send a static instance |

The DirectWrite defaults are the rendering params of a clean Windows install (`/info`
`windows.dwrite_default_params` shows the same on the maintainers' server), so `dwrite` output does not depend
on the account's settings. Windows does the blending itself:
`IDWriteGdiInterop::CreateBitmapRenderTarget` → `DrawGlyphRun` onto the background with params
from `IDWriteFactory2::CreateCustomRenderingParams`. The server applies no gamma or contrast of
its own. The font is loaded through the `IDWriteFactory5` in-memory loader.

GDI follows the font's `gasp` table (to ignore it, send a copy with `gasp` patched). GDI ClearType
gamma (contrast) and subpixel order are the service account's SPI settings (`/info`
`windows.spi`); they cannot be set per request. The font is loaded with `AddFontMemResourceEx`
(private, in memory) under the family `F_<sha256[:16]>`. The `name` table is rewritten and `STAT`
is dropped: GDI names the faces of a font with `fvar` from STAT, so a partial instance whose
default is not "Regular" would not be found. All other tables are passed through byte for byte.

How the modes relate to Chromium `/render` (measured on the maintainers' Windows 11 server with hinted statics). The bytes
always differ, because each engine blends differently, so the comparison depends on the metric:

| Mode | Pixels with max per-channel \|Δ\| > 64 | Channels binarized at 64, masks compared |
|------|------------------------------------|------------------------------------------|
| `dwrite` `natural_symmetric` (defaults) | 0 | ~1.5% of ink |
| `gdi` `cleartype_natural` | 0 | ~4.5% (bold) to ~19% (DejaVu Sans) |
| `dwrite` `natural` | a few hundred over 160 glyph×ppem | ~5% |
| `gdi` `cleartype` | a few hundred | ~13% |

Chromium on this host is closest to `dwrite` `natural_symmetric` with default params. In `cells`,
`gdi_natural` and `natural` give identical pixels: they hint the same way and differ only in glyph
positioning (whole vs subpixel), which shows in `run`.

Identical requests give byte-identical PNGs. Response:

```json
{"image": "<base64 PNG>",
 "geometry": {"engine": "gdi", "render_version": 1, "upem": 2048,
   "params": {"mode": "cleartype", "layout": "cells", "gap": 4, "background": "#ffffff", "ink": "#000000"},
   "gasp": [{"max_ppem": 65535, "flags": 15}],
   "rows": [{"ppem": 12, "baseline_y": 31, "box": [x0, y0, x1, y1], "ascent": 11, "descent": 3,
     "glyphs": [{"gid": 40, "x0": 16, "advance": 7, "abc": [1, 5, 1], "design_advance": 1233,
                 "box": [x0, y0, x1, y1]}]}]}}
```

| Field | Meaning |
|-------|---------|
| `rows[].ppem` | The ppem actually rendered |
| `rows[].rendering_mode`, `rows[].grid_fit` | dwrite: the rendering mode used (for `default`: what `IDWriteFontFace3::GetRecommendedRenderingMode` picked from size and `gasp`, and the run is drawn with exactly that mode and grid fit) |
| `gasp` | The font's `gasp` ranges `[{"max_ppem": 8, "flags": 2}, …]`, or `null` if the font has no table, so you can check what the engines saw |
| `rows[].ascent`, `descent` | The engine's font metrics at this size (GDI: `tmAscent`/`tmDescent`); the row is at least this tall and also fits all ink |
| `glyphs[].x0` | Pen origin on the baseline, in image pixels. Always an integer in `cells`; in `run` it is fractional for DirectWrite natural measuring |
| `glyphs[].advance` | The advance the engine itself uses. GDI: A+B+C from `GetCharABCWidthsI` (`abc`), so compatible widths show here. dwrite `gdi_classic`/`gdi_natural` measuring: `GetGdiCompatibleGlyphMetrics` scaled and rounded. dwrite `natural`: design advance × ppem / upem |
| `glyphs[].design_advance` | `hmtx` advance in font units (`upem`) |
| `glyphs[].box` | Ink box (pixels that differ from the background), in image pixels; `[x0, baseline, x0, baseline]` for a glyph without ink. In `run` layout it is the ink of the glyph drawn alone |

Errors: `400` bad parameters, or `gid_out_of_range` with `max`; `409` `missing_fonts`; `422`
font not readable or rejected by the engine (`font_not_selected`, `dwrite_*`); `429` busy; `501` engine not available on this host;
`504` timeout.

## POST /fonts

Caches fonts in server RAM for the calling client. Multipart `fonts` (1–20); an empty file is
refused with `400`.

```json
{"fonts": [{"sha256": "<hex>", "size": 123456}]}
```

In upload order. Each client (token) has its own namespace; the cache is bounded by total
size (LRU) and an idle TTL, so a later `/render` may answer `409` — just upload again.

## DELETE /fonts

Drops all cached fonts of the calling client: `{"deleted": n}`.

## GET /info

Server version and capabilities. Fields clients rely on:

| Field | Meaning |
|-------|---------|
| `version`, `branch`, `commit_date` | Deployed git commit; in an installed layout `version` is the version directory (`1.2.0`, `dev-a1b2c3d`) and `branch` is `installed` |
| `release` | Package version (`pyproject.toml`), e.g. `1.2.0` |
| `layout` | `checkout` (git working copy) or `installed` ([distribution.md](distribution.md)) |
| `dev_mode` | `{"enabled": bool, "until": "<time>"\|null}` — developer mode ([deployment.md](deployment.md#developer-mode)) |
| `platform`, `python` | Host OS and the server's Python version |
| `browsers` | `{supported, installed, active}` |
| `render_version` | Bumped when rendered pixels can change — use it (with `browser_version` and `platform`) as a render-cache key |
| `browser_version` | e.g. `{"chromium": "145.0.7632.6"}` |
| `api` | `2` = font cache available; `3` = `engines`, `render_versions`, `windows` |
| `engines` | Render engines this host runs: `["chromium", "gdi", "dwrite"]`; the native ones serve `POST /glyphs` |
| `render_versions` | Per engine, e.g. `{"chromium": 2, "gdi": 1, "dwrite": 1}`; bumped when that engine's pixels can change |
| `windows` | Everything on the host that changes pixels, for the service account (below); `null` off Windows |
| `auth` | `{"required": bool, "client": "<id>"\|null}` — who the server thinks you are |
| `font_cache` | `{enabled, max_bytes, ttl_s, client: {fonts, bytes}}` |
| `limits` | `{max_fonts, max_font_size, max_ppem_values}` |
| `pool` | `{browsers, concurrency, busy, queued, queue_max, client_max, timeout_s, recycle_after, live, page_cache}` |
| `render.geometry` | `true` = geometry mode supported |

### `windows`

| Field | Meaning |
|-------|---------|
| `build`, `display_version` | Windows build with UBR (`10.0.26100.6899`), e.g. `24H2` — the rasterizer changes with updates |
| `dll` | File versions of `dwrite.dll`, `gdi32full.dll`, `win32kfull.sys` |
| `account` | User the service runs as (`SYSTEM` for the installed service); the settings below belong to this account |
| `spi` | `SystemParametersInfo` font smoothing: `font_smoothing`, `smoothing_type` (1 standard, 2 ClearType), `smoothing_contrast` (1000–2200), `smoothing_orientation` (0 BGR, 1 RGB). GDI reads these |
| `cleartype_registry` | Raw ClearType Tuner values, `{"hkcu"\|"hklm": {"DISPLAY1": {GammaLevel, EnhancedContrastLevel, ClearTypeLevel, PixelStructure, ...}}}`; missing = Windows defaults. Chromium (since 132) follows the Tuner's contrast and gamma on a desktop, but in the service (session 0) these keys change nothing: tested on lurkbox with extreme values, all engines stayed byte-identical |
| `dwrite_default_params` | `IDWriteFactory::CreateRenderingParams`: `gamma`, `enhanced_contrast`, `grayscale_enhanced_contrast`, `cleartype_level`, `pixel_geometry`, `rendering_mode` |

A part that cannot be read is `{"error": "..."}`. For a Chromium render-cache key use
`render_versions.chromium`, `browser_version`, `windows.build`, `windows.spi` and
`windows.cleartype_registry`.

## GET /

Service name, running version (`version`, `branch`, `commit_date` as in `/info`) and a short
list of endpoints. Needs a token like `/info` while anonymous rendering is off.

## GET /health

Public: `{"status": "ok", "version": "<commit or version directory>", "release": "<package version>"}`.

## Admin endpoints

All are `POST` with form fields; authenticate with an admin Bearer token or the legacy
`token` field.

| Endpoint | Fields | Effect |
|----------|--------|--------|
| `/update` | `version` (optional, installed only) | Installed: start the update job to `version` or the newest release of the channel → `202 {"status": "started", "old_version", "new_version", "job"}`, or `{"status": "up-to-date"}`; follow it with `GET /admin/api/update-status`; it ends with a restart ([distribution.md](distribution.md#updates)). Checkout: `git pull` → `uv sync` → compile check (rollback on error) → restart |
| `/rollback` | — | Installed: switch to the previous version → restart (`{"version"}`). Checkout: reset to the commit saved in `.last_good_commit` → restart |
| `/browsers/install` | `browser` | `playwright install <browser>` as the service user |
| `/admin/clients/list` | — | Clients and settings |
| `/admin/clients/add` | `client_id`, `scopes` (`render`, `admin`) | New client; the token is returned once |
| `/admin/clients/{rotate,disable,enable,remove}` | `client_id` | Manage a client (`rotate` returns a new token) |
| `/admin/settings` | `allow_anonymous_render`, `legacy_admin_token` | Toggle auth settings |

Developer mode (installed server, `dev` scope; `404` while the mode is off, expired or the
caller is outside `allow_from` — [deployment.md](deployment.md#developer-mode)):

| Endpoint | Fields | Effect |
|----------|--------|--------|
| `GET /dev/status` | — | Mode, current / previous version, the job |
| `POST /dev/deploy` | `archive` (zip ≤ 50 MB), `commit`, `dirty` | `202`, job like `/update`: install into `versions\dev-<commit[:7]>[-dirty-<hash>]`, restart; archives with font files are refused |
| `POST /dev/release` | — | Back to the newest release on disk, or install the newest of the channel |

Use `uv run font-render-clients ...` instead of calling these by hand ([auth.md](auth.md)).
Changing `legacy_admin_token` or disabling/removing a client answers `409` when it would leave
no way to log in as admin.

The web admin (`/admin/`) and its endpoints (`/admin/api/*`, restart, recycle browsers, clear
the font cache) are described in [admin.md](admin.md).
