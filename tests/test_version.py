"""Version tracking. Run: python -m unittest discover tests (standard library only)."""

import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sermon_shorts import __version__
from sermon_shorts.core import runs, version
from sermon_shorts.core.db import PROJECT_DIR, connect

CHANGELOG = PROJECT_DIR / "CHANGELOG.md"


class VersionTest(unittest.TestCase):
    def test_version_is_semver(self):
        self.assertRegex(__version__, version.SEMVER)

    def test_changelog_has_an_entry_for_this_version(self):
        # Bumping __version__ without saying what changed fails here.
        headings = re.findall(r"^## \[([^\]]+)\]", CHANGELOG.read_text(encoding="utf-8"), re.M)
        self.assertIn(__version__, headings, "add a CHANGELOG.md entry for this version")
        self.assertEqual(headings[0], "Unreleased", "keep [Unreleased] at the top")
        released = [h for h in headings if h != "Unreleased"]
        key = lambda v: tuple(int(x) for x in v.split("-")[0].split("."))
        self.assertEqual(released, sorted(released, key=key, reverse=True), "newest first")
        self.assertEqual(released[0], __version__, "the newest entry is the current version")

    def test_version_on_disk_matches_the_import(self):
        self.assertEqual(version.version_on_disk(), __version__)

    def test_label(self):
        self.assertEqual(version.label({"version": "1.2.3", "commit": "abc1234", "dirty": False}),
                         "1.2.3 (abc1234)")
        self.assertEqual(version.label({"version": "1.2.3", "commit": "abc1234", "dirty": True}),
                         "1.2.3 (abc1234, modified)")
        self.assertEqual(version.label({"version": "1.2.3", "commit": None, "dirty": False}), "1.2.3")

    def test_no_git_means_no_commit_not_an_error(self):
        with mock.patch("subprocess.run", side_effect=FileNotFoundError):
            self.assertIsNone(version.commit())
            self.assertFalse(version.dirty())
            self.assertIsNone(version.python_changed_since("abc1234"))


class ReleaseCheckTest(unittest.TestCase):
    CHANGELOG = "## [Unreleased]\n\n## [0.3.1] - 2026-10-26\n\n## [0.3.0] - 2026-10-26\n"

    def test_docs_only_changes_need_no_release(self):
        changed = ["README.md", "CLAUDE.md", "docs/api-design.md"]
        self.assertFalse(version.needs_release(changed))
        self.assertEqual(version.release_problems("0.3.0", "0.3.0", changed, ""), [])

    def test_workflows_and_tests_need_a_release(self):
        self.assertTrue(version.needs_release([".github/workflows/weekly.yml"]))
        self.assertTrue(version.needs_release(["tests/test_schedule.py"]))
        self.assertTrue(version.needs_release(["docs.py"]))  # not the docs/ folder

    def test_a_bump_with_a_dated_section_passes(self):
        self.assertEqual(version.release_problems("0.3.0", "0.3.1", ["x.py"], self.CHANGELOG), [])

    def test_a_forgotten_bump_fails(self):
        problems = version.release_problems("0.3.0", "0.3.0", ["x.py"], self.CHANGELOG)
        self.assertEqual(len(problems), 1)
        self.assertIn("bump __version__", problems[0])
        self.assertTrue(version.release_problems("0.3.1", "0.3.0", ["x.py"], self.CHANGELOG))  # a downgrade

    def test_a_bump_left_under_unreleased_fails(self):
        changelog = "## [Unreleased]\n\n### Fixed\n- x\n\n## [0.3.0] - 2026-10-26\n"
        problems = version.release_problems("0.3.0", "0.3.1", ["x.py"], changelog)
        self.assertEqual(len(problems), 1)
        self.assertIn("## [0.3.1] - YYYY-MM-DD", problems[0])
        undated = "## [Unreleased]\n\n## [0.3.1]\n"
        self.assertTrue(version.release_problems("0.3.0", "0.3.1", ["x.py"], undated))

    def test_minor_and_major_bumps_compare_as_numbers(self):
        changelog = "## [0.10.0] - 2026-10-26\n## [1.0.0] - 2026-10-26\n"
        self.assertEqual(version.release_problems("0.9.4", "0.10.0", ["x.py"], changelog), [])
        self.assertEqual(version.release_problems("0.10.0", "1.0.0", ["x.py"], changelog), [])


class RunsRecordTheCodeTest(unittest.TestCase):
    def test_a_run_records_the_version_that_made_it(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"SERMON_SHORTS_DB": str(Path(tmp) / "t.db")}):
            r = runs.begin("clips", [])
            runs.end(0)
            conn = connect()
            row = runs.get_run(conn, r.id)
            conn.close()
        self.assertEqual(row["code_version"], __version__)
        commit = version.commit()
        if commit:  # inside a git checkout
            self.assertTrue(row["code_commit"].startswith(commit))


if __name__ == "__main__":
    unittest.main()
