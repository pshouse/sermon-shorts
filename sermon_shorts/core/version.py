"""What code is running: the semantic version and the git commit.

The version (sermon_shorts/__init__.py, bumped by hand with a CHANGELOG.md
entry) says what changed for people; the commit says exactly which code is
deployed, since the sprite deploys with `git pull` between releases. Both
are recorded on every run and shown by the CLI, the API and the Studio.

The API process also compares the commit it started with against the
checkout on disk: Python changes need the `api` service restarted, while
the Studio's web files are read fresh on every request and never do.
"""

from __future__ import annotations

import re
import subprocess

from .. import __version__
from .db import PROJECT_DIR

SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?$")
_INIT = PROJECT_DIR / "sermon_shorts" / "__init__.py"


def _git(*args: str) -> str | None:
    try:
        r = subprocess.run(["git", "-C", str(PROJECT_DIR), *args],
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None  # no git, or not a checkout (e.g. an installed copy)
    return r.stdout.strip() if r.returncode == 0 else None


def commit() -> str | None:
    """The checkout's current commit, short form."""
    return _git("rev-parse", "--short=7", "HEAD")


def dirty() -> bool:
    """Tracked files changed since that commit (a hand edit on the sprite)."""
    return bool(_git("status", "--porcelain", "--untracked-files=no"))


def version_on_disk() -> str | None:
    """The version in __init__.py as it is on disk now, which can be newer
    than the one this process imported."""
    try:
        m = re.search(r'__version__\s*=\s*"([^"]+)"', _INIT.read_text(encoding="utf-8"))
    except OSError:
        return None
    return m.group(1) if m else None


def info() -> dict:
    return {"version": __version__, "commit": commit(), "dirty": dirty()}


def label(v: dict | None = None) -> str:
    """e.g. "0.2.0 (39d0a25)", "0.2.0 (39d0a25, modified)" or "0.2.0"."""
    v = v or info()
    extra = [x for x in (v.get("commit"), "modified" if v.get("dirty") else None) if x]
    return f"{v['version']} ({', '.join(extra)})" if extra else v["version"]


def python_changed_since(old_commit: str) -> list[str] | None:
    """Files a running process would need a restart to pick up: Python code
    and requirements changed between `old_commit` and the checkout now.
    None if git can't tell."""
    out = _git("diff", "--name-only", old_commit, "HEAD", "--", "*.py", "requirements.txt")
    return None if out is None else [line for line in out.splitlines() if line]
