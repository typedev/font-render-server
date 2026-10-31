"""DirectWrite engine for /glyphs (Windows only): explicit rendering parameters, independent of
the account's settings.

Fonts load from memory (IDWriteFactory5 in-memory loader, never on disk). Glyphs are drawn by
Windows itself: IDWriteGdiInterop → IDWriteBitmapRenderTarget(1)::DrawGlyphRun with rendering
params from IDWriteFactory2::CreateCustomRenderingParams. Windows blends (gamma, enhanced
contrast, ClearType level) onto the background already in the bitmap, so the server has no
blending formula of its own. Everything runs on the engine's single worker thread.

Vtable indices: IUnknown is 0–2, then the methods of each interface in declaration order,
base interfaces first (dwrite.h, dwrite_1.h … dwrite_3.h). Each object obtained is checked
with QueryInterface for the expected IID before it is used.
"""

import ctypes
import math
from collections import OrderedDict
from ctypes import POINTER, Structure, byref, c_float, c_int, c_uint16, c_uint32, c_void_p
from ctypes import wintypes

from PIL import Image

from . import dwrite
from .dwrite import Com
from .engines import Cell, Positions, RenderError, ink_box, pens, place
from . import sfnt
from .sfnt import PreparedFont

IID_IDWriteFactory2 = "0439fc60-ca44-4994-8dee-3a9af7b732ec"
IID_IDWriteFactory5 = "958db99a-be2a-4f09-af7d-65189803d1d3"
IID_IDWriteFactory6 = "f3744d80-21f7-42eb-b35d-995bc72fc223"
IID_IDWriteInMemoryFontFileLoader = "dc102f47-a12d-4b1c-822d-9e117e33043f"
IID_IDWriteFontFace = "5f49804d-7024-4d43-bfa9-d25984f53849"
IID_IDWriteFontFace3 = "d37d7598-09be-4222-a236-2081341cc1f2"
IID_IDWriteFontFace5 = "98eff3a5-b667-479a-b145-e2fa5b9fdc29"
IID_IDWriteRenderingParams2 = "f9d711c3-9777-40ae-87e8-3e5af9bf0948"
IID_IDWriteBitmapRenderTarget1 = "791e8298-3ef3-4230-9880-c9bdecc42064"

RENDERING_MODES = {"default": 0, "aliased": 1, "gdi_classic": 2, "gdi_natural": 3, "natural": 4,
                   "natural_symmetric": 5}
RENDERING_MODE1_NAMES = {1: "aliased", 2: "gdi_classic", 3: "gdi_natural", 4: "natural",
                         5: "natural_symmetric", 7: "natural_symmetric_downsampled"}
MEASURING_MODES = {"natural": 0, "gdi_classic": 1, "gdi_natural": 2}
ANTIALIAS = {"cleartype": 0, "grayscale": 1}
PIXEL_GEOMETRY = {"flat": 0, "rgb": 1, "bgr": 2}
GRID_FIT = {"default": 0, "disabled": 1, "enabled": 2}
# A clean Windows install (IDWriteFactory::CreateRenderingParams on a fresh profile; /info
# windows.dwrite_default_params on the maintainers' server reports the same).
DEFAULTS = {"rendering_mode": "natural_symmetric", "measuring_mode": "natural",
            "antialias": "cleartype", "pixel_geometry": "rgb", "gamma": 1.8, "contrast": 0.5,
            "grayscale_contrast": 1.0, "cleartype_level": 1.0, "grid_fit": "default"}
FLOAT_RANGES = {"gamma": (1.0, 3.0), "contrast": (0.0, 10.0), "grayscale_contrast": (0.0, 10.0),
                "cleartype_level": (0.0, 1.0)}
FACES_MAX = 16
LOADS_PER_FACTORY = 64   # the in-memory loader keeps every file it saw: start over after this


class DWRITE_FONT_METRICS(Structure):
    _fields_ = [("designUnitsPerEm", c_uint16), ("ascent", c_uint16), ("descent", c_uint16),
                ("lineGap", ctypes.c_int16), ("capHeight", c_uint16), ("xHeight", c_uint16),
                ("underlinePosition", ctypes.c_int16), ("underlineThickness", c_uint16),
                ("strikethroughPosition", ctypes.c_int16), ("strikethroughThickness", c_uint16)]


