"""Pipeline runs: who started each, its steps, and how it ended.

`cli.main()` opens a run once it has parsed a render command and closes it
on the way out, however it exits. `--weekly` recurses into main() for the
sermon trim and the clips; those nested calls join the run already open, so
one Sunday job is one run.

Only one run may be open at a time (a partial unique index). A run whose
process died without closing it (power loss, kill -9) is marked failed the
next time a run starts, so a crash can't lock the pipeline forever.
"""

from __future__ import annotations

import getpass
import json
import os
import secrets
import socket
import sqlite3
from datetime import datetime, timedelta, timezone

from . import audit, users
from .audit import Actor
from .db import connect, write

USER_ENV = "SERMON_SHORTS_USER"            # a login: the person starting the run
STARTED_BY_ENV = "SERMON_SHORTS_STARTED_BY"  # set by the workflow: "schedule" or "github:<login>"
GITHUB_RUN_ENV = "SERMON_SHORTS_GITHUB_RUN"  # the Actions run id, to join the two histories
LOG_ENV = "SERMON_SHORTS_LOG"              # the log file sprite_weekly.sh tees into
RUN_ID_ENV = "SERMON_SHORTS_RUN_ID"        # set by the API, so it knows the id of a run it starts
# A run on another machine can't be checked for a live process; after this
# long it is assumed dead. The longest real run so far took well under an hour.
STALE_AFTER = timedelta(hours=6)


class RunBusy(Exception):
    pass


class Run:
    def __init__(self, conn: sqlite3.Connection, run_id: str, kind: str, actor: Actor):
        self.conn, self.id, self.kind, self.actor = conn, run_id, kind, actor
        self.service_id: str | None = None
        self.outcome = ""           # one line for the history screen
        self.skip_reason: str | None = None

    def step(self, step: str, state: str = "done", **data) -> None:
        """A progress marker. Durations come from the gaps between them."""
        with write(self.conn):
            self.conn.execute(
                "INSERT INTO run_events (run_id, t, step, state, data) VALUES (?, ?, ?, ?, ?)",
                (self.id, audit.now(), step, state,
                 json.dumps(data, ensure_ascii=False, default=str)))

    def attach(self, service_id: str) -> None:
        self.service_id = service_id
        with write(self.conn):
            self.conn.execute("UPDATE runs SET service_id = ? WHERE id = ?",
                              (service_id, self.id))

    def skip(self, reason: str) -> None:
        self.skip_reason = reason


_current: Run | None = None


def current() -> Run | None:
    return _current


def _starter(conn: sqlite3.Connection) -> Actor:
    via = "github" if os.environ.get(GITHUB_RUN_ENV) else "cli"
    login = os.environ.get(USER_ENV)
    if login and users.get(conn, login):
        return Actor(login, via)
    if os.environ.get(STARTED_BY_ENV):
        return Actor(os.environ[STARTED_BY_ENV], via)
    return Actor(f"shell:{getpass.getuser()}", via)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _reap(conn: sqlite3.Connection) -> None:
    """Close runs whose process is gone."""
    host = socket.gethostname()
    cutoff = (datetime.now(timezone.utc) - STALE_AFTER).isoformat(
        timespec="seconds").replace("+00:00", "Z")
    for r in conn.execute("SELECT * FROM runs WHERE status = 'running'").fetchall():
        same_host = r["host"] == host
        if (same_host and r["pid"] and _alive(r["pid"])) or (not same_host and r["started_at"] > cutoff):
            continue
        why = "process ended without closing the run" if same_host else "no word for 6 hours"
        with write(conn):
            conn.execute("UPDATE runs SET status = 'failed', finished_at = ?, error = ? "
                         "WHERE id = ? AND status = 'running'", (audit.now(), why, r["id"]))
            audit.record(conn, Actor("system", "system"), "run.failed",
                         service=r["service_id"], run=r["id"], kind=r["kind"], error=why)


def new_id() -> str:
    return "r_" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "_" + secrets.token_hex(2)


def running(conn: sqlite3.Connection) -> dict | None:
    """The open run, after closing any whose process is gone."""
    _reap(conn)
    row = conn.execute("SELECT * FROM runs WHERE status = 'running'").fetchone()
    return dict(row) if row else None


def begin(kind: str, argv: list[str]) -> Run:
    """Open a run. Raises RunBusy if another is open and its process is alive."""
    global _current
    conn = connect()
    _reap(conn)
    run_id = os.environ.pop(RUN_ID_ENV, None) or new_id()
    actor = _starter(conn)
    try:
        with write(conn):
            conn.execute(
                "INSERT INTO runs (id, kind, args, status, started_at, started_by, via, "
                "github_run_id, log_path, pid, host) VALUES (?, ?, ?, 'running', ?, ?, ?, ?, ?, ?, ?)",
                (run_id, kind, json.dumps(argv), audit.now(), actor.login, actor.via,
                 os.environ.get(GITHUB_RUN_ENV), os.environ.get(LOG_ENV), os.getpid(),
                 socket.gethostname()))
    except sqlite3.IntegrityError:
        other = conn.execute("SELECT * FROM runs WHERE status = 'running'").fetchone()
        conn.close()
        raise RunBusy(f"another run is in progress: {other['id']} ({other['kind']}, "
                      f"started {other['started_at']} by {other['started_by']}, "
                      f"pid {other['pid']} on {other['host']})")
    _current = Run(conn, run_id, kind, actor)
    return _current


def end(rc: int | None = None, error: str | None = None) -> None:
    """Close the open run: skipped, failed (an error or non-zero exit) or succeeded."""
    global _current
    run, _current = _current, None
    if run is None:
        return
    if run.skip_reason and not error and not rc:
        status = "skipped"
    elif error or rc:
        status = "failed"
    else:
        status = "succeeded"
    error = error or (f"exit status {rc}" if rc else None)
    with write(run.conn):
        run.conn.execute(
            "UPDATE runs SET status = ?, finished_at = ?, outcome = ?, error = ? WHERE id = ?",
            (status, audit.now(), run.skip_reason or run.outcome, error, run.id))
        # A schedule that finds nothing new every half hour isn't worth an
        # audit entry each time; the runs table still lists them.
        if status != "skipped":
            audit.record(run.conn, run.actor,
                         "run.finished" if status == "succeeded" else "run.failed",
                         service=run.service_id, run=run.id, kind=run.kind,
                         outcome=run.outcome, error=error)
    run.conn.close()


# --- reading ----------------------------------------------------------------

def list_runs(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,))]


def get_run(conn: sqlite3.Connection, run_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    return dict(row) if row else None


def events(conn: sqlite3.Connection, run_id: str) -> list[dict]:
    return [{**dict(r), "data": json.loads(r["data"])} for r in conn.execute(
        "SELECT * FROM run_events WHERE run_id = ? ORDER BY id", (run_id,))]
