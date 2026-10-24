"""The audit log: who did what, when, chained so a changed entry shows.

Each event's hash covers the previous event's hash, so editing or deleting a
row (after first dropping the append-only triggers) breaks every hash after
it, and `verify` names the first bad one. That makes tampering evident,
not impossible: someone with shell access could rebuild the whole chain. The
tail hash printed by `audit verify` is the thing to keep a copy of
elsewhere if that matters.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

GENESIS = "0" * 64


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class Actor:
    """Who an event is attributed to. `login` is a user's login, or a
    descriptive name for actors that aren't accounts (e.g. "admin:sprite"
    for the shell user who bootstraps the root owner)."""
    login: str
    via: str  # "cli", "web", "system"


def _digest(prev: str, at: str, actor: str, via: str, action: str,
            service: str | None, clip: str | None, data: str) -> str:
    body = json.dumps([prev, at, actor, via, action, service, clip, data],
                      ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def record(conn: sqlite3.Connection, actor: Actor, action: str, *,
           service: str | None = None, clip: str | None = None,
           **data) -> int:
    """Append one event. Call inside db.write() so it commits with the change
    it describes (and so the chain's tail can't move underneath us)."""
    row = conn.execute("SELECT hash FROM audit ORDER BY seq DESC LIMIT 1").fetchone()
    prev = row["hash"] if row else GENESIS
    at = now()
    data = {k: v for k, v in data.items() if v is not None}  # absent, not "None"
    payload = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = _digest(prev, at, actor.login, actor.via, action, service, clip, payload)
    cur = conn.execute(
        "INSERT INTO audit (at, actor, via, action, service, clip, data, prev, hash) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (at, actor.login, actor.via, action, service, clip, payload, prev, digest))
    return cur.lastrowid


def events(conn: sqlite3.Connection, *, service: str | None = None,
           action: str | None = None, limit: int | None = None) -> list[dict]:
    """Newest first. `action` matches a prefix, so "user." gives every account event."""
    sql, args = "SELECT * FROM audit WHERE 1=1", []
    if service:
        sql += " AND service = ?"
        args.append(service)
    if action:
        sql += " AND action LIKE ? ESCAPE '\\'"
        args.append(action.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
    sql += " ORDER BY seq DESC"
    if limit:
        sql += " LIMIT ?"
        args.append(limit)
    return [{**dict(r), "data": json.loads(r["data"])} for r in conn.execute(sql, args)]


@dataclass
class Verification:
    ok: bool
    events: int
    tail: str
    first_bad: int | None = None
    problem: str = ""


def verify(conn: sqlite3.Connection) -> Verification:
    prev, count, expected_seq = GENESIS, 0, None
    for r in conn.execute("SELECT * FROM audit ORDER BY seq"):
        count += 1
        if r["prev"] != prev:
            return Verification(False, count, prev, r["seq"],
                                "does not follow the event before it (one was removed or changed)")
        if _digest(r["prev"], r["at"], r["actor"], r["via"], r["action"], r["service"],
                   r["clip"], r["data"]) != r["hash"]:
            return Verification(False, count, prev, r["seq"], "contents were changed")
        if expected_seq is not None and r["seq"] != expected_seq:
            return Verification(False, count, prev, r["seq"],
                                f"sequence jumps from {expected_seq - 1} (events removed)")
        prev, expected_seq = r["hash"], r["seq"] + 1
    # AUTOINCREMENT remembers the highest seq ever issued, so events cut off
    # the end of the log (which the chain alone can't see) still show.
    issued = conn.execute("SELECT seq FROM sqlite_sequence WHERE name = 'audit'").fetchone()
    if issued and issued["seq"] != (expected_seq or 1) - 1:
        return Verification(False, count, prev, (expected_seq or 1),
                            f"events after {(expected_seq or 1) - 1} were removed "
                            f"(the log reached {issued['seq']})")
    return Verification(True, count, prev)
