"""Shared types of the native engines (gdi.py, later dwrite): cells, errors, ink boxes.

An engine renders on its own worker thread (glyphs.py) and offers:
  name, render_version, parse_params(form) -> params (ValueError = 400), ppem_ok(ppem) -> bool
  cells(font, gids, ppem, params, background, ink) -> (ascent, descent, [Cell])
  run(font, gids, ppem, params, background, ink) -> (image, pen_x, baseline_y, [pen x per glyph])
"""

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