class DWRITE_GLYPH_METRICS(Structure):
    _fields_ = [("leftSideBearing", c_int), ("advanceWidth", c_uint32),
                ("rightSideBearing", c_int), ("topSideBearing", c_int),
                ("advanceHeight", c_uint32), ("bottomSideBearing", c_int),
                ("verticalOriginY", c_int)]


class DWRITE_GLYPH_RUN(Structure):
    _fields_ = [("fontFace", c_void_p), ("fontEmSize", c_float), ("glyphCount", c_uint32),
                ("glyphIndices", POINTER(c_uint16)), ("glyphAdvances", POINTER(c_float)),
                ("glyphOffsets", c_void_p), ("isSideways", wintypes.BOOL),
                ("bidiLevel", c_uint32)]


class DWRITE_GLYPH_OFFSET(Structure):
    _fields_ = [("advanceOffset", c_float), ("ascenderOffset", c_float)]


class DWRITE_FONT_AXIS_VALUE(Structure):
    _fields_ = [("axisTag", c_uint32), ("value", c_float)]


class BITMAP(Structure):
    _fields_ = [("bmType", wintypes.LONG), ("bmWidth", wintypes.LONG),
                ("bmHeight", wintypes.LONG), ("bmWidthBytes", wintypes.LONG),
                ("bmPlanes", wintypes.WORD), ("bmBitsPixel", wintypes.WORD),
                ("bmBits", c_void_p)]


class DIBSECTION(Structure):
    _fields_ = [("dsBm", BITMAP), ("dsBmih_biSize", wintypes.DWORD),
                ("dsBmih_biWidth", wintypes.LONG), ("dsBmih_biHeight", wintypes.LONG),
                ("_rest", ctypes.c_byte * 28), ("dsBitfields", wintypes.DWORD * 3),
                ("dshSection", c_void_p), ("dsOffset", wintypes.DWORD)]


HR = getattr(ctypes, "HRESULT", ctypes.c_long)  # importable off Windows (tests)
PP = POINTER(c_void_p)


def _out_call(obj: Com, index: int, *args) -> Com:
    """Call a method whose last parameter is an interface out-pointer; return it owned."""
    out = c_void_p()
    obj.call(index, HR, *args, (PP, byref(out)))
    if not out.value:
        raise RenderError("dwrite_null_object")
    return Com(out)


def _checked(obj: Com, iid: str) -> Com:
    """Make sure `obj` really is `iid` (catches a wrong vtable index before it is used)."""
    q = obj.query(iid)
    if q is None:
        obj.release()
        raise RenderError("dwrite_interface_mismatch")
    q.release()
    return obj


def axis_tag(tag: str) -> int:
    b = tag.encode("ascii").ljust(4)[:4]
    return b[0] | (b[1] << 8) | (b[2] << 16) | (b[3] << 24)


