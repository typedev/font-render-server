"""GDI rasterizer through ctypes (Windows only): ExtTextOutW with ETO_GLYPH_INDEX into a DIB.

Fonts are loaded with AddFontMemResourceEx (private to the process, not enumerable, never on
disk) under the renamed family from sfnt.prepare(). All calls run on one worker thread
(glyphs.py), so the loaded-font cache needs no lock.
"""

import ctypes
import sys
from collections import OrderedDict
from ctypes import POINTER, byref, c_int, c_uint, c_ushort, c_void_p
from ctypes import wintypes

from PIL import Image

from .engines import Cell, Positions, RenderError, ink_box, pens, place
from .sfnt import PreparedFont

MODES = {"bw": 3, "gray": 4, "cleartype": 5, "cleartype_natural": 6}  # lfQuality
ETO_GLYPH_INDEX = 0x0010
ETO_PDY = 0x2000
TRANSPARENT = 1
TA_BASELINE = 24
FONTS_MAX = 32


class LOGFONTW(ctypes.Structure):
    _fields_ = [("lfHeight", wintypes.LONG), ("lfWidth", wintypes.LONG),
                ("lfEscapement", wintypes.LONG), ("lfOrientation", wintypes.LONG),
                ("lfWeight", wintypes.LONG), ("lfItalic", wintypes.BYTE),
                ("lfUnderline", wintypes.BYTE), ("lfStrikeOut", wintypes.BYTE),
                ("lfCharSet", wintypes.BYTE), ("lfOutPrecision", wintypes.BYTE),
                ("lfClipPrecision", wintypes.BYTE), ("lfQuality", wintypes.BYTE),
                ("lfPitchAndFamily", wintypes.BYTE), ("lfFaceName", wintypes.WCHAR * 32)]


class TEXTMETRICW(ctypes.Structure):
    _fields_ = [(n, wintypes.LONG) for n in (
        "tmHeight", "tmAscent", "tmDescent", "tmInternalLeading", "tmExternalLeading",
        "tmAveCharWidth", "tmMaxCharWidth", "tmWeight", "tmOverhang",
        "tmDigitizedAspectX", "tmDigitizedAspectY")] + [
        (n, wintypes.WCHAR) for n in ("tmFirstChar", "tmLastChar", "tmDefaultChar", "tmBreakChar")] + [
        (n, wintypes.BYTE) for n in ("tmItalic", "tmUnderlined", "tmStruckOut",
                                     "tmPitchAndFamily", "tmCharSet")]


class ABC(ctypes.Structure):
    _fields_ = [("abcA", c_int), ("abcB", c_uint), ("abcC", c_int)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 1)]


_gdi = None


def _api():
    """gdi32 with explicit signatures (64-bit handles must not be truncated to int)."""
    global _gdi
    if _gdi is not None:
        return _gdi
    g = ctypes.WinDLL("gdi32", use_last_error=True)
    H = c_void_p
    sig = {
        "CreateCompatibleDC": (H, [H]),
        "DeleteDC": (wintypes.BOOL, [H]),
        "CreateDIBSection": (H, [H, POINTER(BITMAPINFO), c_uint, POINTER(c_void_p), H, wintypes.DWORD]),
        "SelectObject": (H, [H, H]),
        "DeleteObject": (wintypes.BOOL, [H]),
        "AddFontMemResourceEx": (H, [c_void_p, wintypes.DWORD, c_void_p, POINTER(wintypes.DWORD)]),
        "RemoveFontMemResourceEx": (wintypes.BOOL, [H]),
        "CreateFontIndirectW": (H, [POINTER(LOGFONTW)]),
        "GetTextFaceW": (c_int, [H, c_int, wintypes.LPWSTR]),
        "GetTextMetricsW": (wintypes.BOOL, [H, POINTER(TEXTMETRICW)]),
        "SetTextColor": (wintypes.DWORD, [H, wintypes.DWORD]),
        "SetBkMode": (c_int, [H, c_int]),
        "SetTextAlign": (c_uint, [H, c_uint]),
        "ExtTextOutW": (wintypes.BOOL, [H, c_int, c_int, c_uint, c_void_p, c_void_p, c_uint, c_void_p]),
        "GetCharABCWidthsI": (wintypes.BOOL, [H, c_uint, c_uint, POINTER(c_ushort), POINTER(ABC)]),
        "GetTextExtentExPointI": (wintypes.BOOL, [H, POINTER(c_ushort), c_int, c_int,
                                                  POINTER(c_int), POINTER(c_int), POINTER(wintypes.SIZE)]),
        "GdiFlush": (wintypes.BOOL, []),
    }
    for name, (res, args) in sig.items():
        fn = getattr(g, name)
        fn.restype, fn.argtypes = res, args
    _gdi = g
    return g


# sha -> (AddFontMemResourceEx handle, the bytes it was loaded from)
_loaded: OrderedDict[str, tuple[int, bytes]] = OrderedDict()


