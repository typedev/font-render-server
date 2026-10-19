"""Windows settings and versions that change rendered pixels, as seen by the service account.

Reported in /info so clients can key their render caches on them. Everything returns None
off Windows. Reads are cheap and done per call: the settings can change while the service runs.
"""

import ctypes
import functools
import sys

_DLLS = ("dwrite.dll", "gdi32full.dll", "win32kfull.sys")


def text_settings() -> dict | None:
    """System font smoothing of the account the service runs as (SPI; GDI and browsers read it)."""
    if sys.platform != "win32":
        return None
    spi = ctypes.windll.user32.SystemParametersInfoW
    out = {}
    for name, action in (("font_smoothing", 0x004A), ("smoothing_type", 0x200A),
                         ("smoothing_contrast", 0x200C), ("smoothing_orientation", 0x2012)):
        v = ctypes.c_uint(0)
        out[name] = v.value if spi(action, 0, ctypes.byref(v), 0) else None
    out["smoothing_type_name"] = {1: "standard (grayscale)", 2: "ClearType"}.get(out["smoothing_type"])
    out["orientation_name"] = {0: "BGR", 1: "RGB"}.get(out["smoothing_orientation"])
    return out


def cleartype_registry() -> dict | None:
    """ClearType Tuner values (Avalon.Graphics\\DISPLAYn), raw; a missing key = Windows defaults."""
    if sys.platform != "win32":
        return None
    import winreg
    out = {}
    for hive_name, hive in (("hkcu", winreg.HKEY_CURRENT_USER), ("hklm", winreg.HKEY_LOCAL_MACHINE)):
        displays = {}
        try:
            root = winreg.OpenKey(hive, r"Software\Microsoft\Avalon.Graphics")
        except OSError:
            out[hive_name] = displays
            continue
        with root:
            for display in _subkeys(root):
                with winreg.OpenKey(root, display) as key:
                    displays[display] = _values(key)
        out[hive_name] = displays
    return out


def dwrite_default_params() -> dict | None:
    """IDWriteFactory::CreateRenderingParams of the service account (what DWrite apps get)."""
    if sys.platform != "win32":
        return None
    from . import dwrite
    try:
        return dwrite.default_rendering_params()
    except OSError as e:
        return {"error": str(e)}


@functools.cache
def versions() -> dict | None:
    """Windows build with UBR and the versions of the rasterizer DLLs (fixed until a reboot)."""
    if sys.platform != "win32":
        return None
    import os
    import winreg
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion") as k:
        v = _values(k)
    build = (f"{v.get('CurrentMajorVersionNumber', 10)}.{v.get('CurrentMinorVersionNumber', 0)}."
             f"{v.get('CurrentBuildNumber', '?')}.{v.get('UBR', 0)}")
    system32 = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
    dlls = {name: _file_version(os.path.join(system32, name)) for name in _DLLS}
    return {"build": build, "display_version": v.get("DisplayVersion"), "dll": dlls}


def account() -> str:
    """User name of the process token (SYSTEM for the service; USERNAME would say HOST$)."""
    buf = ctypes.create_unicode_buffer(257)
    size = ctypes.c_uint(len(buf))
    if not ctypes.windll.advapi32.GetUserNameW(buf, ctypes.byref(size)):
        raise OSError("GetUserNameW failed")
    return buf.value


def info() -> dict | None:
    """The `windows` block of /info."""
    if sys.platform != "win32":
        return None
    out = {}
    for name, fn in (("versions", versions), ("account", account), ("spi", text_settings),
                     ("cleartype_registry", cleartype_registry),
                     ("dwrite_default_params", dwrite_default_params)):
        try:
            value = fn()
        except Exception as e:  # one unreadable part must not break /info
            value = {"error": f"{type(e).__name__}: {e}"}
        if name == "versions" and "error" not in value:
            out.update(value)
        else:
            out[name] = value
    return out


def _subkeys(key) -> list[str]:
    import winreg
    out, i = [], 0
    while True:
        try:
            out.append(winreg.EnumKey(key, i))
        except OSError:
            return out
        i += 1


def _values(key) -> dict:
    import winreg
    out, i = {}, 0
    while True:
        try:
            name, value, _type = winreg.EnumValue(key, i)
        except OSError:
            return out
        if isinstance(value, (int, str)):
            out[name] = value
        i += 1


def _file_version(path: str) -> str | None:
    """FileVersion from the fixed version info (VS_FIXEDFILEINFO), e.g. 10.0.26100.6899."""
    ver = ctypes.windll.version
    size = ver.GetFileVersionInfoSizeW(path, None)
    if not size:
        return None
    buf = ctypes.create_string_buffer(size)
    if not ver.GetFileVersionInfoW(path, 0, size, buf):
        return None
    ptr, length = ctypes.c_void_p(), ctypes.c_uint()
    if not ver.VerQueryValueW(buf, "\\", ctypes.byref(ptr), ctypes.byref(length)):
        return None
    ms, ls = (ctypes.c_uint32 * 4).from_address(ptr.value)[2:4]  # dwFileVersionMS/LS
    return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
