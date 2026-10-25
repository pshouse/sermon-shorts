"""`sermon-shorts services ...` and `sermon-shorts runs ...`: read what the
pipeline recorded, and import clips folders made before it recorded anything.

Standard library only, dispatched from __main__ before the pipeline imports.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from .core import posts, records, review, runs, users
from .core.audit import Actor
from .core.db import connect

USER_ENV = "SERMON_SHORTS_USER"


def _when(iso: str | None) -> str:
    return iso.replace("T", " ").removesuffix("Z") if iso else "-"


def _took(start: str | None, end: str | None) -> str:
    if not (start and end):
        return "-"
    seconds = int((datetime.fromisoformat(end.replace("Z", "+00:00"))
                   - datetime.fromisoformat(start.replace("Z", "+00:00"))).total_seconds())
    return f"{seconds // 60}m{seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"


def _mmss(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    s = int(round(seconds))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def _resolve_service(conn, ref: str | None) -> str:
    """A service id, a clips folder or a video path; or the newest service."""
    if ref is None:
        newest = records.list_services(conn, limit=1)
        if not newest:
            sys.exit("No services recorded yet.")
        return newest[0]["id"]
    sid = records.service_id_for(Path(ref).expanduser()) if ("/" in ref or ref.endswith(
        ("_clips", ".mp4"))) else ref
    if records.get_service(conn, sid) is None:
        sys.exit(f"No service {sid!r} (see: sermon-shorts services).")
    return sid


def _folder(service: dict) -> Path | None:
    folder = Path(service["clips_dir"]) if service.get("clips_dir") else None
    return folder if folder and (folder / "clips.json").exists() else None


def _needs_you(conn, service: dict) -> list[dict]:
    folder = _folder(service)
    return posts.needs_you(conn, service["id"], folder) if folder else []


def services_main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="sermon-shorts services",
                                description="Services (one per recording) and their clips.")
    sub = p.add_subparsers(dest="cmd")
    s = sub.add_parser("list", help="newest first (the default)")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("show", help="one service: sermon, clips, every candidate")
    s.add_argument("service", nargs="?", help="service id or clips folder (default: newest)")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("import", help="record a clips folder made before the database existed")
    s.add_argument("folder", type=Path)
    s.add_argument("--title", help="the service title (Subsplash's), if known")
    s.add_argument("--speaker")
    s.add_argument("--date", help="YYYY-MM-DD (default: read from the folder name)")
    s.add_argument("--as", dest="as_user", metavar="LOGIN",
                   help=f"who is importing (default: ${USER_ENV}, else your shell user)")
    args = p.parse_args(argv)
    conn = connect()

    if args.cmd in (None, "list"):
        rows = records.list_services(conn)
        if getattr(args, "json", False):
            print(json.dumps(rows, indent=2, ensure_ascii=False))
        elif not rows:
            print("No services recorded yet. Clip runs record them from now on; "
                  "older folders: sermon-shorts services import <clips folder>")
        for r in [] if getattr(args, "json", False) else rows:
            open_items = len(_needs_you(conn, r))
            print(f"  {r['date'] or '----------'}  {r['id']:<28} {r['clip_count']} clip(s), "
                  f"{r['candidate_count']} candidate(s), "
                  f"{open_items or 'nothing'} needing you  last run: "
                  f"{r['last_run_status'] or '-'}  {r['title'] or ''}")
        return 0

    if args.cmd == "show":
        sid = _resolve_service(conn, args.service)
        service = records.get_service(conn, sid)
        clips, cands = records.clips(conn, sid), records.candidates(conn, sid)
        folder = _folder(service)
        posted = posts.posts_for(conn, sid)
        for c in clips:
            fp = (records.manifest_fingerprint(folder, {"file": c["file"], "title": c["title"],
                                                         "description": c["description"]})
                  if folder and (folder / c["file"]).exists() else c["fingerprint"])
            state, d = review.status(conn, sid, c["file"], fp)
            c.update(review_state=state, review=review.describe(state, d),
                     posts=posted.get(c["file"], {}),
                     history=review.history(conn, sid, c["file"]))
        todo = _needs_you(conn, service)
        if args.json:
            print(json.dumps({**service, "clips": clips, "candidates": cands,
                              "needs_you": todo}, indent=2, ensure_ascii=False))
            return 0
        print(f"{service['title'] or sid}  ({sid})")
        print(f"  {service['date'] or 'no date'}" + (f", {service['speaker']}" if service['speaker'] else ""))
        if service["sermon_start"] is not None:
            print(f"  sermon: {_mmss(service['sermon_start'])} to {_mmss(service['sermon_end'])}"
                  f" ({service['labeler']}) {service['sermon_title'] or ''}")
        if service["summary"]:
            print(f"  summary: {service['summary']}")
        if todo:
            print(f"\n  Needs you ({len(todo)}):")
            for t in todo:
                print(f"    - {t['text']}")
        print(f"\n  Clips ({len(clips)}):")
        for c in clips:
            print(f"    {c['n']}. {c['title']}  ({c['end_s'] - c['start_s']:.0f}s, "
                  f"fingerprint {c['fingerprint'][:12]})  {c['file']}")
            print(f"       {c['review']}")
            for platform, p in sorted(c["posts"].items()):
                how = ("by hand" if p["manual"] else f"by {p['by_login']}" if p["by_login"]
                       else "before accounts")
                cover = {"failed": ", cover NOT set", "set": ", cover set"}.get(p["cover"], "")
                print(f"       {platform}: {p['url']} ({how}, {_when(p['at'])}{cover})")
            for h in (h for h in c["history"] if h["kind"] == "note"):
                print(f"       note from {h['by_login']}: {h['text']}")
        print(f"\n  Candidates ({len(cands)}):")
        if not cands:
            print("    none recorded (the run predates candidate recording)")
        for c in cands:
            score = f"stranger {c['standalone']}" if c["standalone"] is not None else "not reviewed"
            print(f"    #{c['n']} {c['outcome']:<12} {score:<14} pick {c['pick_score']}  "
                  f"{c['title']}" + (f"\n        {c['note']}" if c["note"] else ""))
        return 0

    if args.cmd == "import":
        login = args.as_user or os.environ.get(USER_ENV)
        if login and users.get(conn, login) is None:
            sys.exit(f"No active user {login!r}.")
        actor = Actor(login or f"shell:{getpass.getuser()}", "cli")
        folder = args.folder.expanduser().resolve()
        try:
            sid, n = records.import_folder(conn, actor, folder, title=args.title,
                                           speaker=args.speaker, date=args.date)
        except FileNotFoundError as e:
            sys.exit(f"Not imported: {e}")
        print(f"Imported {sid}: {n} clip(s), fingerprints written to clips.json.")
        return 0
    return 2


def runs_main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="sermon-shorts runs", description="Pipeline runs.")
    sub = p.add_subparsers(dest="cmd")
    s = sub.add_parser("list", help="newest first (the default)")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("show", help="one run and its steps")
    s.add_argument("run_id", nargs="?", help="default: the newest run")
    s.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    conn = connect()

    if args.cmd in (None, "list"):
        rows = runs.list_runs(conn, getattr(args, "limit", 20))
        if getattr(args, "json", False):
            print(json.dumps(rows, indent=2))
            return 0
        if not rows:
            print("No runs recorded yet.")
        for r in rows:
            print(f"  {r['id']}  {_when(r['started_at'])}  {r['kind']:<8} {r['status']:<9} "
                  f"{_took(r['started_at'], r['finished_at']):>7}  by {r['started_by']:<16} "
                  f"{r['error'] or r['outcome'] or ''}")
        return 0

    if args.cmd == "show":
        run_id = args.run_id or (runs.list_runs(conn, 1) or [{}])[0].get("id")
        run = runs.get_run(conn, run_id) if run_id else None
        if run is None:
            sys.exit(f"No run {run_id!r}." if run_id else "No runs recorded yet.")
        steps = runs.events(conn, run_id)
        if args.json:
            print(json.dumps({**run, "events": steps}, indent=2, ensure_ascii=False))
            return 0
        print(f"{run['id']}: {run['kind']} {run['status']}, started {_when(run['started_at'])} "
              f"by {run['started_by']} ({run['via']}), took {_took(run['started_at'], run['finished_at'])}")
        if run.get("code_version"):
            print(f"  code: {run['code_version']}" + (f" ({run['code_commit']})" if run.get("code_commit") else ""))
        for key in ("service_id", "outcome", "error", "github_run_id", "log_path"):
            if run[key]:
                print(f"  {key.replace('_', ' ')}: {run[key]}")
        prev = run["started_at"]
        for e in steps:
            detail = ", ".join(f"{k}={v}" for k, v in e["data"].items() if v is not None)
            print(f"  +{_took(prev, e['t']):>7}  {e['step']:<12} {e['state']:<5} {detail}")
            prev = e["t"]
        return 0
    return 2
