"""Decisions about clips: approve, reject, and reviewers' notes.

A decision names the fingerprint of the clip as it is on disk at that moment
(video, cover, title and description; see records.fingerprint). A clip counts
as approved only while its latest decision is an approval of exactly what is
on disk now, so a re-render or an edit after approval sends it back for
review, and nothing can be posted in a form nobody approved.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from . import audit, records
from .db import write
from .users import User

PENDING, APPROVED, REJECTED, STALE = "pending", "approved", "rejected", "stale"


class ReviewError(Exception):
    pass


class StaleError(ReviewError):
    """The clip changed between being watched and being decided on."""


def clip_at(folder: Path, n: int) -> dict:
    """Clip number n (1-based, as listed by publish and services show)."""
    clips = records.load_manifest(folder)
    if not 1 <= n <= len(clips):
        raise ReviewError(f"clip numbers in {folder.name} are 1-{len(clips)}")
    return clips[n - 1]


def latest(conn: sqlite3.Connection, service_id: str, file: str) -> dict | None:
    row = conn.execute("SELECT * FROM decisions WHERE service_id = ? AND file = ? "
                       "ORDER BY id DESC LIMIT 1", (service_id, file)).fetchone()
    return dict(row) if row else None


def status(conn: sqlite3.Connection, service_id: str, file: str,
           fingerprint: str) -> tuple[str, dict | None]:
    """(state, latest decision). A decision about an earlier version of the
    clip doesn't count for this one: an old approval is STALE, and an old
    rejection leaves the new version PENDING."""
    d = latest(conn, service_id, file)
    if d is None:
        return PENDING, None
    if d["fingerprint"] != fingerprint:
        return (STALE if d["decision"] == "approve" else PENDING), d
    return (APPROVED if d["decision"] == "approve" else REJECTED), d


def describe(state: str, d: dict | None) -> str:
    """One line for people, e.g. "approved by pshouse, 2026-10-24T14:14:03Z"."""
    if state == PENDING and d is None:
        return "waiting for review"
    if state == PENDING:
        return "changed since it was rejected; waiting for review"
    if state == STALE:
        return f"changed since {d['by_login']} approved it; needs approving again"
    when = d["backfilled_from"] or d["at"]
    text = f"{state} by {d['by_login']}, {when}"
    return text + (f' ("{d["note"]}")' if d["note"] else "")


def decide(conn: sqlite3.Connection, user: User, folder: Path, n: int, decision: str,
           note: str = "", via: str = "cli", backfilled_from: str | None = None,
           expected_fingerprint: str | None = None) -> dict:
    """Approve or reject clip n as it is on disk now. Returns the decision.

    `expected_fingerprint` is the version the person watched (the web app
    sends it); if the clip has changed since, nothing is recorded."""
    if decision not in ("approve", "reject"):
        raise ValueError(decision)
    if not user.can("approve"):
        raise ReviewError(f"{user.login} is a {user.role}; approving and rejecting "
                          "needs approver or above")
    note = note.strip()
    if decision == "reject" and not note:
        raise ReviewError("say why when rejecting (--note)")
    service_id = records.ensure_service(conn, user.actor(via), folder)
    entry = clip_at(folder, n)
    fp = records.manifest_fingerprint(folder, entry)
    if expected_fingerprint and expected_fingerprint != fp:
        raise StaleError(f"{entry['title']} changed since you loaded it; watch it again")
    at = audit.now()
    with write(conn):
        cur = conn.execute(
            "INSERT INTO decisions (service_id, file, decision, fingerprint, note, by_login, "
            "via, at, backfilled_from) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (service_id, entry["file"], decision, fp, note, user.login, via, at, backfilled_from))
        audit.record(conn, user.actor(via),
                     "clip.approved" if decision == "approve" else "clip.rejected",
                     service=service_id, clip=entry["file"], title=entry["title"],
                     fingerprint=fp, note=note or None, backfilled_from=backfilled_from)
    return dict(conn.execute("SELECT * FROM decisions WHERE id = ?", (cur.lastrowid,)).fetchone())


def add_note(conn: sqlite3.Connection, user: User, folder: Path, n: int, text: str,
             via: str = "cli") -> None:
    """A reviewer's comment on a clip. It informs approvers; it decides nothing."""
    if not user.can("note"):
        raise ReviewError(f"{user.login} can't leave notes")
    text = text.strip()
    if not text:
        raise ReviewError("the note is empty")
    service_id = records.ensure_service(conn, user.actor(via), folder)
    entry = clip_at(folder, n)
    with write(conn):
        conn.execute("INSERT INTO notes (service_id, file, text, by_login, via, at) "
                     "VALUES (?, ?, ?, ?, ?, ?)",
                     (service_id, entry["file"], text, user.login, via, audit.now()))
        audit.record(conn, user.actor(via), "clip.noted", service=service_id,
                     clip=entry["file"], text=text)


def history(conn: sqlite3.Connection, service_id: str, file: str) -> list[dict]:
    """Decisions and notes for one clip, oldest first."""
    rows = [{**dict(r), "kind": "decision"} for r in conn.execute(
        "SELECT * FROM decisions WHERE service_id = ? AND file = ?", (service_id, file))]
    rows += [{**dict(r), "kind": "note"} for r in conn.execute(
        "SELECT * FROM notes WHERE service_id = ? AND file = ?", (service_id, file))]
    return sorted(rows, key=lambda r: r["at"])
