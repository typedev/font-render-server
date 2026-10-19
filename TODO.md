# TODO

## font-render-bot: moved to lurkbox (done 2026-10-14)

The bot runs on lurkbox as NSSM service `FontRenderBot` from its own checkout and is
maintained in its own repo (https://github.com/typedev/font-render-bot, `CLAUDE.md` there,
own admin API + `bot_admin.py`). Its open items live there. From this repo's side it is just
client `telegram-bot`.

## Rows land on fractional device pixels (finding, unresolved)

At non-integer scales (e.g. 1.5) waterfall rows can start at fractional device-pixel
offsets: the 1px `.pair` border becomes 1.5 device px, and `line-height: normal` gives
font-dependent fractional row heights (even at scale 1.0). A row at y+0.5 rasterizes
differently, so on Linux/FreeType a row is NOT byte-identical across requests with
different ppem sets. hintbench verified identity on Windows (DirectWrite probably snaps the
baseline vertically), but there is no guarantee. hintbench informed 2026-10-13 and re-checked
on lurkbox with its exact params (plain, line_height=2, ClearType, geometry, font_ids; scales
1/1.25/1.5/2; 1–2 fonts): 0 of 684 row cells differ across ppem sets — it keeps fetching only
missing sizes and does not need S5.

Tried: shifting each row down by < 1 device px (`transform: translateY`) so its top is on a
whole device pixel — changes pixels of all existing renders (needs a `RENDER_VERSION` bump,
invalidates client caches) and still left 10 of 32 cells different with 2 fonts at 1.5.
Not pursued.

## S5 (shelved): several texts per request

hintbench asked for `texts` (one row per text, each starting at the line origin) to fetch
12–20 glyphs × all ppem in one request. Shelved: per-glyph requests are already cheap
(~77 ms, font_ids + page reuse, 4 in flight, background prefetch), and per-row layout runs
into the fractional-pixel problem above. If the request count becomes a real bottleneck,
prefer a batch endpoint: one HTTP request carrying a list of render jobs, each rendered as a
normal single render on the reused page — byte-identical by construction, no
`RENDER_VERSION` change.

## GDI engine for bi-level / gasp proofs (declined for now)

hintbench asked for bi-level (aliased) renders and "follow gasp". Measured on lurkbox
2026-10-13: Chromium ignores the gasp table entirely (4 gasp variants of DejaVuSans → byte-identical
PNGs with and without ClearType) and has no per-request aliased mode on Windows. A second
rasterizer via Windows GDI (AddFontMemResourceEx, lfQuality NONANTIALIASED/ANTIALIASED/CLEARTYPE/
DEFAULT=follow gasp) would work, but shows an environment real users barely have. hintbench uses
its own FreeType mono engine for these proofs. Revisit only for legacy-GDI targets or exact
VTT-proof parity: start with a prototype (engine=gdi, plain layout) and check gasp under ClearType
against VTT. Open issues: family-name collisions (rename in memory), WOFF/WOFF2 decompression, no
shaping, ClearType params from the service account.

## Firefox ClearType (tried, not possible headless)

Headless Firefox on lurkbox renders grayscale only, although the service account's system
setting is ClearType (RGB, contrast 1200; see `windows_text` in /admin/api/status). Prefs tried
via /admin/api/firefox-prefs, none produced color fringes: cleartype_params.cleartype_level /
rendering_mode (2, 3, 5 — change the output, still gray), gfx.webrender.software on/off,
compositor off, layers.acceleration.disabled, browser.display.background_color, GDI table
loading, subpixel-position off. Likely the headless widget / snapshot path. A headful Firefox in
the service's non-interactive session might differ — untried.
