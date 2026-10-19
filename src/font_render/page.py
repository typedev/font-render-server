"""HTML page for a render: templates, @font-face generation, waterfall content."""

import base64
import hashlib
import html
import re
from dataclasses import dataclass

_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{3,8}$")
BG_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}){1,2}$")

# The page is assembled from three parts so a loaded page can be reused: the
# @font-face block (#ff) stays, the rest of the CSS (#st) and the body are swapped.
PAGE_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style id="ff">
{font_faces}
</style>
<style id="st">
{style}</style>
</head>
<body>{body}</body>
</html>"""

STYLE_TEMPLATE = """* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
    background: {bg_color};
    padding: 16px;
    width: fit-content;
    -webkit-font-smoothing: antialiased;
}}
.header {{
    font-family: Consolas, monospace;
    font-size: 11px;
    color: #888;
    margin-bottom: 12px;
    border-bottom: 1px solid #ddd;
    padding-bottom: 8px;
}}
.row {{
    display: flex;
    align-items: baseline;
    margin: 2px 0;
    white-space: nowrap;
}}
.label {{
    font-family: Consolas, monospace;
    font-size: 10px;
    color: #999;
    width: 70px;
    flex-shrink: 0;
    text-align: right;
    padding-right: 12px;
}}
{sample_classes}
.mode-interleaved .pair {{
    margin-bottom: 6px;
    border-bottom: 1px solid #f0f0f0;
    padding-bottom: 6px;
}}
.mode-interleaved .pair:last-child {{
    border-bottom: none;
}}
{extra_css}
"""

BODY_TEMPLATE = """
<div class="header">
{header}
</div>
<div class="content mode-interleaved">
{content}
</div>
"""


STYLE_TEMPLATE_PLAIN = """* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
    background: {bg_color};
    padding: 12px;
    width: fit-content;
    -webkit-font-smoothing: antialiased;
}}
.row {{
    display: flex;
    align-items: baseline;
    margin: 2px 0;
    white-space: nowrap;
}}
{sample_classes}
.mode-interleaved .pair {{
    margin-bottom: 6px;
    border-bottom: 1px solid #f0f0f0;
    padding-bottom: 6px;
}}
.mode-interleaved .pair:last-child {{
    border-bottom: none;
}}
{extra_css}
"""

BODY_TEMPLATE_PLAIN = """
<div class="content mode-interleaved">
{content}
</div>
"""


_FORMAT_MAP = {
    ".ttf": "truetype",
    ".otf": "opentype",
    ".woff": "woff",
    ".woff2": "woff2",
}

_MAGIC_FORMATS = {
    b"wOFF": "woff",
    b"wOF2": "woff2",
    b"OTTO": "opentype",
    b"\x00\x01\x00\x00": "truetype",
    b"true": "truetype",
}


def _font_format(ext: str, data: bytes) -> str:
    """CSS format() hint: sniffed from the file signature, extension as fallback."""
    return _MAGIC_FORMATS.get(data[:4]) or _FORMAT_MAP.get(ext, "truetype")


def _font_families(font_data: list[tuple[str, bytes]]) -> list[str]:
    """CSS family names that embed a content hash.

    An edited font (new bytes) always gets a new family name, so Blink's
    in-process font cache can never match an older version by name.
    The same font listed twice (e.g. one row per variation location) shares one family, so
    its @font-face is embedded once.
    """
    seen: dict[str, str] = {}
    out = []
    for i, (_ext, data) in enumerate(font_data, 1):
        sha = hashlib.sha256(data).hexdigest()
        out.append(seen.setdefault(sha, f"F{i}_{sha[:16]}"))
    return out


def _generate_font_faces(font_data: list[tuple[str, bytes]], families: list[str]) -> str:
    """Generate @font-face declarations with base64 data URIs."""
    parts, done = [], set()
    for (ext, data), family in zip(font_data, families):
        if family in done:
            continue
        done.add(family)
        fmt = _font_format(ext, data)
        b64 = base64.b64encode(data).decode("ascii")
        parts.append(
            f"@font-face {{\n"
            f"    font-family: '{family}';\n"
            f"    src: url('data:application/octet-stream;base64,{b64}') format('{fmt}');\n"
            f"    font-display: block;\n"
            f"}}"
        )
    return "\n".join(parts)


def _sanitize_color(color: str) -> str:
    """Validate and return a CSS hex color, fallback to black."""
    color = color.strip()
    if _COLOR_RE.match(color):
        return color
    return "#000000"


def is_dark(color: str) -> bool:
    """True for a #rgb / #rrggbb color darker than mid-gray (relative luminance)."""
    h = color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b < 0.5


