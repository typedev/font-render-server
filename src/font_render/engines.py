"""Shared types of the native engines (gdi.py, later dwrite): cells, errors, ink boxes.

An engine renders on its own worker thread (glyphs.py) and offers:
  name, render_version, parse_params(form) -> params (ValueError = 400), ppem_ok(ppem) -> bool
  cells(font, gids, ppem, params, background, ink) -> (ascent, descent, [Cell])
  run(font, gids, ppem, params, background, ink, positions=None)
      -> (image, pen_x, baseline_y, [pen x per glyph], [advance], [(dx, dy)] | None)
  positions: Positions from a shaper (font units) or None for the engine's own advances; the
  returned offsets are in pixels, dy up, the glyph origin is (pen_x + pen + dx, baseline_y - dy).
"""

import math
from dataclasses import dataclass

from PIL import Image, ImageChops


class RenderError(Exception):
    """The engine refused the font or a call failed; answered as 422 with this code."""


class Cell:
    """One glyph drawn alone: its ink (cropped) and the ink's offset from the pen origin on the
    baseline. `img` is None for a glyph without ink; `advance` is the engine's own advance."""

    __slots__ = ("gid", "img", "left", "top", "advance", "extra")

    def __init__(self, gid: int, img: Image.Image | None, left: int, top: int, advance: float,
                 extra: dict | None = None):
        self.gid, self.img, self.left, self.top = gid, img, left, top
        self.advance, self.extra = advance, extra or {}

    @classmethod
    def from_canvas(cls, gid, img, background, ox, oy, advance, extra=None) -> "Cell":
        """Crop `img` (pen origin at ox, oy) to the pixels that differ from the background."""
        box = ink_box(img, background)
        if box is None:
            return cls(gid, None, 0, 0, advance, extra)
        return cls(gid, img.crop(box), box[0] - ox, box[1] - oy, advance, extra)


def ink_box(img: Image.Image, background) -> tuple[int, int, int, int] | None:
    return ImageChops.difference(img, Image.new("RGB", img.size, background)).getbbox()


@dataclass
class Positions:
    """A shaped run's positions in font units (HarfBuzz x_advance, x_offset/y_offset; y up).
    offsets is None when only advances were given."""

    advances: list[float]
    offsets: list[tuple[float, float]] | None


def place(font, gids, ppem: float, own: list[float], positions: Positions, whole: bool):
    """Device advances and offsets for a shaped run, as a shaper places it on a hinted run: the
    engine's own advance plus the shaper's adjustment (given advance - hmtx advance) scaled to
    ppem, and the offsets scaled. whole: rounded to whole pixels (GDI, DirectWrite GDI measuring).
    -> ([advance px], [(dx, dy) px] | None)"""
    scale = ppem / font.upem
    snap = (lambda v: float(math.floor(v + 0.5))) if whole else (lambda v: v)
    advances = [o + snap((a - font.advances[g]) * scale)
                for g, o, a in zip(gids, own, positions.advances)]
    offsets = None if positions.offsets is None else [
        (snap(dx * scale), snap(dy * scale)) for dx, dy in positions.offsets]
    return advances, offsets


def pens(advances: list[float]) -> list[float]:
    """Pen x of each glyph: cumulative advances from 0."""
    out, x = [], 0.0
    for a in advances:
        out.append(x)
        x += a
    return out