class _Factory:
    """Factory, GDI interop and the in-memory loader; recreated after LOADS_PER_FACTORY fonts."""

    def __init__(self):
        out = c_void_p()
        fn = ctypes.windll.dwrite.DWriteCreateFactory
        fn.restype = HR
        fn(c_int(dwrite.DWRITE_FACTORY_TYPE_ISOLATED), byref(dwrite.GUID.of(IID_IDWriteFactory5)),
           byref(out))
        self.factory = _checked(Com(out), IID_IDWriteFactory5)
        self.loader = _checked(_out_call(self.factory, 44), IID_IDWriteInMemoryFontFileLoader)
        self.factory.call(13, HR, (c_void_p, self.loader.ptr))       # RegisterFontFileLoader
        self.interop = _out_call(self.factory, 17)                    # GetGdiInterop
        self.loads = 0
        self.faces: OrderedDict[tuple, Com] = OrderedDict()
        self.files: OrderedDict[str, Com] = OrderedDict()

    def _file(self, font: PreparedFont) -> Com:
        """The font's in-memory file, loaded once per font (all locations share it)."""
        if font.sha in self.files:
            self.files.move_to_end(font.sha)
            return self.files[font.sha]
        data = ctypes.create_string_buffer(font.data, len(font.data))
        # CreateInMemoryFontFileReference(factory, data, size, owner=NULL → DWrite copies)
        file = _out_call(self.loader, 4, (c_void_p, self.factory.ptr), (c_void_p, data),
                         (c_uint32, len(font.data)), (c_void_p, None))
        self.loads += 1
        self.files[font.sha] = file
        while len(self.files) > FACES_MAX:
            self.files.popitem(last=False)[1].release()
        return file

    def face(self, font: PreparedFont, axes: tuple) -> Com:
        """axes: every fvar axis as (tag, user value) for a variable font, () for a static one."""
        key = (font.sha, axes)
        if key in self.faces:
            self.faces.move_to_end(key)
            return self.faces[key]
        file = self._file(font)
        if axes:
            resource = _out_call(self.factory, 49, (c_void_p, file.ptr), (c_uint32, 0))
            with resource:                                   # IDWriteFontResource
                values = (DWRITE_FONT_AXIS_VALUE * len(axes))(
                    *(DWRITE_FONT_AXIS_VALUE(axis_tag(t), v) for t, v in axes))
                face = _out_call(resource, 13, (c_int, 0), (c_void_p, values),
                                 (c_uint32, len(axes)))      # CreateFontFace (user coordinates)
            face = _checked(face, IID_IDWriteFontFace5)
        else:
            face = self._static_face(file)
        if face.call(9, c_uint16) != font.num_glyphs:              # GetGlyphCount
            face.release()
            raise RenderError("dwrite_glyph_count_mismatch")
        self.faces[key] = face
        while len(self.faces) > FACES_MAX:
            self.faces.popitem(last=False)[1].release()
        return face

    def _static_face(self, file: Com) -> Com:
        supported, file_type, face_type, faces = wintypes.BOOL(), c_int(), c_int(), c_uint32()
        file.call(5, HR, (POINTER(wintypes.BOOL), byref(supported)), (POINTER(c_int), byref(file_type)),
                  (POINTER(c_int), byref(face_type)), (POINTER(c_uint32), byref(faces)))  # Analyze
        if not supported.value:
            raise RenderError("font_rejected")
        files = (c_void_p * 1)(file.ptr)
        face = _out_call(self.factory, 9, (c_int, face_type.value), (c_uint32, 1),
                         (c_void_p, files), (c_uint32, 0), (c_int, 0))   # CreateFontFace
        return _checked(face, IID_IDWriteFontFace)

    def rendering_params(self, p: dict) -> Com:
        """IDWriteFactory2::CreateCustomRenderingParams, read back to verify the values."""
        params = _out_call(self.factory, 29, (c_float, p["gamma"]), (c_float, p["contrast"]),
                           (c_float, p["grayscale_contrast"]), (c_float, p["cleartype_level"]),
                           (c_int, PIXEL_GEOMETRY[p["pixel_geometry"]]),
                           (c_int, RENDERING_MODES[p["rendering_mode"]]),
                           (c_int, GRID_FIT[p["grid_fit"]]))
        params = _checked(params, IID_IDWriteRenderingParams2)
        if abs(params.call(3, c_float) - p["gamma"]) > 1e-4:     # GetGamma
            params.release()
            raise RenderError("dwrite_params_mismatch")
        return params

    def release(self):
        for obj in (*self.faces.values(), *self.files.values()):
            obj.release()
        self.faces.clear()
        self.files.clear()
        self.interop.release()
        self.factory.call(14, HR, (c_void_p, self.loader.ptr))       # UnregisterFontFileLoader
        self.loader.release()
        self.factory.release()


