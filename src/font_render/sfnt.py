"""Font preparation for the native engines (GDI, DirectWrite): in memory only.

GDI selects a loaded font by family name, so two uploads with the same family would collide.
prepare() renames the family to F_<sha256[:16]> in the `name` table and drops `STAT`: for a
font with `fvar` (e.g. a partial instance), GDI names its faces from STAT, and a default
location that is not elidable ("Light") gives a face name other than F_<sha>, so the font
is never selected. STAT only names things; it does not affect rasterization. The font is
opened lazily and every other table is copied byte-for-byte (hinting, metrics, outlines and
fvar/gvar untouched). WOFF/WOFF2 come out as plain sfnt.
"""

import io
from collections import OrderedDict
from dataclasses import dataclass

from fontTools.ttLib import TTFont

_DROP_NAME_IDS = {1, 2, 3, 4, 6, 16, 17, 18, 21, 22, 25}
_CACHE_MAX = 32


class FontError(ValueError):
    """The font cannot be parsed (answered as 422)."""


Axis = tuple[str, float, float, float]  # (tag, min, default, max) from fvar


@dataclass(frozen=True)
class PreparedFont:
    sha: str
    face: str                 # family name the engine selects, F_<sha256[:16]>
    data: bytes               # sfnt with the renamed family
    num_glyphs: int
    upem: int
    advances: tuple[int, ...]  # design advance width per glyph id (hmtx), font units
    axes: tuple[Axis, ...]     # empty for a static font
    gasp: tuple[tuple[int, int], ...] | None = None  # (max ppem, flags) ranges; None = no table

    @property
    def is_variable(self) -> bool:
        return bool(self.axes)


_cache: OrderedDict[str, PreparedFont] = OrderedDict()


def face_name(sha: str) -> str:
    return f"F_{sha[:16]}"


def prepare(data: bytes, sha: str) -> PreparedFont:
    """Renamed sfnt + the metrics the endpoints need; cached by content hash."""
    if (hit := _cache.get(sha)) is not None:
        _cache.move_to_end(sha)
        return hit
    face = face_name(sha)
    try:
        info = TTFont(io.BytesIO(data), lazy=True, fontNumber=0)
        order = info.getGlyphOrder()
        hmtx = info["hmtx"]
        advances = tuple(hmtx[g][0] for g in order)
        upem = info["head"].unitsPerEm
        axes = _fvar_axes(info)
        gasp = tuple(sorted(info["gasp"].gaspRange.items())) if "gasp" in info else None

        font = TTFont(io.BytesIO(data), lazy=True, fontNumber=0,
                      recalcBBoxes=False, recalcTimestamp=False)
        font.flavor = None
        name = font["name"]
        name.names = [r for r in name.names if r.nameID not in _DROP_NAME_IDS]
        for name_id, value in ((1, face), (2, "Regular"), (3, face), (4, face), (6, face)):
            name.setName(value, name_id, 3, 1, 0x409)
        if "STAT" in font:
            del font["STAT"]
        out = io.BytesIO()
        font.save(out, reorderTables=False)
    except Exception as e:  # fontTools raises many types on broken input
        raise FontError(f"{type(e).__name__}") from e
    prepared = PreparedFont(sha, face, out.getvalue(), len(order), upem, advances, axes, gasp)
    _cache[sha] = prepared
    while len(_cache) > _CACHE_MAX:
        _cache.popitem(last=False)
    return prepared


_axes_cache: OrderedDict[str, tuple[Axis, ...]] = OrderedDict()


def _fvar_axes(font: TTFont) -> tuple[Axis, ...]:
    if "fvar" not in font:
        return ()
    return tuple((a.axisTag, float(a.minValue), float(a.defaultValue), float(a.maxValue))
                 for a in font["fvar"].axes)


def font_axes(data: bytes, sha: str) -> tuple[Axis, ...]:
    """fvar axes of a font (empty if static), cached by content hash."""
    if (hit := _axes_cache.get(sha)) is not None:
        _axes_cache.move_to_end(sha)
        return hit
    try:
        axes = _fvar_axes(TTFont(io.BytesIO(data), lazy=True, fontNumber=0))
    except Exception as e:
        raise FontError(f"{type(e).__name__}") from e
    _axes_cache[sha] = axes
    while len(_axes_cache) > _CACHE_MAX:
        _axes_cache.popitem(last=False)
    return axes


def parse_location(s: str) -> dict[str, float]:
    """'wght=350.5, wdth=87' -> {'wght': 350.5, 'wdth': 87.0} (ValueError on bad input)."""
    out = {}
    for part in (p.strip() for p in s.split(",")):
        if not part:
            continue
        tag, sep, value = part.partition("=")
        tag = tag.strip()
        if not sep or not (1 <= len(tag) <= 4 and tag.isascii() and tag.isprintable()):
            raise ValueError(f"bad axis {part!r}")
        v = float(value)
        if v != v or v in (float("inf"), float("-inf")):
            raise ValueError(f"bad value {part!r}")
        out[tag] = v
    return out


def resolve_location(axes: tuple[Axis, ...], requested: dict[str, float]) -> dict[str, float]:
    """Every fvar axis in fvar order: the requested value clamped to the axis range, else the
    default. ValueError for a tag the font does not have."""
    known = {a[0] for a in axes}
    if unknown := sorted(set(requested) - known):
        raise ValueError(f"axes not in fvar: {', '.join(unknown)}")
    return {tag: min(max(requested.get(tag, default), lo), hi) for tag, lo, default, hi in axes}
