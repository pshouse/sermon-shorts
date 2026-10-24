"""Decisions, the publish gate, posts and the needs-you list.
Run: python -m unittest discover tests (standard library only)."""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from sermon_shorts.core import audit, posts, records, review, users
from sermon_shorts.core.db import connect


class ReviewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.conn = connect(self.dir / "t.db")
        self.owner, _ = users.init_root(self.conn, "pshouse", "Patrick S.", "sprite")
        self.approver = self.join("approver", "abe")
        self.reviewer = self.join("reviewer", "rita")
        self.folder = self.dir / "Live_2026-10-18_7am_sermon_clips"
        self.folder.mkdir()
        clips = []
        for i, seconds in ((1, 79), (2, 50)):
            (self.folder / f"0{i}_clip.mp4").write_bytes(b"video %d" % i)
            (self.folder / f"0{i}_clip.jpg").write_bytes(b"cover %d" % i)
            clips.append({"file": f"0{i}_clip.mp4", "title": f"Clip {i}", "start": 100.0,
                          "end": 100.0 + seconds, "score": 80, "description": f"About {i}"})
        (self.folder / "clips.json").write_text(json.dumps({"summary": "S", "clips": clips}))
        self.sid = "Live_2026-10-18_7am"

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def join(self, role, login):
        _, code = users.invite(self.conn, self.owner, role)
        return users.accept_invite(self.conn, code, login, login.title())[0]

    def state(self, n):
        entry = review.clip_at(self.folder, n)
        fp = records.manifest_fingerprint(self.folder, entry)
        return review.status(self.conn, self.sid, entry["file"], fp)[0]

    def plan(self, chosen=(1, 2), platforms=("youtube", "instagram", "facebook")):
        records.ensure_service(self.conn, self.owner.actor("cli"), self.folder)
        return posts.plan(self.conn, self.sid, self.folder, list(chosen), list(platforms))

    # --- decisions ---

    def test_new_clips_wait_for_review(self):
        records.ensure_service(self.conn, self.owner.actor("cli"), self.folder)
        self.assertEqual(self.state(1), review.PENDING)

    def test_reviewers_note_but_cannot_decide(self):
        review.add_note(self.conn, self.reviewer, self.folder, 1, "Check the verse reference")
        with self.assertRaises(review.ReviewError):
            review.decide(self.conn, self.reviewer, self.folder, 1, "approve")
        self.assertEqual(self.state(1), review.PENDING)

    def test_approve_then_reject_latest_wins(self):
        review.decide(self.conn, self.approver, self.folder, 1, "approve")
        self.assertEqual(self.state(1), review.APPROVED)
        review.decide(self.conn, self.owner, self.folder, 1, "reject", note="wrong verse")
        self.assertEqual(self.state(1), review.REJECTED)

    def test_rejecting_needs_a_reason(self):
        with self.assertRaises(review.ReviewError):
            review.decide(self.conn, self.approver, self.folder, 1, "reject")

    def test_any_change_after_approval_needs_a_new_one(self):
        review.decide(self.conn, self.approver, self.folder, 1, "approve")
        (self.folder / "01_clip.mp4").write_bytes(b"re-rendered")
        self.assertEqual(self.state(1), review.STALE)
        review.decide(self.conn, self.approver, self.folder, 1, "approve")
        self.assertEqual(self.state(1), review.APPROVED)
        m = json.loads((self.folder / "clips.json").read_text())
        m["clips"][0]["description"] = "Edited"
        (self.folder / "clips.json").write_text(json.dumps(m))
        self.assertEqual(self.state(1), review.STALE)

    def test_decisions_cannot_be_edited(self):
        review.decide(self.conn, self.approver, self.folder, 1, "approve")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE decisions SET decision = 'reject'")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM decisions")

    def test_backfilled_approvals_say_so(self):
        review.decide(self.conn, self.owner, self.folder, 1, "approve",
                      backfilled_from="2026-10-24T14:14:00Z")
        e = audit.events(self.conn, action="clip.approved")[0]
        self.assertEqual(e["data"]["backfilled_from"], "2026-10-24T14:14:00Z")

    # --- the gate ---

    def test_gate_posts_only_approved_clips(self):
        review.decide(self.conn, self.approver, self.folder, 2, "approve")
        jobs, skipped = self.plan()
        self.assertEqual({(j["n"], j["platform"]) for j in jobs},
                         {(2, "youtube"), (2, "instagram"), (2, "facebook")})
        self.assertEqual([(s["n"], s["kind"]) for s in skipped], [(1, "not_approved")])

    def test_gate_holds_instagram_over_the_limit_for_manual_posting(self):
        review.decide(self.conn, self.approver, self.folder, 1, "approve")
        jobs, skipped = self.plan(chosen=[1])
        self.assertEqual({j["platform"] for j in jobs}, {"youtube", "facebook"})
        self.assertEqual([(s["platform"], s["kind"]) for s in skipped],
                         [("instagram", "manual_required")])

    def test_gate_never_posts_twice(self):
        review.decide(self.conn, self.approver, self.folder, 2, "approve")
        posts.record_post(self.conn, self.approver.actor("cli"), self.sid, "02_clip.mp4",
                          "youtube", "abc", "https://youtube.com/shorts/abc", "fp")
        jobs, skipped = self.plan(chosen=[2])
        self.assertNotIn("youtube", {j["platform"] for j in jobs})
        with self.assertRaises(sqlite3.IntegrityError):
            posts.record_post(self.conn, self.approver.actor("cli"), self.sid, "02_clip.mp4",
                              "youtube", "def", "https://youtube.com/shorts/def", "fp")

    def test_gate_reads_posts_from_the_ledger_file_too(self):
        review.decide(self.conn, self.approver, self.folder, 2, "approve")
        (self.folder / "published.json").write_text(json.dumps(
            {"02_clip.mp4": {"facebook": {"id": "9", "url": "https://fb/9", "at": "x"}}}))
        jobs, _ = self.plan(chosen=[2])
        self.assertNotIn("facebook", {j["platform"] for j in jobs})

    def test_assume_approved_previews_without_recording(self):
        records.ensure_service(self.conn, self.owner.actor("cli"), self.folder)
        jobs, _ = posts.plan(self.conn, self.sid, self.folder, [2], ["youtube"],
                             assume_approved={2})
        self.assertEqual(len(jobs), 1)
        self.assertEqual(self.state(2), review.PENDING)

    # --- posts and covers ---

    def test_ledger_import_is_idempotent_and_keeps_old_posts_anonymous(self):
        (self.folder / "published.json").write_text(json.dumps(
            {"01_clip.mp4": {"youtube": {"id": "H19", "url": "https://youtube.com/shorts/H19",
                                         "at": "2026-10-24T10:20:00"}}}))
        records.ensure_service(self.conn, self.owner.actor("cli"), self.folder)
        records.ensure_service(self.conn, self.owner.actor("cli"), self.folder)
        rows = posts.posts_for(self.conn, self.sid)
        self.assertEqual(list(rows), ["01_clip.mp4"])
        self.assertIsNone(rows["01_clip.mp4"]["youtube"]["by_login"])
        self.assertEqual(len(audit.events(self.conn, action="post.imported")), 1)

    def test_cover_failure_and_retry(self):
        review.decide(self.conn, self.approver, self.folder, 2, "approve")
        posts.record_post(self.conn, self.approver.actor("cli"), self.sid, "02_clip.mp4",
                          "youtube", "abc", "u", "fp", cover="failed")
        kinds = [i["kind"] for i in posts.needs_you(self.conn, self.sid, self.folder)]
        self.assertIn("cover", kinds)
        posts.record_cover(self.conn, self.approver.actor("cli"), self.sid, "02_clip.mp4",
                           "youtube", True)
        kinds = [i["kind"] for i in posts.needs_you(self.conn, self.sid, self.folder)]
        self.assertNotIn("cover", kinds)

    # --- needs you ---

    def test_needs_you_walks_a_clip_through_its_life(self):
        records.ensure_service(self.conn, self.owner.actor("cli"), self.folder)
        kinds = lambda: [(i["n"], i["kind"]) for i in
                         posts.needs_you(self.conn, self.sid, self.folder)]
        self.assertEqual(kinds(), [(1, "review"), (2, "review")])
        review.decide(self.conn, self.approver, self.folder, 1, "approve")
        review.decide(self.conn, self.approver, self.folder, 2, "reject", note="no")
        self.assertEqual(kinds(), [(1, "publish")])
        posts.record_post(self.conn, self.approver.actor("cli"), self.sid, "01_clip.mp4",
                          "youtube", "a", "u", "fp")
        self.assertEqual(kinds(), [(1, "manual")])  # 79s: Instagram by hand
        posts.record_post(self.conn, self.approver.actor("cli"), self.sid, "01_clip.mp4",
                          "instagram", None, "https://instagram.com/reel/x", "fp", manual=True)
        self.assertEqual(kinds(), [])


if __name__ == "__main__":
    unittest.main()
