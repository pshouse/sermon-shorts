"""The HTTP API, through FastAPI's test client. Skipped where FastAPI isn't
installed (the rest of the suite needs only the standard library).
Uploads are replaced by fakes: nothing is ever posted."""

import json
import os
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

try:
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover
    TestClient = None

from sermon_shorts.core import records, users
from sermon_shorts.core.db import connect

CSRF = {"X-Requested-With": "sermon-shorts"}


@unittest.skipIf(TestClient is None, "FastAPI not installed")
class ApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {
            "SERMON_SHORTS_DB": str(self.dir / "t.db"), "SERMON_SHORTS_DATA": str(self.dir),
            "SERMON_SHORTS_LOG_DIR": str(self.dir / "logs"),
            "SERMON_SHORTS_INSECURE_COOKIE": "1"})
        self.env.start()
        for key in ("SERMON_SHORTS_USER", "SERMON_SHORTS_STARTED_BY", "SERMON_SHORTS_GITHUB_RUN",
                    "GITHUB_TOKEN", "SERMON_SHORTS_RUN_CMD"):
            os.environ.pop(key, None)
        conn = connect()
        self.owner, self.owner_token = users.init_root(conn, "pshouse", "Patrick S.", "test")
        _, code = users.invite(conn, self.owner, "reviewer")
        _, self.reviewer_token = users.accept_invite(conn, code, "rita", "Rita")
        _, code = users.invite(conn, self.owner, "approver")
        _, self.approver_token = users.accept_invite(conn, code, "abe", "Abe")
        self.folder = self.dir / "Live_2026-10-18_7am_sermon_clips"
        self.folder.mkdir()
        clips = []
        for i, seconds in ((1, 79), (2, 50)):
            (self.folder / f"0{i}_clip.mp4").write_bytes(b"video %d" % i)
            (self.folder / f"0{i}_clip.jpg").write_bytes(b"cover %d" % i)
            clips.append({"file": f"0{i}_clip.mp4", "title": f"Clip {i}", "start": 0.0,
                          "end": float(seconds), "score": 80, "description": f"About {i}"})
        (self.folder / "clips.json").write_text(json.dumps({"summary": "S", "clips": clips}))
        records.import_folder(conn, self.owner.actor("cli"), self.folder)
        conn.close()
        self.sid = "Live_2026-10-18_7am"

        from sermon_shorts import api, publish
        self.posted = []

        def fake(platform):
            def post(clip, mp4, jpg):
                self.posted.append((clip["file"], platform))
                return f"{platform}-id", f"https://{platform}.example/{clip['file']}", "set"
            return post
        self.patches = [
            mock.patch.dict(publish.PUBLISHERS, {p: fake(p) for p in publish.PLATFORMS}),
            mock.patch.object(publish, "_configured", lambda: ["youtube", "instagram", "facebook"]),
        ]
        for p in self.patches:
            p.start()
        api._jobs.clear()
        api._spawned.clear()
        self.client = TestClient(api.create_app())

    def tearDown(self):
        from sermon_shorts import api
        for proc in api._spawned.values():
            if proc.poll() is None:
                proc.kill()
            proc.wait(10)
        for p in self.patches:
            p.stop()
        self.env.stop()
        self.tmp.cleanup()

    def as_(self, token):
        return {"Authorization": f"Bearer {token}"}

    def clip(self, file, token=None):
        r = self.client.get(f"/api/v1/services/{self.sid}/clips/{file}",
                            headers=self.as_(token or self.owner_token))
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    # --- signing in ---

    def test_everything_but_health_needs_a_token(self):
        self.assertEqual(self.client.get("/api/v1/health").status_code, 200)
        r = self.client.get("/api/v1/services")
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (401, "not_signed_in"))
        r = self.client.get("/api/v1/services", headers=self.as_("ssk_wrong"))
        self.assertEqual(r.status_code, 401)

    def test_cookie_sessions_need_the_csrf_header_to_write(self):
        r = self.client.post("/api/v1/session", json={"token": self.reviewer_token})
        self.assertEqual((r.status_code, r.json()["role"]), (200, "reviewer"))
        self.assertEqual(self.client.get("/api/v1/me").json()["login"], "rita")
        path = f"/api/v1/services/{self.sid}/clips/01_clip.mp4/notes"
        r = self.client.post(path, json={"text": "hi"})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (403, "csrf"))
        self.assertEqual(self.client.post(path, json={"text": "hi"}, headers=CSRF).status_code, 201)
        self.client.delete("/api/v1/session", headers=CSRF)
        self.assertEqual(self.client.get("/api/v1/me").status_code, 401)

    def test_invite_accept_and_roles(self):
        r = self.client.post("/api/v1/invites", json={"role": "approver"},
                             headers=self.as_(self.approver_token))
        self.assertEqual(r.status_code, 403)
        code = self.client.post("/api/v1/invites", json={"role": "reviewer", "note": "Sam"},
                                headers=self.as_(self.owner_token)).json()["code"]
        r = self.client.post("/api/v1/invites/accept",
                             json={"code": code, "login": "sam", "name": "Sam"})
        self.assertEqual((r.status_code, r.json()["role"]), (201, "reviewer"))
        self.assertTrue(r.json()["token"].startswith("ssk_"))
        r = self.client.patch("/api/v1/users/sam", json={"role": "approver"},
                              headers=self.as_(self.owner_token))
        self.assertEqual(r.json()["role"], "approver")
        r = self.client.delete("/api/v1/users/pshouse", headers=self.as_(self.owner_token))
        self.assertEqual(r.status_code, 400)  # the root account stays

    # --- reviewing ---

    def test_service_view_has_clips_media_and_needs_you(self):
        s = self.client.get(f"/api/v1/services/{self.sid}", headers=self.as_(self.reviewer_token)).json()
        self.assertEqual([c["review_state"] for c in s["clips"]], ["pending", "pending"])
        self.assertEqual(s["clips"][0]["video_url"],
                         "/Live_2026-10-18_7am_sermon_clips/01_clip.mp4")
        self.assertTrue(s["clips"][0]["instagram_needs_manual"])
        self.assertEqual([i["kind"] for i in s["needs_you"]], ["review", "review"])

    def test_decisions_need_the_version_that_was_watched(self):
        fp = self.clip("01_clip.mp4")["fingerprint"]
        path = f"/api/v1/services/{self.sid}/clips/01_clip.mp4/decisions"
        r = self.client.post(path, json={"decision": "approve", "fingerprint": fp},
                             headers=self.as_(self.reviewer_token))
        self.assertEqual(r.status_code, 403)
        (self.folder / "01_clip.mp4").write_bytes(b"re-rendered while watching")
        r = self.client.post(path, json={"decision": "approve", "fingerprint": fp},
                             headers=self.as_(self.approver_token))
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "approval_stale"))
        fp = self.clip("01_clip.mp4")["fingerprint"]
        r = self.client.post(path, json={"decision": "approve", "fingerprint": fp},
                             headers=self.as_(self.approver_token))
        self.assertEqual((r.status_code, r.json()["clip"]["review_state"]), (201, "approved"))
        r = self.client.post(path, json={"decision": "reject", "fingerprint": fp},
                             headers=self.as_(self.approver_token))
        self.assertEqual(r.status_code, 400)  # a rejection needs a note

    # --- publishing ---

    def approve(self, file):
        fp = self.clip(file)["fingerprint"]
        r = self.client.post(f"/api/v1/services/{self.sid}/clips/{file}/decisions",
                             json={"decision": "approve", "fingerprint": fp},
                             headers=self.as_(self.approver_token))
        self.assertEqual(r.status_code, 201, r.text)

    def test_plan_then_publish_posts_exactly_the_plan(self):
        self.approve("01_clip.mp4")
        base = f"/api/v1/services/{self.sid}"
        body = {"clips": ["01_clip.mp4", "02_clip.mp4"]}
        plan = self.client.post(f"{base}/publish-plans", json=body,
                                headers=self.as_(self.approver_token)).json()
        self.assertEqual({(p["file"], p["platform"]) for p in plan["posts"]},
                         {("01_clip.mp4", "youtube"), ("01_clip.mp4", "facebook")})
        self.assertEqual({(s["file"], s["kind"]) for s in plan["skipped"]},
                         {("01_clip.mp4", "manual_required"), ("02_clip.mp4", "not_approved")})

        self.approve("02_clip.mp4")  # the world moved on since the preview
        r = self.client.post(f"{base}/publish", json={**body, "plan_id": plan["plan_id"]},
                             headers=self.as_(self.approver_token))
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "plan_changed"))
        self.assertEqual(self.posted, [])

        plan = self.client.post(f"{base}/publish-plans", json=body,
                                headers=self.as_(self.approver_token)).json()
        r = self.client.post(f"{base}/publish", json={**body, "plan_id": plan["plan_id"]},
                             headers=self.as_(self.approver_token))
        self.assertEqual(r.status_code, 202, r.text)
        job = self.wait_job(r.json()["job_id"])
        self.assertEqual(job["status"], "succeeded")
        self.assertEqual(len(self.posted), 5)  # 2 + 3 platforms, minus Instagram for the 79s clip
        s = self.client.get(f"{base}", headers=self.as_(self.reviewer_token)).json()
        self.assertEqual(s["clips"][0]["posts"]["youtube"]["by_login"], "abe")
        self.assertEqual([i["kind"] for i in s["needs_you"]], ["manual"])

        r = self.client.post(f"{base}/clips/01_clip.mp4/posts/instagram/manual",
                             json={"url": "https://instagram.com/reel/x"},
                             headers=self.as_(self.approver_token))
        self.assertEqual(r.status_code, 201)
        s = self.client.get(f"{base}", headers=self.as_(self.reviewer_token)).json()
        self.assertEqual(s["needs_you"], [])

    def wait_job(self, job_id):
        for _ in range(100):
            job = self.client.get(f"/api/v1/publish-jobs/{job_id}",
                                  headers=self.as_(self.reviewer_token)).json()
            if job["status"] != "running":
                return job
            time.sleep(0.05)
        self.fail("publish job never finished")

    def test_reviewers_cannot_publish(self):
        r = self.client.post(f"/api/v1/services/{self.sid}/publish-plans",
                             json={"clips": ["01_clip.mp4"]}, headers=self.as_(self.reviewer_token))
        self.assertEqual(r.status_code, 403)

    # --- runs ---

    def fake_run_cmd(self, body):
        script = self.dir / "fake_run.py"
        script.write_text(textwrap.dedent(body))
        os.environ["SERMON_SHORTS_RUN_CMD"] = f"{sys.executable} {script}"

    def wait_run(self, run_id, until):
        for _ in range(200):
            run = self.client.get(f"/api/v1/runs/{run_id}",
                                  headers=self.as_(self.owner_token)).json()
            if run.get("status") in until:
                return run
            time.sleep(0.05)
        self.fail(f"run stayed {run.get('status')}")

    def test_owner_starts_a_run_that_records_itself(self):
        self.fake_run_cmd("""
            import sys; sys.path.insert(0, %r)
            from sermon_shorts.core import runs
            r = runs.begin("weekly", sys.argv[1:])
            r.step("fetch", title="Fake")
            runs.end(0)
        """ % str(Path(__file__).resolve().parent.parent))
        r = self.client.post("/api/v1/runs", json={}, headers=self.as_(self.approver_token))
        self.assertEqual(r.status_code, 403)  # runs are for owners
        r = self.client.post("/api/v1/runs", json={"labeler": "jev"},
                             headers=self.as_(self.owner_token))
        self.assertEqual(r.status_code, 202, r.text)
        run = self.wait_run(r.json()["run_id"], ("succeeded", "failed"))
        self.assertEqual((run["status"], run["started_by"]), ("succeeded", "pshouse"))
        self.assertIn("--labeler", json.loads(run["args"]))
        self.assertEqual([e["step"] for e in run["events"]], ["fetch"])

    def test_cancel_stops_a_run(self):
        self.fake_run_cmd("""
            import sys, time; sys.path.insert(0, %r)
            from sermon_shorts.core import runs
            runs.begin("weekly", [])
            time.sleep(60)
        """ % str(Path(__file__).resolve().parent.parent))
        run_id = self.client.post("/api/v1/runs", json={},
                                  headers=self.as_(self.owner_token)).json()["run_id"]
        self.wait_run(run_id, ("running",))
        r = self.client.post("/api/v1/runs", json={}, headers=self.as_(self.owner_token))
        self.assertEqual(r.json()["error"]["code"], "run_active")
        r = self.client.post(f"/api/v1/runs/{run_id}/cancel", headers=self.as_(self.owner_token))
        self.assertEqual(r.status_code, 202)
        from sermon_shorts import api
        api._spawned[run_id].wait(10)
        conn = connect()
        try:
            from sermon_shorts.core import runs
            runs.running(conn)  # reaps the killed run
            self.assertEqual(runs.get_run(conn, run_id)["status"], "failed")
        finally:
            conn.close()

    def test_run_logs_are_only_read_from_the_log_folder(self):
        conn = connect()
        conn.execute("INSERT INTO runs (id, kind, args, status, started_at, started_by, via, "
                     "log_path) VALUES ('r_x', 'weekly', '[]', 'succeeded', 'now', 'x', 'cli', ?)",
                     (str(self.dir / "t.db"),))
        conn.close()
        r = self.client.get("/api/v1/runs/r_x/log", headers=self.as_(self.owner_token))
        self.assertEqual(r.status_code, 404)

    # --- the review app ---

    def test_app_is_served_with_a_strict_content_policy(self):
        r = self.client.get("/app", follow_redirects=False)
        self.assertEqual((r.status_code, r.headers["location"]), (307, "/app/"))
        r = self.client.get("/app/")
        self.assertEqual(r.status_code, 200)
        self.assertIn('<script src="app.js" defer></script>', r.text)
        csp = r.headers["content-security-policy"]
        self.assertIn("script-src 'self'", csp)
        self.assertNotIn("unsafe-inline", csp)
        self.assertEqual(self.client.get("/app/app.js").status_code, 200)
        # The API itself doesn't need the page policy.
        self.assertNotIn("content-security-policy", self.client.get("/api/v1/health").headers)

    # --- audit ---

    def test_audit_lists_exports_and_verifies(self):
        self.approve("02_clip.mp4")
        h = self.as_(self.reviewer_token)
        events = self.client.get("/api/v1/audit?action=clip.", headers=h).json()
        self.assertEqual(events[0]["action"], "clip.approved")
        self.assertEqual(events[0]["via"], "web")
        csv_text = self.client.get("/api/v1/audit.csv", headers=h).text
        self.assertTrue(csv_text.startswith("seq,at,actor"))
        self.assertTrue(self.client.get("/api/v1/audit/verify", headers=h).json()["ok"])


if __name__ == "__main__":
    unittest.main()
