"""DirectWrite through ctypes: hand-written COM vtable calls (Windows only).

Vtable indices count IUnknown's three methods first; they follow the declaration order in
dwrite.h / dwrite_1.h. A failing HRESULT raises OSError (ctypes.HRESULT restype).
"""

import ctypes
import uuid
from ctypes import POINTER, byref, c_float, c_int, c_void_p

DWRITE_FACTORY_TYPE_ISOLATED = 1

IID_IDWriteFactory = "b859ee5a-d838-4b5b-a2e8-1adc7d93db48"
IID_IDWriteRenderingParams1 = "94413cf4-a6fc-4248-8b50-6674348fcad3"

PIXEL_GEOMETRY = {0: "flat", 1: "rgb", 2: "bgr"}
RENDERING_MODE = {0: "default", 1: "aliased", 2: "gdi_classic", 3: "gdi_natural", 4: "natural",
                  5: "natural_symmetric", 6: "outline"}


class GUID(ctypes.Structure):
    _fields_ = [("bytes", ctypes.c_ubyte * 16)]

    @classmethod
    def of(cls, s: str) -> "GUID":
        g = cls()
        ctypes.memmove(g.bytes, uuid.UUID(s).bytes_le, 16)
        return g


class Com:
    """An owned COM interface pointer; call(index, restype, (argtype, value)...)."""

    def __init__(self, ptr: c_void_p):
        self.ptr = ptr

    def call(self, index: int, restype, *args):
        vtbl = ctypes.cast(self.ptr, POINTER(POINTER(c_void_p))).contents
        proto = ctypes.WINFUNCTYPE(restype, c_void_p, *(t for t, _ in args))
        return proto(vtbl[index])(self.ptr, *(v for _, v in args))

    def query(self, iid: str) -> "Com | None":
        out = c_void_p()
        try:
            self.call(0, ctypes.HRESULT, (POINTER(GUID), byref(GUID.of(iid))),
                      (POINTER(c_void_p), byref(out)))
        except OSError:
            return None
        return Com(out)

    def release(self):
        if self.ptr:
            self.call(2, ctypes.c_ulong)
            self.ptr = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()


def create_factory() -> Com:
    """An isolated factory: no state shared with other DWrite users, fresh system settings."""
    out = c_void_p()
    fn = ctypes.windll.dwrite.DWriteCreateFactory
    fn.restype = ctypes.HRESULT
    fn(c_int(DWRITE_FACTORY_TYPE_ISOLATED), byref(GUID.of(IID_IDWriteFactory)), byref(out))
    return Com(out)


def _params_view(params: Com) -> dict:
    out = {
        "gamma": round(params.call(3, c_float), 4),
        "enhanced_contrast": round(params.call(4, c_float), 4),
        "cleartype_level": round(params.call(5, c_float), 4),
        "pixel_geometry": PIXEL_GEOMETRY.get(params.call(6, c_int)),
        "rendering_mode": RENDERING_MODE.get(params.call(7, c_int)),
    }
    if (p1 := params.query(IID_IDWriteRenderingParams1)) is not None:
        with p1:
            out["grayscale_enhanced_contrast"] = round(p1.call(8, c_float), 4)
    return out


def default_rendering_params() -> dict:
    """IDWriteFactory::CreateRenderingParams: the account's settings for the primary monitor."""
    with create_factory() as factory:
        out = c_void_p()
        factory.call(10, ctypes.HRESULT, (POINTER(c_void_p), byref(out)))
        with Com(out) as params:
            return _params_view(params)
