"""Developer-mode deploys: pack a git ref or the working tree for POST /dev/deploy.

    uv run font-render --deploy            # origin/main (after git fetch)
    uv run font-render --deploy a1b2c3d    # any commit or branch
    uv run font-render --deploy --local    # the working tree, uncommitted changes included
    uv run font-render --release           # back to a release

Needs FONT_RENDER_DEV_TOKEN (a client the server's administrator named in
`font-render-server dev-mode on --client ...`). Font files are never packed: the archive is
refused if one would be in it (the server refuses them too).
"""

import io
import subprocess
import zipfile
from pathlib import Path

FONT_EXTENSIONS = (".ttf", ".otf", ".ttc", ".otc", ".woff", ".woff2", ".dfont", ".pfb", ".pfa", ".eot")


class DeployError(Exception):
    pass


def _git(*args: str, cwd: Path | None = None) -> bytes:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True)
    if r.returncode != 0:
        raise DeployError(f"git {' '.join(args)}: {r.stderr.decode(errors='replace').strip()}")
    return r.stdout


def _refuse_fonts(names: list[str]) -> None:
    fonts = [n for n in names if n.lower().endswith(FONT_EXTENSIONS)]
    if fonts:
        raise DeployError("refusing to upload font files (move them out of the repository "
                          "or add them to .gitignore): " + ", ".join(fonts))


def pack_ref(ref: str, cwd: Path | None = None) -> tuple[bytes, str]:
    """git archive of `ref` → (zip, full commit sha); origin/* refs are fetched first."""
    if ref.startswith("origin/"):
        _git("fetch", "--quiet", "origin", cwd=cwd)
    commit = _git("rev-parse", "--verify", f"{ref}^{{commit}}", cwd=cwd).decode().strip()
    data = _git("archive", "--format=zip", commit, cwd=cwd)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        _refuse_fonts(z.namelist())
    return data, commit


def pack_worktree(cwd: Path | None = None) -> tuple[bytes, str, bool, int]:
    """Tracked + untracked non-ignored files as they are on disk → (zip, HEAD sha, dirty, count)."""
    root = Path(_git("rev-parse", "--show-toplevel", cwd=cwd).decode().strip())
    names = [n for n in _git("ls-files", "--cached", "--others", "--exclude-standard", "-z",
                             cwd=root).decode().split("\0") if n]
    names = [n for n in names if (root / n).is_file()]  # deleted but still tracked
    _refuse_fonts(names)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for n in sorted(names):
            z.write(root / n, n)
    commit = _git("rev-parse", "HEAD", cwd=root).decode().strip()
    dirty = bool(_git("status", "--porcelain", cwd=root).strip())
    return buf.getvalue(), commit, dirty, len(names)