def _load(font: PreparedFont):
    g = _api()
    if font.sha in _loaded:
        _loaded.move_to_end(font.sha)
        return
    count = wintypes.DWORD(0)
    buf = ctypes.create_string_buffer(font.data, len(font.data))
    handle = g.AddFontMemResourceEx(buf, len(font.data), None, byref(count))
    if not handle or not count.value:
        raise RenderError("font_rejected")
    _loaded[font.sha] = (handle, buf)
    while len(_loaded) > FONTS_MAX:
        _sha, (old, _buf) = _loaded.popitem(last=False)
        g.RemoveFontMemResourceEx(old)


def available() -> bool:
    return sys.platform == "win32"


def _colorref(rgb: tuple[int, int, int]) -> int:
    r, gr, b = rgb
    return r | (gr << 8) | (b << 16)


class _Canvas:
    """A memory DC with a 32-bpp top-down DIB and the font for one (ppem, mode) selected."""

    def __init__(self, font: PreparedFont, ppem: int, mode: str, width: int, height: int,
                 background, ink):
        g = _api()
        self.g, self.w, self.h = g, width, height
        self.dc = g.CreateCompatibleDC(None)
        bmi = BITMAPINFO()
        hdr = bmi.bmiHeader
        hdr.biSize, hdr.biWidth, hdr.biHeight = ctypes.sizeof(BITMAPINFOHEADER), width, -height
        hdr.biPlanes, hdr.biBitCount, hdr.biCompression = 1, 32, 0
        self.bits = c_void_p()
        self.bmp = g.CreateDIBSection(self.dc, byref(bmi), 0, byref(self.bits), None, 0)
        if not self.bmp:
            g.DeleteDC(self.dc)
            raise MemoryError("CreateDIBSection failed")
        g.SelectObject(self.dc, self.bmp)
        lf = LOGFONTW()
        lf.lfHeight = -ppem            # negative: em height in pixels = ppem
        lf.lfWeight = 0                # FW_DONTCARE: never synthesize bold
        lf.lfCharSet = 1               # DEFAULT_CHARSET
        lf.lfOutPrecision = 4          # OUT_TT_PRECIS
        lf.lfQuality = MODES[mode]
        lf.lfFaceName = font.face
        self.hfont = g.CreateFontIndirectW(byref(lf))
        g.SelectObject(self.dc, self.hfont)
        face = ctypes.create_unicode_buffer(64)
        g.GetTextFaceW(self.dc, 64, face)
        if face.value != font.face:
            self.close()
            raise RenderError("font_not_selected")
        g.SetBkMode(self.dc, TRANSPARENT)
        g.SetTextAlign(self.dc, TA_BASELINE)
        g.SetTextColor(self.dc, _colorref(ink))
        self.fill = bytes((background[2], background[1], background[0], 0)) * (width * height)

    def clear(self):
        self.g.GdiFlush()
        ctypes.memmove(self.bits, self.fill, len(self.fill))

    def draw(self, x: int, y: int, gids: list[int], dx: list[int] | None = None,
             pdy: bool = False):
        """dx: lpDx, the distance to the next glyph's origin per glyph; with pdy, (dx, dy) pairs
        (y down) — how Uniscribe's ScriptTextOut draws glyph offsets."""
        arr = (c_ushort * len(gids))(*gids)
        lpdx = (c_int * len(dx))(*dx) if dx is not None else None
        flags = ETO_GLYPH_INDEX | (ETO_PDY if pdy else 0)
        if not self.g.ExtTextOutW(self.dc, x, y, flags, None, arr, len(gids), lpdx):
            raise RenderError("exttextout_failed")

    def image(self) -> Image.Image:
        self.g.GdiFlush()
        data = ctypes.string_at(self.bits, self.w * self.h * 4)
        return Image.frombuffer("RGB", (self.w, self.h), data, "raw", "BGRX", 0, 1).copy()

    def metrics(self) -> tuple[int, int]:
        tm = TEXTMETRICW()
        self.g.GetTextMetricsW(self.dc, byref(tm))
        return tm.tmAscent, tm.tmDescent

    def abc(self, gids: list[int]) -> list[tuple[int, int, int]]:
        out = (ABC * len(gids))()
        arr = (c_ushort * len(gids))(*gids)
        # pgi given: cgi glyph indices from the array (giFirst is ignored)
        if not self.g.GetCharABCWidthsI(self.dc, 0, len(gids), arr, out):
            raise RenderError("abc_failed")
        return [(x.abcA, x.abcB, x.abcC) for x in out]

    def run_offsets(self, gids: list[int]) -> list[int]:
        """Pen x of each glyph as GDI lays the run out (cumulative extents)."""
        arr = (c_ushort * len(gids))(*gids)
        dx = (c_int * len(gids))()
        fit, size = c_int(), wintypes.SIZE()
        if not self.g.GetTextExtentExPointI(self.dc, arr, len(gids), 0x7FFFFFFF,
                                            byref(fit), dx, byref(size)):
            raise RenderError("extent_failed")
        return [0] + list(dx)[:-1]

    def close(self):
        if self.dc:
            self.g.DeleteDC(self.dc)
            self.g.DeleteObject(self.bmp)
            self.g.DeleteObject(self.hfont)
            self.dc = None


