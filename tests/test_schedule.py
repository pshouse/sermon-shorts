"""The Sunday schedule. Run: python -m unittest discover tests (standard library only)."""

import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from sermon_shorts.core import audit, schedule, users
from sermon_shorts.core.db import connect

SUNDAYS = ['0,30 16-19 * * 0']


class ScheduleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self.tmp.name) / "t.db")
        self.owner, _ = users.init_root(self.conn, "pshouse", "Patrick S.", "test")
        _, code = users.invite(self.conn, self.owner, "approver")
        self.approver = users.accept_invite(self.conn, code, "abe", "Abe")[0]
        self.crons = mock.patch.object(schedule, "crons", lambda: SUNDAYS)
        self.crons.start()
        self.tz = mock.patch.object(schedule, "church_tz", lambda: schedule.ZoneInfo("America/New_York"))
        self.tz.start()

    def tearDown(self):
        self.crons.stop()
        self.tz.stop()
        self.conn.close()
        self.tmp.cleanup()

    def test_the_real_workflow_has_a_schedule(self):
        self.crons.stop()
        try:
            self.assertTrue(schedule.crons(), "no cron line found in .github/workflows/weekly.yml")
        finally:
            self.crons.start()

    def test_fire_times_follow_the_cron(self):
        start = datetime(2026, 10, 25, 17, 10, tzinfo=timezone.utc)  # a Sunday, mid-window
        times = schedule.fire_times(start, days=8)
        self.assertEqual(times[0], datetime(2026, 10, 25, 17, 30, tzinfo=timezone.utc))
        self.assertEqual(len(times), 5 + 8)  # rest of today, then next Sunday's 8
        self.assertTrue(all(t.weekday() == 6 for t in times))

    def test_upcoming_groups_by_church_day_and_shows_dst(self):
        start = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
        up = schedule.upcoming(self.conn, count=2, now=start)
        self.assertEqual([u["date"] for u in up], ["2026-10-25", "2026-11-01"])
        self.assertEqual([u["checks"] for u in up], [8, 8])
        ny = schedule.ZoneInfo("America/New_York")
        first = [datetime.fromisoformat(u["first"].replace("Z", "+00:00")).astimezone(ny).hour for u in up]
        self.assertEqual(first, [12, 11])  # the UTC cron drifts an hour when daylight time ends

    def test_pause_holds_scheduled_runs_until_resumed(self):
        self.assertIsNone(schedule.holds(self.conn))
        schedule.set_paused(self.conn, self.owner, True, "holiday break")
        self.assertIn("holiday break", schedule.holds(self.conn))
        with self.assertRaises(schedule.ScheduleError):
            schedule.set_paused(self.conn, self.owner, True)
        schedule.set_paused(self.conn, self.owner, False)
        self.assertIsNone(schedule.holds(self.conn))

    def test_a_skipped_date_holds_only_that_day(self):
        schedule.skip(self.conn, self.owner, "2099-11-01", "combined service")
        on = datetime(2099, 11, 1, 17, 0, tzinfo=timezone.utc)
        off = datetime(2099, 11, 8, 17, 0, tzinfo=timezone.utc)
        self.assertIn("combined service", schedule.holds(self.conn, on))
        self.assertIsNone(schedule.holds(self.conn, off))
        with self.assertRaises(schedule.ScheduleError):
            schedule.skip(self.conn, self.owner, "2099-11-01")
        schedule.unskip(self.conn, self.owner, "2099-11-01")
        self.assertIsNone(schedule.holds(self.conn, on))

    def test_skipped_dates_count_in_the_church_timezone(self):
        schedule.skip(self.conn, self.owner, "2099-11-01")
        late_saturday_in_ny = datetime(2099, 11, 1, 2, 0, tzinfo=timezone.utc)  # 10 PM Oct 31 ET
        self.assertIsNone(schedule.holds(self.conn, late_saturday_in_ny))

    def test_only_owners_change_it_and_bad_dates_are_refused(self):
        with self.assertRaises(schedule.ScheduleError):
            schedule.set_paused(self.conn, self.approver, True)
        with self.assertRaises(schedule.ScheduleError):
            schedule.skip(self.conn, self.approver, "2099-11-01")
        for bad in ("2000-01-02", "next sunday"):
            with self.assertRaises(schedule.ScheduleError):
                schedule.skip(self.conn, self.owner, bad)

    def test_changes_are_audited(self):
        schedule.set_paused(self.conn, self.owner, True, "x")
        schedule.skip(self.conn, self.owner, "2099-11-01")
        actions = [e["action"] for e in audit.events(self.conn, action="schedule.")]
        self.assertEqual(actions, ["schedule.date_skipped", "schedule.paused"])


class ScheduledRunsObeyTest(unittest.TestCase):
    """The pipeline's check: a scheduled --weekly run is held, a person's isn't."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"SERMON_SHORTS_DB": str(Path(self.tmp.name) / "t.db")})
        self.env.start()
        conn = connect()
        owner, _ = users.init_root(conn, "pshouse", "P", "test")
        schedule.set_paused(conn, owner, True, "testing")
        conn.close()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def run_weekly(self, started_by):
        try:
            from sermon_shorts import cli  # needs the pipeline's dependencies
        except ImportError as e:
            self.skipTest(f"pipeline dependencies not installed: {e}")
        from sermon_shorts.core import runs
        env = {"SERMON_SHORTS_STARTED_BY": started_by} if started_by else {}
        with mock.patch.dict(os.environ, env), \
                mock.patch("sermon_shorts.cli._load_church", lambda: {"subsplash": "x"}), \
                mock.patch("sermon_shorts.subsplash.latest_recording",
                           side_effect=RuntimeError("fetched")) as fetch:
            try:
                rc = cli.main(["--weekly"])
            except RuntimeError:
                rc = "fetched"
        conn = connect()
        last = runs.list_runs(conn, 1)[0]
        conn.close()
        return rc, last, fetch.called

    def test_scheduled_run_is_held(self):
        rc, run, fetched = self.run_weekly("schedule")
        self.assertEqual((rc, run["status"], fetched), (0, "skipped", False))
        self.assertIn("testing", run["outcome"])

    def test_a_person_started_run_goes_ahead(self):
        _, run, fetched = self.run_weekly(None)
        self.assertTrue(fetched)


if __name__ == "__main__":
    unittest.main()