# Separators and labels for a dark background; the defaults are tuned for white
DARK_BG_CSS = """.header { border-bottom-color: #444; }
.label { color: #888; }
.mode-interleaved .pair { border-bottom-color: #222; }
"""


def _css_number(v: float) -> str:
    return f"{v:.6f}".rstrip("0").rstrip(".")


def font_variation_settings(location: dict[str, float]) -> str:
    """'"wdth" 87, "wght" 350.5' — every axis explicit, so font-weight, font-stretch and
    automatic optical sizing cannot move the location."""
    return ", ".join(f'"{tag}" {_css_number(v)}' for tag, v in location.items())


def _generate_sample_classes(families: list[str], colors: list[str],
                             variations: list[dict | None] | None = None) -> str:
    """Generate .sample-1 .. .sample-N CSS classes."""
    parts = []
    for i, family in enumerate(families, 1):
        color = _sanitize_color(colors[i - 1] if i - 1 < len(colors) else "#000000")
        location = variations[i - 1] if variations and i - 1 < len(variations) else None
        fvs = (f"    font-variation-settings: {font_variation_settings(location)};\n"
               if location else "")
        parts.append(
            f".sample-{i} {{\n"
            f"    font-family: '{family}', sans-serif;\n"
            f"    color: {color};\n"
            f"{fvs}"
            f"}}"
        )
    return "\n".join(parts)


def _generate_content(
    ppem_values: list[int], text: str, num_fonts: int,
) -> str:
    """Generate interleaved waterfall content for N fonts."""
    safe_text = html.escape(text)
    rows = []
    if num_fonts == 1:
        # Single font — no pair wrappers, just rows
        for ppem in ppem_values:
            rows.append(
                f'<div class="row" data-ppem="{ppem}" data-font="1">'
                f'  <span class="label">{ppem}px</span>'
                f'  <span class="sample-1" style="font-size:{ppem}px">{safe_text}</span>'
                f'</div>'
            )
    else:
        for ppem in ppem_values:
            rows.append('<div class="pair">')
            for i in range(1, num_fonts + 1):
                rows.append(
                    f'  <div class="row" data-ppem="{ppem}" data-font="{i}">'
                    f'    <span class="label">{ppem}px {i}</span>'
                    f'    <span class="sample-{i}" style="font-size:{ppem}px">{safe_text}</span>'
                    f'  </div>'
                )
            rows.append('</div>')
    return "\n".join(rows)


def _generate_content_plain(
    ppem_values: list[int], text: str, num_fonts: int,
) -> str:
    """Generate plain waterfall content — no labels, no pair borders."""
    safe_text = html.escape(text)
    rows = []
    if num_fonts == 1:
        for ppem in ppem_values:
            rows.append(
                f'<div class="row" data-ppem="{ppem}" data-font="1">'
                f'  <span class="sample-1" style="font-size:{ppem}px">{safe_text}</span>'
                f'</div>'
            )
    else:
        for ppem in ppem_values:
            rows.append('<div class="pair">')
            for i in range(1, num_fonts + 1):
                rows.append(
                    f'  <div class="row" data-ppem="{ppem}" data-font="{i}">'
                    f'    <span class="sample-{i}" style="font-size:{ppem}px">{safe_text}</span>'
                    f'  </div>'
                )
            rows.append('</div>')
    return "\n".join(rows)


