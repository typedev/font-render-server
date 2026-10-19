"""Font Render Service — renders fonts in headless Chromium on Windows (DirectWrite)
and returns PNG screenshots (+ glyph geometry) over HTTP."""

from importlib.metadata import PackageNotFoundError, version as _dist_version

try:
    __version__ = _dist_version("font-render-server")
except PackageNotFoundError:
    __version__ = "unknown"
