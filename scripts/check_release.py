"""CI's release check: a pull request that changes more than the docs must
bump the version and give it a dated CHANGELOG.md section.

    python scripts/check_release.py origin/main

Run it locally before opening a PR; .github/workflows/checks.yml runs it on
every pull request to main.
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sermon_shorts.core import version  # noqa: E402


def git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(ROOT), *args], check=True,
                          capture_output=True, text=True).stdout


def main(base: str) -> int:
    # Three dots: only what this branch changed since it left the base.
    changed = [p for p in git("diff", "--name-only", f"{base}...HEAD").splitlines() if p]
    base_version = version.parse_version_file(git("show", f"{base}:sermon_shorts/__init__.py"))
    head_version = version.parse_version_file((ROOT / "sermon_shorts" / "__init__.py").read_text(encoding="utf-8"))
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    problems = version.release_problems(base_version, head_version, changed, changelog)
    if not version.needs_release(changed):
        print("Nothing beyond the docs changed, so no release is needed.")
    elif not problems:
        print(f"Releases {head_version} (the base branch is at {base_version}).")
    for p in problems:
        print(f"::error::{p}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "origin/main"))
