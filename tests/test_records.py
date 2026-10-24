"""Services, clips, fingerprints, folder import and run bookkeeping.
Run: python -m unittest discover tests (standard library only)."""

import json
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sermon_shorts.core import audit, records, runs
from sermon_shorts.core.audit import Actor
from sermon_shorts.core.db import connect, write

ME = Actor("tester", "cli")


class RecordsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.conn = connect(self.dir / "t.db")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def make_folder(self, name="Live_2026-10-18_7am_sermon_clips", n=2):
        folder = self.dir / name
        folder.mkdir()
        entries = []
        for i in range(1, n + 1):
            (folder / f"0{i}_clip.mp4").write_bytes(b"video %d" % i)
            (folder / f"0{i}_clip.jpg").write_bytes(b"cover %d" % i)
            entries.append({"file": f"0{i}_clip.mp4", "title": f"Clip {i}", "start": 10.0 * i,
                            "end": 10.0 * i + 60, "score": 80, "description": f"About {i}"})
        (folder / "clips.json").write_text(json.dumps({"summary": "S", "clips": entries}))
        return folder

    def test_service_ids_tie_recording_sermon_and_folder_together(self):
        for name in ("Live_2026-10-18_7am.mp4", "Live_2026-10-18_7am_sermon.mp4",
                     "Live_2026-10-18_7am_sermon_clips", "Live_2026-10-18_7am_clips"):
            self.assertEqual(records.service_id_for(self.dir / name), "Live_2026-10-18_7am")
        self.assertEqual(records.date_in("Live_2026-10-18_7am"), "2026-10-18")

    def test_fingerprint_covers_video_cover_and_text(self):
        folder = self.make_folder(n=1)
        mp4, jpg = folder / "01_clip.mp4", folder / "01_clip.jpg"
        base = records.fingerprint(mp4, jpg, "T", "D")
        self.assertEqual(base, records.fingerprint(mp4, jpg, "T", "D"))
        self.assertNotEqual(base, records.fingerprint(mp4, jpg, "T2", "D"))
        self.assertNotEqual(base, records.fingerprint(mp4, jpg, "T", "D2"))
        jpg.write_bytes(b"new cover")
        self.assertNotEqual(base, records.fingerprint(mp4, jpg, "T", "D"))

    def test_upsert_only_touches_given_fields(self):
        with write(self.conn):
            records.upsert_service(self.conn, "s1", title="A", speaker="Rev. X")
            records.upsert_service(self.conn, "s1", summary="Sum")
        s = records.get_service(self.conn, "s1")
        self.assertEqual((s["title"], s["speaker"], s["summary"]), ("A", "Rev. X", "Sum"))

    def test_import_records_clips_and_writes_fingerprints_back(self):
        folder = self.make_folder()
        sid, n = records.import_folder(self.conn, ME, folder, title="T")
        self.assertEqual((sid, n), ("Live_2026-10-18_7am", 2))
        service = records.get_service(self.conn, sid)
        self.assertEqual((service["date"], service["source"], service["title"]),
                         ("2026-10-18", "import", "T"))
        rows = records.clips(self.conn, sid)
        saved = json.loads((folder / "clips.json").read_text())["clips"]
        self.assertEqual([r["fingerprint"] for r in rows], [c["fingerprint"] for c in saved])
        actions = [e["action"] for e in audit.events(self.conn)]
        self.assertEqual(actions, ["service.imported", "clip.imported", "clip.imported"])

    def test_record_clips_mirrors_the_manifest(self):
        folder = self.make_folder(n=2)
        records.import_folder(self.conn, ME, folder)
        entries = json.loads((folder / "clips.json").read_text())["clips"][:1]
        entries[0]["fingerprint"] = "new"
        with write(self.conn):
            records.record_clips(self.conn, ME, "Live_2026-10-18_7am", folder, entries,
                                 {"01_clip.mp4"}, None)
        rows = records.clips(self.conn, "Live_2026-10-18_7am")
        self.assertEqual([(r["file"], r["fingerprint"]) for r in rows], [("01_clip.mp4", "new")])

    def test_candidates_replace_the_previous_selection(self):
        with write(self.conn):
            records.upsert_service(self.conn, "s1")
            records.replace_candidates(self.conn, "s1", None, [
                {"n": 1, "title": "A", "start": 0, "end": 40, "outcome": "rendered"},
                {"n": 2, "title": "B", "start": 50, "end": 90, "outcome": "below_bar",
                 "standalone": 60, "note": "weak"}])
            records.replace_candidates(self.conn, "s1", None, [
                {"n": 1, "title": "C", "start": 0, "end": 40, "outcome": "rendered"}])
        self.assertEqual([c["title"] for c in records.candidates(self.conn, "s1")], ["C"])

    def test_none_values_are_left_out_of_audit_data(self):
        with write(self.conn):
            audit.record(self.conn, ME, "x.test", kept=1, dropped=None)
        self.assertEqual(audit.events(self.conn)[0]["data"], {"kept": 1})


class RunsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {
            "SERMON_SHORTS_DB": str(Path(self.tmp.name) / "t.db")}, clear=False)
        self.env.start()
        for key in (runs.USER_ENV, runs.STARTED_BY_ENV, runs.GITHUB_RUN_ENV, runs.LOG_ENV):
            os.environ.pop(key, None)
        self.conn = connect()

    def tearDown(self):
        runs.end()  # never leak an open run into the next test
        self.conn.close()
        self.env.stop()
        self.tmp.cleanup()

    def status(self, run_id):
        return runs.get_run(self.conn, run_id)["status"]

    def test_success_failure_and_skip(self):
        r = runs.begin("clips", ["x.mp4"])
        r.step("transcribe", segments=3)
        r.outcome = "2 clip(s) rendered"
        runs.end(0)
        self.assertEqual(self.status(r.id), "succeeded")
        self.assertEqual([e["step"] for e in runs.events(self.conn, r.id)], ["transcribe"])

        r = runs.begin("clips", [])
        runs.end(error="boom")
        self.assertEqual(runs.get_run(self.conn, r.id)["error"], "boom")

        r = runs.begin("weekly", [])
        r.skip("already processed")
        runs.end(0)
        self.assertEqual(self.status(r.id), "skipped")
        # Skipped runs stay out of the audit log; the other two are in it.
        self.assertEqual([e["action"] for e in audit.events(self.conn)],
                         ["run.failed", "run.finished"])

    def test_only_one_run_at_a_time(self):
        runs.begin("clips", [])
        saved, runs._current = runs._current, None
        try:
            with self.assertRaises(runs.RunBusy):
                runs.begin("clips", [])
        finally:
            runs._current = saved

    def test_a_dead_run_is_reaped(self):
        self.conn.execute(
            "INSERT INTO runs (id, kind, args, status, started_at, started_by, via, pid, host) "
            "VALUES ('r_dead', 'clips', '[]', 'running', ?, 'x', 'cli', 999999999, ?)",
            (audit.now(), socket.gethostname()))
        r = runs.begin("clips", [])
        self.assertEqual(self.status("r_dead"), "failed")
        self.assertEqual(self.status(r.id), "running")

    def test_starter_comes_from_the_environment(self):
        os.environ[runs.STARTED_BY_ENV] = "schedule"
        os.environ[runs.GITHUB_RUN_ENV] = "39165517310"
        r = runs.begin("weekly", [])
        row = runs.get_run(self.conn, r.id)
        self.assertEqual((row["started_by"], row["via"], row["github_run_id"]),
                         ("schedule", "github", "39165517310"))


if __name__ == "__main__":
    unittest.main()