@dataclass
class PageParts:
    """A render page split for reuse: fonts (#ff) stay, style (#st) and body swap."""

    font_data: list[tuple[str, bytes]]
    families: list[str]
    style: str
    body: str

    @property
    def fonts_key(self) -> tuple[str, ...]:
        """Identifies the @font-face block; families embed a content hash."""
        return tuple(dict.fromkeys(
            f"{family}:{_font_format(ext, data)}"
            for (ext, data), family in zip(self.font_data, self.families)
        ))

    def font_faces(self) -> str:
        return _generate_font_faces(self.font_data, self.families)

    def html(self) -> str:
        return PAGE_TEMPLATE.format(font_faces=self.font_faces(), style=self.style, body=self.body)


def build_page(
    ppem_values: list[int],
    text: str,
    font_names: list[str],
    font_data: list[tuple[str, bytes]],
    colors: list[str],
    cleartype: bool,
    scale: float = 1.0,
    bg_color: str = "white",
    browser: str = "chromium",
    layout: str = "full",
    zoom: int = 1,
    line_height: float = 0,
    variations: list[dict | None] | None = None,
) -> PageParts:
    """variations: per font, a resolved location {tag: value} (all fvar axes) or None."""
    num_fonts = len(font_names)
    families = _font_families(font_data)
    sample_classes = _generate_sample_classes(families, colors, variations)
    # line_height > 0 overrides the browser default; spreads waterfall rows
    # apart so ink (accents, descenders) never crosses row boundaries
    extra_css = f".row {{ line-height: {line_height}; }}" if line_height > 0 else ""
    if bg_color != "white" and is_dark(bg_color):
        extra_css = DARK_BG_CSS + extra_css
    style_args = dict(bg_color=bg_color, sample_classes=sample_classes, extra_css=extra_css)

    if layout == "plain":
        content = _generate_content_plain(ppem_values, text, num_fonts)
        return PageParts(
            font_data, families,
            STYLE_TEMPLATE_PLAIN.format(**style_args),
            BODY_TEMPLATE_PLAIN.format(content=content),
        )

    content = _generate_content(ppem_values, text, num_fonts)

    ppem_list = ", ".join(str(p) for p in ppem_values)

    # Header line 1: settings
    ct_part = f" | ClearType: {'on' if cleartype else 'off'}" if browser == "chromium" else ""
    zoom_part = f" | Zoom: {zoom}x" if zoom > 1 else ""
    header = f"    Scale: {scale}{ct_part} | Browser: {browser}{zoom_part}<br>\n"
    # Header line 2: PPEM
    header += f"    PPEM: {ppem_list}"
    # Font names (for 2+ fonts, numbered list)
    if num_fonts == 1:
        header = f"    Font: <strong>{html.escape(font_names[0])}</strong> | " + header.lstrip()
    else:
        font_lines = "".join(
            f"<br>\n    {i}: <strong>{html.escape(name)}</strong>"
            for i, name in enumerate(font_names, 1)
        )
        header += font_lines

    return PageParts(
        font_data, families,
        STYLE_TEMPLATE.format(**style_args),
        BODY_TEMPLATE.format(header=header, content=content),
    )


def build_html(*args, **kwargs) -> str:
    """Full HTML document for the render page (see build_page)."""
    return build_page(*args, **kwargs).html()



def parse_ppem_range(ppem_str: str) -> list[int]:
    """Parse PPEM specification: '9-24' or '9,10,12,14,16,20,24'."""
    ppem_str = ppem_str.strip()
    if "-" in ppem_str and "," not in ppem_str:
        parts = ppem_str.split("-")
        start, end = int(parts[0]), int(parts[1])
        return list(range(start, end + 1))
    else:
        return sorted(int(x.strip()) for x in ppem_str.split(","))