class _Target:
    """An IDWriteBitmapRenderTarget(1) of a given size with direct access to its DIB bits."""

    def __init__(self, fx: _Factory, width: int, height: int, antialias: str, background):
        self.w, self.h = width, height
        self.target = _out_call(fx.interop, 7, (c_void_p, None), (c_uint32, width),
                                (c_uint32, height))                   # CreateBitmapRenderTarget
        t1 = self.target.query(IID_IDWriteBitmapRenderTarget1)
        if t1 is None:
            self.target.release()
            raise RenderError("dwrite_no_render_target1")
        with t1:
            t1.call(12, HR, (c_int, ANTIALIAS[antialias]))            # SetTextAntialiasMode
        self.target.call(6, HR, (c_float, 1.0))                      # SetPixelsPerDip
        dc = self.target.call(4, c_void_p)                            # GetMemoryDC
        gdi32 = ctypes.WinDLL("gdi32")
        gdi32.GetCurrentObject.restype, gdi32.GetCurrentObject.argtypes = c_void_p, [c_void_p, wintypes.UINT]
        gdi32.GetObjectW.restype, gdi32.GetObjectW.argtypes = c_int, [c_void_p, c_int, c_void_p]
        gdi32.GdiFlush.restype = wintypes.BOOL
        self.gdi32 = gdi32
        ds = DIBSECTION()
        bmp = gdi32.GetCurrentObject(dc, 7)                           # OBJ_BITMAP
        if gdi32.GetObjectW(bmp, ctypes.sizeof(ds), byref(ds)) != ctypes.sizeof(ds) \
                or ds.dsBm.bmBitsPixel != 32 or not ds.dsBm.bmBits:
            self.target.release()
            raise RenderError("dwrite_bitmap_unreadable")
        self.bits, self.stride = ds.dsBm.bmBits, ds.dsBm.bmWidthBytes
        self.fill = bytes((background[2], background[1], background[0], 0)) * (self.stride // 4 * height)
        # GetObject reports biHeight positive even for a top-down DIB: find the row order by
        # setting pixel (0, 0) and looking where it lands in memory.
        gdi32.SetPixelV.restype, gdi32.SetPixelV.argtypes = wintypes.BOOL, [c_void_p, c_int, c_int, wintypes.DWORD]
        self.clear()
        gdi32.SetPixelV(dc, 0, 0, 0x00010203)
        gdi32.GdiFlush()
        self.bottom_up = ctypes.string_at(self.bits, 3) != b"\x01\x02\x03"  # BGR of 0x010203

    def clear(self):
        self.gdi32.GdiFlush()
        ctypes.memmove(self.bits, self.fill, len(self.fill))

    def draw(self, fx: _Factory, face: Com, ppem: float, gids, advances, x: float, y: float,
             measuring: str, params: Com, ink, offsets=None):
        """offsets: [(dx, dy)] in pixels, dy up (glyphOffsets: advanceOffset, ascenderOffset)."""
        n = len(gids)
        goff = (DWRITE_GLYPH_OFFSET * n)(*offsets) if offsets is not None else None
        run = DWRITE_GLYPH_RUN(face.ptr, ppem, n, (c_uint16 * n)(*gids),
                               (c_float * n)(*advances),
                               ctypes.cast(goff, c_void_p) if goff is not None else None, False, 0)
        color = ink[0] | (ink[1] << 8) | (ink[2] << 16)
        self.target.call(3, HR, (c_float, x), (c_float, y), (c_int, MEASURING_MODES[measuring]),
                         (POINTER(DWRITE_GLYPH_RUN), byref(run)), (c_void_p, params.ptr),
                         (wintypes.DWORD, color), (c_void_p, None))   # DrawGlyphRun

    def image(self) -> Image.Image:
        self.gdi32.GdiFlush()
        data = ctypes.string_at(self.bits, self.stride * self.h)
        img = Image.frombuffer("RGB", (self.w, self.h), data, "raw", "BGRX", self.stride,
                               -1 if self.bottom_up else 1)
        return img.copy()

    def release(self):
        self.target.release()


class DWriteEngine:
    name = "dwrite"
    render_version = 1

    def __init__(self):
        self._fx: _Factory | None = None

    def ppem_ok(self, ppem: float) -> bool:
        return True

    def parse_params(self, form: dict) -> dict:
        p = dict(DEFAULTS)
        for key, table in (("rendering_mode", RENDERING_MODES), ("measuring_mode", MEASURING_MODES),
                           ("antialias", ANTIALIAS), ("pixel_geometry", PIXEL_GEOMETRY),
                           ("grid_fit", GRID_FIT)):
            if form.get(key):
                if form[key] not in table:
                    raise ValueError(f"{key} must be one of {', '.join(table)}")
                p[key] = form[key]
        for key, (lo, hi) in FLOAT_RANGES.items():
            if form.get(key):
                v = float(form[key])
                if not lo <= v <= hi:
                    raise ValueError(f"{key} must be {lo}..{hi}")
                p[key] = v
        p["axes"] = sfnt.parse_location(form.get("axes", ""))  # resolved per font in resolve()
        return p

    def resolve(self, font: PreparedFont, params: dict) -> dict:
        """Fill every fvar axis (requested value clamped, else default); ValueError if the font
        is static but axes were given, or a tag is not in fvar."""
        if not font.axes:
            if params["axes"]:
                raise ValueError("axes given for a font without fvar")
            return {**params, "axes": ()}
        return {**params, "axes": tuple(sfnt.resolve_location(font.axes, params["axes"]).items())}

    def _factory(self) -> _Factory:
        if self._fx is not None and self._fx.loads >= LOADS_PER_FACTORY:
            self._fx.release()
            self._fx = None
        if self._fx is None:
            self._fx = _Factory()
        return self._fx

    def _metrics(self, face: Com, font: PreparedFont, ppem: float, gids, measuring: str):
        """(ascent px, descent px, [advance px]) as DirectWrite measures in this mode."""
        fm = DWRITE_FONT_METRICS()
        gm = (DWRITE_GLYPH_METRICS * len(gids))()
        idx = (c_uint16 * len(gids))(*gids)
        if measuring == "natural":
            face.call(8, None, (POINTER(DWRITE_FONT_METRICS), byref(fm)))          # GetMetrics
            face.call(10, HR, (POINTER(c_uint16), idx), (c_uint32, len(gids)),
                      (POINTER(DWRITE_GLYPH_METRICS), gm), (wintypes.BOOL, False))  # GetDesignGlyphMetrics
        else:
            face.call(16, HR, (c_float, ppem), (c_float, 1.0), (c_void_p, None),
                      (POINTER(DWRITE_FONT_METRICS), byref(fm)))                    # GetGdiCompatibleMetrics
            face.call(17, HR, (c_float, ppem), (c_float, 1.0), (c_void_p, None),
                      (wintypes.BOOL, measuring == "gdi_natural"), (POINTER(c_uint16), idx),
                      (c_uint32, len(gids)), (POINTER(DWRITE_GLYPH_METRICS), gm),
                      (wintypes.BOOL, False))                                       # GetGdiCompatibleGlyphMetrics
        scale = ppem / fm.designUnitsPerEm
        advances = [m.advanceWidth * scale for m in gm]
        if measuring != "natural":
            advances = [float(round(a)) for a in advances]
        return math.ceil(fm.ascent * scale - 1e-6), math.ceil(fm.descent * scale - 1e-6), advances

    def _effective(self, fx: _Factory, face: Com, ppem: float, params: dict) -> tuple[dict, dict]:
        """For rendering_mode=default: ask DirectWrite (IDWriteFontFace3::GetRecommendedRenderingMode,
        which reads gasp) for the mode and grid fit at this size, and draw with exactly those, so
        the mode reported per row is the mode drawn. Explicit modes pass through unchanged."""
        if params["rendering_mode"] != "default":
            return params, {"rendering_mode": params["rendering_mode"]}
        f3 = face.query(IID_IDWriteFontFace3)
        if f3 is None:
            raise RenderError("dwrite_no_fontface3")
        rp = fx.rendering_params(params)
        try:
            mode, grid = c_int(), c_int()
            f3.call(44, HR, (c_float, ppem), (c_float, 96.0), (c_float, 96.0), (c_void_p, None),
                    (wintypes.BOOL, False), (c_int, 0),           # outline threshold: antialiased
                    (c_int, MEASURING_MODES[params["measuring_mode"]]), (c_void_p, rp.ptr),
                    (POINTER(c_int), byref(mode)), (POINTER(c_int), byref(grid)))
        finally:
            rp.release()
            f3.release()
        name = RENDERING_MODE1_NAMES.get(mode.value)
        grid_name = {v: k for k, v in GRID_FIT.items()}.get(grid.value)
        if name is None or grid_name is None:
            raise RenderError("dwrite_unexpected_mode")
        drawn = "natural_symmetric" if name == "natural_symmetric_downsampled" else name
        return ({**params, "rendering_mode": drawn, "grid_fit": grid_name},
                {"rendering_mode": name, "grid_fit": grid_name})

    def cells(self, font: PreparedFont, gids, ppem, params, background, ink):
        fx = self._factory()
        face = fx.face(font, params["axes"])
        params, row_info = self._effective(fx, face, ppem, params)
        rp = fx.rendering_params(params)
        try:
            ascent, descent, advances = self._metrics(face, font, ppem, gids, params["measuring_mode"])
            size = math.ceil(ppem)
            margin = 2 * size + 16
            while True:
                w, h = 5 * size + 2 * margin, 4 * size + 2 * margin
                target = _Target(fx, w, h, params["antialias"], background)
                try:
                    out, clipped = [], False
                    for gid, adv in zip(gids, advances):
                        target.clear()
                        target.draw(fx, face, ppem, [gid], [adv], margin, margin + 2 * size,
                                    params["measuring_mode"], rp, ink)
                        img = target.image()
                        box = ink_box(img, background)
                        if box and (box[0] == 0 or box[1] == 0 or box[2] == w or box[3] == h):
                            clipped = True
                            break
                        out.append(Cell.from_canvas(gid, img, background, margin, margin + 2 * size,
                                                    adv))
                finally:
                    target.release()
                if not clipped:
                    return ascent, descent, out, row_info
                if margin > 16 * size + 64:
                    raise RenderError("glyph_too_large")
                margin *= 2
        finally:
            rp.release()

    def run(self, font: PreparedFont, gids, ppem, params, background, ink,
            positions: Positions | None = None):
        """With positions: glyphAdvances = own advance + the shaper's adjustment, glyphOffsets
        scaled; whole pixels for gdi_classic/gdi_natural measuring (GetGdiCompatibleGlyphPlacements)."""
        fx = self._factory()
        face = fx.face(font, params["axes"])
        params, _info = self._effective(fx, face, ppem, params)
        rp = fx.rendering_params(params)
        try:
            _a, _d, advances = self._metrics(face, font, ppem, gids, params["measuring_mode"])
            glyph_offsets = None
            if positions is not None:
                advances, glyph_offsets = place(font, gids, ppem, advances, positions,
                                                whole=params["measuring_mode"] != "natural")
            offsets = pens(advances)
            size = math.ceil(ppem)
            margin = 2 * size + 16
            pen_x, width = margin, math.ceil(offsets[-1] + advances[-1]) + 2 * margin + 2 * size
            if positions is not None:
                xs = [p + dx for p, (dx, _dy) in zip(offsets, glyph_offsets or [(0, 0)] * len(gids))]
                xs.append(offsets[-1] + advances[-1])
                margin += math.ceil(max([abs(d) for o in glyph_offsets or [] for d in o] + [0]))
                pen_x = margin - math.floor(min(xs + [0]))
                width = math.ceil(max(xs + [0])) - math.floor(min(xs + [0])) + 2 * margin + 2 * size
            height = 4 * size + 2 * margin
            target = _Target(fx, width, height, params["antialias"], background)
            try:
                target.clear()
                target.draw(fx, face, ppem, gids, advances, pen_x, margin + 2 * size,
                            params["measuring_mode"], rp, ink, glyph_offsets)
                return (target.image(), pen_x, margin + 2 * size, offsets, advances,
                        glyph_offsets)
            finally:
                target.release()
        finally:
            rp.release()