class GdiEngine:
    """Engine protocol for glyphs.py: cells() and run(), sync, on the engine thread."""

    name = "gdi"
    render_version = 1

    def ppem_ok(self, ppem: float) -> bool:
        return ppem == int(ppem)  # GDI sizes are whole pixels (lfHeight)

    def parse_params(self, form: dict) -> dict:
        mode = form.get("mode") or "cleartype"
        if mode not in MODES:
            raise ValueError(f"mode must be one of {', '.join(MODES)}")
        if form.get("axes", "").strip():
            raise ValueError("axes not supported by gdi, send a static instance")
        return {"mode": mode}

    def resolve(self, font: PreparedFont, params: dict) -> dict:
        return params

    def cells(self, font: PreparedFont, gids: list[int], ppem, params: dict, background, ink):
        """(ascent, descent, [Cell]) for one ppem; each glyph drawn alone in a clean canvas."""
        _load(font)
        ppem = int(ppem)
        margin = 2 * ppem + 16
        while True:
            w, h = 5 * ppem + 2 * margin, 4 * ppem + 2 * margin
            canvas = _Canvas(font, ppem, params["mode"], w, h, background, ink)
            try:
                ascent, descent = canvas.metrics()
                abcs = canvas.abc(gids)
                out, clipped = [], False
                for gid, abc in zip(gids, abcs):
                    canvas.clear()
                    canvas.draw(margin, margin + 2 * ppem, [gid])
                    img = canvas.image()
                    box = ink_box(img, background)
                    if box and (box[0] == 0 or box[1] == 0 or box[2] == w or box[3] == h):
                        clipped = True
                        break
                    out.append(Cell.from_canvas(gid, img, background, margin, margin + 2 * ppem,
                                                sum(abc), {"abc": list(abc)}))
            finally:
                canvas.close()
            if not clipped:
                return ascent, descent, out
            if margin > 16 * ppem + 64:
                raise RenderError("glyph_too_large")
            margin *= 2

    def run(self, font: PreparedFont, gids: list[int], ppem, params: dict, background, ink,
            positions: Positions | None = None):
        """The glyphs as one ExtTextOut run: (image, pen_x, baseline_y, [pen x per glyph],
        [advance], [(dx, dy)] | None). With positions: lpDx = own advance + the shaper's
        adjustment, both in whole pixels, as Uniscribe's ScriptPlace + ScriptTextOut."""
        _load(font)
        ppem = int(ppem)
        probe = _Canvas(font, ppem, params["mode"], 1, 1, background, ink)
        try:
            offsets = probe.run_offsets(gids)
            abcs = probe.abc(gids)
        finally:
            probe.close()
        own = [sum(abc) for abc in abcs]
        if positions is not None:
            return self._positioned(font, gids, ppem, params, background, ink, own, positions)
        margin = 2 * ppem + 16
        width = offsets[-1] + max(sum(abcs[-1]), 0) + 2 * margin + 2 * ppem
        height = 4 * ppem + 2 * margin
        canvas = _Canvas(font, ppem, params["mode"], width, height, background, ink)
        try:
            canvas.clear()
            canvas.draw(margin, margin + 2 * ppem, gids)
            return canvas.image(), margin, margin + 2 * ppem, offsets, own, None
        finally:
            canvas.close()

    def _positioned(self, font, gids, ppem: int, params, background, ink, own, positions):
        advances, offsets = place(font, gids, ppem, own, positions, whole=True)
        adv = [int(a) for a in advances]
        off = [(int(dx), int(dy)) for dx, dy in offsets] if offsets else [(0, 0)] * len(gids)
        pen = [int(p) for p in pens(adv)]
        xs = [p + dx for p, (dx, _dy) in zip(pen, off)] + [pen[-1] + adv[-1]]
        reach = max([abs(d) for o in off for d in o] + [0])
        margin = 2 * ppem + 16 + reach
        pen_x = margin - min(xs + [0])
        width = max(xs + [0]) - min(xs + [0]) + 2 * margin + 2 * ppem
        height = 4 * ppem + 2 * margin
        base_y = margin + 2 * ppem
        if offsets is None:
            dx, pdy = adv, False
        else:
            # origin i -> origin i+1: advance + change of offset; GDI's y grows downwards
            dx, pdy = [], True
            for i, a in enumerate(adv):
                nx, ny = off[i + 1] if i + 1 < len(off) else (0, 0)
                dx += [a + nx - off[i][0], -(ny - off[i][1])]
        canvas = _Canvas(font, ppem, params["mode"], width, height, background, ink)
        try:
            canvas.clear()
            canvas.draw(pen_x + off[0][0], base_y - off[0][1], gids, dx, pdy)
            return (canvas.image(), pen_x, base_y, pen, adv,
                    None if offsets is None else off)
        finally:
            canvas.close()


