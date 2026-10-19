# Third-party software

The repository's own code is MIT ([LICENSE.md](LICENSE.md)). A release and the installer
bring along, unchanged:

| Component | Where it comes from | License |
|---|---|---|
| NSSM 2.24 (`bin\nssm.exe`, in the release zip) | https://nssm.cc (SHA256 pinned in `scripts/build_release.py`) | Public domain |
| uv (`bin\uv.exe`, downloaded by `install.ps1`) | https://github.com/astral-sh/uv (version and SHA256 pinned in `install.ps1`) | MIT or Apache-2.0 |
| CPython 3.12 (`python\`, installed by uv) | https://github.com/astral-sh/python-build-standalone | PSF License (and the licenses of the bundled libraries) |
| Chromium (`browsers\`, installed by Playwright) | Playwright's browser builds (https://playwright.dev) | BSD-3-Clause and the licenses listed in the browser's `about:credits` |
| Python packages (`versions\<v>\.venv`) | PyPI, exact versions and hashes in `uv.lock` | Each package's own license (FastAPI, Uvicorn, Playwright, Pillow, fontTools, cryptography, …) |

No fonts are distributed: the tests use a font found on the system or build one with
fontTools.
