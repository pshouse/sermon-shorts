"""The posting queue: spreading clips over the week, and posting what is due.
Run: python -m unittest discover tests (standard library only)."""

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from sermon_shorts.core import audit, posts, records, review, schedule, scheduled, users
from sermon_shorts.core.db import connect

NY = schedule.ZoneInfo("America/New_York")
SUNDAY_EVENING = datetime(2026, 11, 1, 22, 0, tzinfo=timezone.utc)   # 17:00 EST, Nov 1
NOON = lambda day: datetime(2026, 11, day, 17, 0, tzinfo=timezone.utc)  # 12:00 EST


def fake_poster(conn, actor, sid, folder, jobs, progress=None):
    """Records posts the way publish.post_jobs does, without uploading."""
    ledger = posts.load_ledger(folder)
    out = []
    for job in jobs:
        file, platform = job["entry"]["file"], job["platform"]
        if platform == "facebook" and job["entry"]["title"] == "Refused":
            posts.record_failure(conn, actor, sid, file, platform, "no")
            out.append({"n": job["n"], "file": file, "title": job["entry"]["title"],
                        "platform": platform, "url": None, "cover": None, "error": "no"})
            continue
        url = f"https://{platform}.example/{file}"
        posts.write_ledger_entry(folder, ledger, file, platform,
                                 {"id": "x", "url": url, "at": "now", "by": actor.login})
        posts.record_post(conn, actor, sid, file, platform, "x", url, job["fingerprint"])
        out.append({"n": job["n"], "file": file, "title": job["entry"]["title"],
                    "platform": platform, "url": url, "cover": None, "error": None})
    return out


class ScheduledTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.conn = connect(self.dir / "t.db")
        self.owner, _ = users.init_root(self.conn, "pshouse", "Patrick S.", "test")
        _, code = users.invite(self.conn, self.owner, "approver")
        self.approver = users.accept_invite(self.conn, code, "abe", "Abe")[0]
        _, code = users.invite(self.conn, self.owner, "reviewer")
        self.reviewer = users.accept_invite(self.conn, code, "rita", "Rita")[0]
        self.tz = mock.patch.object(schedule, "church_tz", lambda: NY)
        self.tz.start()
        self.folder = self.dir / "Live_2026-11-01_7am_sermon_clips"
        self.folder.mkdir()
        clips = []
        for i in (1, 2, 3):
            (self.folder / f"0{i}_clip.mp4").write_bytes(b"video %d" % i)
            (self.folder / f"0{i}_clip.jpg").write_bytes(b"cover %d" % i)
            clips.append({"file": f"0{i}_clip.mp4", "title": f"Clip {i}", "start": 0.0,
                          "end": 50.0, "score": 80, "description": f"About {i}"})
        (self.folder / "clips.json").write_text(json.dumps({"summary": "S", "clips": clips}))
        self.sid = records.ensure_service(self.conn, self.owner.actor("cli"), self.folder)

    def tearDown(self):
        self.tz.stop()
        self.conn.close()
        self.tmp.cleanup()

    def plan(self, *numbers, platforms=("youtube", "facebook"), **kw):
        return posts.plan(self.conn, self.sid, self.folder, list(numbers), list(platforms), **kw)

    def queue(self, *numbers, now=SUNDAY_EVENING, user=None):
        jobs, _ = self.plan(*numbers)
        times = dict(zip(numbers, scheduled.spread(len(numbers), now)))
        return scheduled.schedule_posts(self.conn, user or self.approver, self.sid, jobs, times,
                                        now=now)

    # --- picking the days ---

    def test_spread_puts_one_clip_a_day_over_the_week(self):
        local = lambda n: [t.astimezone(NY).strftime("%a %d %H:%M") for t in scheduled.spread(n, SUNDAY_EVENING)]
        self.assertEqual(local(1), ["Mon 02 12:00"])
        self.assertEqual(local(2), ["Mon 02 12:00", "Thu 05 12:00"])
        self.assertEqual(local(3), ["Mon 02 12:00", "Wed 04 12:00", "Fri 06 12:00"])
        self.assertEqual(local(6), ["Mon 02 12:00", "Tue 03 12:00", "Wed 04 12:00",
                                    "Thu 05 12:00", "Fri 06 12:00", "Sat 07 12:00"])
        # More clips than days left: still one a day, skipping Sunday.
        self.assertEqual(local(8)[5:], ["Sat 07 12:00", "Mon 09 12:00", "Tue 10 12:00"])

    def test_spread_from_midweek_uses_the_days_left(self):
        thursday_morning = datetime(2026, 11, 5, 15, 0, tzinfo=timezone.utc)   # 10:00 EST
        days = [t.astimezone(NY).strftime("%a") for t in scheduled.spread(3, thursday_morning)]
        self.assertEqual(days, ["Thu", "Fri", "Sat"])
        thursday_afternoon = datetime(2026, 11, 5, 18, 0, tzinfo=timezone.utc)  # 13:00 EST
        days = [t.astimezone(NY).strftime("%a") for t in scheduled.spread(3, thursday_afternoon)]
        self.assertEqual(days, ["Fri", "Sat", "Mon"])  # today's noon has passed

    def test_the_posting_workflow_checks_at_noon_eastern_in_both_seasons(self):
        crons = schedule.crons(schedule.POSTING_WORKFLOW)
        self.assertTrue(crons, "no cron line in .github/workflows/posting.yml")
        for day in ("2026-10-26", "2026-11-09"):  # a Monday in daylight time, one after
            start = datetime.fromisoformat(day + "T00:00:00+00:00")
            local = {t.astimezone(NY).strftime("%H:%M")
                     for t in schedule.fire_times(start, days=1, cron_lines=crons)}
            self.assertIn(f"{scheduled.POST_HOUR:02d}:{scheduled.POST_MINUTE:02d}", local, day)
        sunday = datetime(2026, 11, 8, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(schedule.fire_times(sunday, days=1, cron_lines=crons), [])

    # --- the queue and the gate ---

    def test_queued_posts_leave_the_plan_until_taken_out(self):
        review.decide(self.conn, self.approver, self.folder, 2, "approve")
        rows = self.queue(2)
        self.assertEqual([(r["platform"], r["post_at"], r["status"]) for r in rows],
                         [("youtube", "2026-11-02T17:00:00Z", "waiting"),
                          ("facebook", "2026-11-02T17:00:00Z", "waiting")])
        jobs, skipped = self.plan(2)
        self.assertEqual(jobs, [])
        self.assertEqual({(s["kind"], s["platform"]) for s in skipped},
                         {("scheduled", "youtube"), ("scheduled", "facebook")})
        self.assertIn("Mon 2026-11-02 12:00 by abe", skipped[0]["reason"])
        self.assertNotIn("publish", [i["kind"] for i in posts.needs_you(self.conn, self.sid, self.folder)])
        self.assertEqual(len(scheduled.upcoming(self.conn)), 2)
        self.assertEqual(self.queue(2), [])  # the plan leaves nothing to queue twice

        scheduled.cancel(self.conn, self.approver, self.sid, "02_clip.mp4", "youtube", "changed my mind")
        self.assertEqual([j["platform"] for j in self.plan(2)[0]], ["youtube"])
        scheduled.cancel(self.conn, self.approver, self.sid, "02_clip.mp4")
        self.assertEqual(len(self.plan(2)[0]), 2)
        self.assertEqual(scheduled.upcoming(self.conn), [])
        self.assertEqual([i["kind"] for i in posts.needs_you(self.conn, self.sid, self.folder)],
                         ["review", "publish", "review"])
        actions = [e["action"] for e in audit.events(self.conn, service=self.sid, action="post.")]
        self.assertEqual(actions, ["post.schedule_cancelled"] * 2 + ["post.scheduled"] * 2)
        with self.assertRaises(scheduled.ScheduledError):
            scheduled.cancel(self.conn, self.approver, self.sid, "02_clip.mp4")

    def test_only_approvers_queue_and_past_times_are_refused(self):
        review.decide(self.conn, self.approver, self.folder, 1, "approve")
        with self.assertRaises(scheduled.ScheduledError):
            self.queue(1, user=self.reviewer)
        jobs, _ = self.plan(1)
        with self.assertRaises(scheduled.ScheduledError):
            scheduled.schedule_posts(self.conn, self.approver, self.sid, jobs,
                                     {1: datetime(2020, 1, 1, tzinfo=timezone.utc)})
        self.assertEqual(scheduled.upcoming(self.conn), [])

    # --- posting what is due ---

    def test_due_posts_go_out_as_their_scheduler(self):
        for n in (1, 2):
            review.decide(self.conn, self.approver, self.folder, n, "approve")
        self.queue(1, 2)  # Monday and Thursday
        self.assertEqual(scheduled.post_due(self.conn, NOON(2).replace(hour=16), fake_poster), [])
        done = scheduled.post_due(self.conn, NOON(2), fake_poster)
        self.assertEqual([(r["row"]["file"], r["status"]) for r in done],
                         [("01_clip.mp4", "posted"), ("01_clip.mp4", "posted")])
        mine = posts.posts_for(self.conn, self.sid)["01_clip.mp4"]
        self.assertEqual((mine["youtube"]["by_login"], mine["youtube"]["via"]), ("abe", "schedule"))
        self.assertEqual(posts.load_ledger(self.folder)["01_clip.mp4"]["facebook"]["by"], "abe")
        self.assertEqual(scheduled.latest(self.conn, self.sid).get("01_clip.mp4"), None)  # done
        self.assertEqual(len(scheduled.upcoming(self.conn)), 2)  # Thursday's still waits
        self.assertEqual(scheduled.post_due(self.conn, NOON(2), fake_poster), [])  # never twice
        done = scheduled.post_due(self.conn, NOON(5), fake_poster)
        self.assertEqual([r["row"]["file"] for r in done], ["02_clip.mp4", "02_clip.mp4"])
        self.assertEqual(scheduled.upcoming(self.conn), [])

    def test_a_clip_that_changed_or_went_out_elsewhere_is_held_not_posted(self):
        for n in (1, 2, 3):
            review.decide(self.conn, self.approver, self.folder, n, "approve")
        self.queue(1, 2, 3)  # Mon, Wed, Fri
        (self.folder / "01_clip.mp4").write_bytes(b"re-rendered")  # the approval no longer covers it
        posts.record_post(self.conn, self.owner.actor("cli"), self.sid, "02_clip.mp4", "youtube",
                          "y", "https://youtube.example/by-hand", None, manual=True)
        done = {(r["row"]["file"], r["row"]["platform"]): r for r in
                scheduled.post_due(self.conn, NOON(6), fake_poster)}
        self.assertEqual(done[("01_clip.mp4", "youtube")]["status"], "held")
        self.assertIn("needs approving again", done[("01_clip.mp4", "youtube")]["outcome"])
        self.assertEqual(done[("02_clip.mp4", "youtube")]["status"], "held")
        self.assertIn("already", done[("02_clip.mp4", "youtube")]["outcome"])
        self.assertEqual(done[("02_clip.mp4", "facebook")]["status"], "posted")
        self.assertEqual(done[("03_clip.mp4", "facebook")]["status"], "posted")
        shown = scheduled.latest(self.conn, self.sid)
        self.assertEqual(shown["01_clip.mp4"]["youtube"]["status"], "held")
        self.assertNotIn("03_clip.mp4", shown)
        held = audit.events(self.conn, service=self.sid, action="post.schedule_held")
        self.assertEqual(len(held), 3)
        self.assertEqual(held[0]["actor"], "abe")
        # Held rows are owed again: the clip shows as needing review / posting.
        kinds = [(i["file"], i["kind"]) for i in posts.needs_you(self.conn, self.sid, self.folder)]
        self.assertIn(("01_clip.mp4", "review"), kinds)

    def test_a_refused_post_is_recorded_as_failed(self):
        clips = json.loads((self.folder / "clips.json").read_text())
        clips["clips"][0]["title"] = "Refused"
        (self.folder / "clips.json").write_text(json.dumps(clips))
        review.decide(self.conn, self.approver, self.folder, 1, "approve")
        self.queue(1)
        done = {r["row"]["platform"]: r for r in scheduled.post_due(self.conn, NOON(2), fake_poster)}
        self.assertEqual((done["facebook"]["status"], done["facebook"]["outcome"]), ("failed", "no"))
        self.assertEqual(done["youtube"]["status"], "posted")
        self.assertEqual(posts.failures(self.conn, self.sid)["01_clip.mp4"]["facebook"]["error"], "no")

    def test_a_scheduler_who_left_cannot_post(self):
        review.decide(self.conn, self.approver, self.folder, 1, "approve")
        self.queue(1)
        users.remove(self.conn, self.owner, "abe")
        done = scheduled.post_due(self.conn, NOON(2), fake_poster)
        self.assertEqual({r["status"] for r in done}, {"held"})
        self.assertIn("no longer a member", done[0]["outcome"])
        self.assertEqual(posts.posts_for(self.conn, self.sid), {})

    def test_a_claimed_row_is_left_alone_until_the_claim_goes_stale(self):
        review.decide(self.conn, self.approver, self.folder, 1, "approve")
        row = self.queue(1)[0]
        claim = lambda at: self.conn.execute(
            "UPDATE scheduled_posts SET status = 'posting', claimed_at = ? WHERE id = ?",
            (at, row["id"]))
        claim("2026-11-02T16:30:00Z")  # another --due is posting it right now
        self.assertEqual([r["id"] for r in scheduled.due(self.conn, NOON(2))], [row["id"] + 1])
        claim("2026-11-02T10:00:00Z")  # a --due that died hours ago
        self.assertEqual([r["id"] for r in scheduled.due(self.conn, NOON(2))],
                         [row["id"], row["id"] + 1])

    def test_posting_overview(self):
        o = scheduled.posting_overview(self.conn, SUNDAY_EVENING)
        self.assertEqual((o["time"], o["days"][0], o["days"][-1], o["waiting"]), ("12:00", "Mon", "Sat", 0))
        self.assertEqual(o["next_check"], "2026-11-02T16:00:00Z")
        self.assertTrue(o["workflow"].endswith("posting.yml"))


if __name__ == "__main__":
    unittest.main()
