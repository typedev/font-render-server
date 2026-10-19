"""Build the release assets for a version (used by .github/workflows/release.yml).

    python scripts/build_release.py OUT_DIR [--ref HEAD]

Writes to OUT_DIR:
    font-render-<version>.zip   git archive of REF + bin/nssm.exe (pinned, SHA256 checked)
    install.ps1                 with the version baked in
    SHA256SUMS                  both of the above (signed later: scripts/release_key.py sign)

The version is `project.version` of pyproject.toml at REF. Stdlib only.
"""

import argparse
import hashlib
import io
import re
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# nssm.cc is often down (503); the archived copy is the same file (SHA256 checked either way)
NSSM_URLS = ("https://nssm.cc/release/nssm-2.24.zip",
             "https://web.archive.org/web/2024id_/https://nssm.cc/release/nssm-2.24.zip")
NSSM_SHA256 = "727d1e42275c605e0f04aba98095c38a8e1e46def453cdffce42869428aa6743"
NSSM_MEMBER = "nssm-2.24/win64/nssm.exe"


def git(*args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True).stdout


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _download_nssm() -> bytes:
    for attempt in range(3):
        for url in NSSM_URLS:
            try:
                with urllib.request.urlopen(url, timeout=60) as r:
                    data = r.read()
            except (urllib.error.URLError, OSError) as e:
                print(f"{url}: {e}", file=sys.stderr)
                continue
            if sha256(data) == NSSM_SHA256:
                return data
            print(f"{url}: SHA256 mismatch", file=sys.stderr)
        time.sleep(10 * (attempt + 1))
    sys.exit("nssm-2.24.zip: no download with the pinned SHA256")


def fetch_nssm(cache: Path) -> bytes:
    data = cache.read_bytes() if cache.is_file() else b""
    if sha256(data) != NSSM_SHA256:
        data = _download_nssm()
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(data)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return z.read(NSSM_MEMBER)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("out", type=Path)
    parser.add_argument("--ref", default="HEAD")
    parser.add_argument("--nssm-cache", type=Path, default=ROOT / "tools" / "nssm-2.24.zip")
    args = parser.parse_args()

    pyproject = tomllib.loads(git("show", f"{args.ref}:pyproject.toml").decode())
    version = pyproject["project"]["version"]
    args.out.mkdir(parents=True, exist_ok=True)

    # git archive applies .gitattributes (CRLF for .bat/.cmd/.ps1, export-ignore)
    zip_path = args.out / f"font-render-{version}.zip"
    zip_path.write_bytes(git("archive", "--format=zip", args.ref))
    with zipfile.ZipFile(zip_path, "a", zipfile.ZIP_DEFLATED) as z:
        z.writestr("bin/nssm.exe", fetch_nssm(args.nssm_cache))

    script = git("show", f"{args.ref}:install.ps1").decode("ascii").replace("\r\n", "\n")
    script, n = re.subn(r"^\$ReleaseVersion = ''", f"$ReleaseVersion = '{version}'", script, flags=re.M)
    if n != 1:
        sys.exit("install.ps1: $ReleaseVersion line not found")
    (args.out / "install.ps1").write_bytes(script.replace("\n", "\r\n").encode("ascii"))

    sums = "".join(f"{sha256((args.out / name).read_bytes())}  {name}\n"
                   for name in (zip_path.name, "install.ps1"))
    (args.out / "SHA256SUMS").write_text(sums, encoding="ascii")
    print(f"version={version}")
    print(sums, end="")


if __name__ == "__main__":
    main()
