"""The SQLite store: people, their tokens and invites, and the audit log.

It lives in the project root next to .env (gitignored), not in ~/Downloads,
because on the sprite ~/Downloads is published by the `clips` file server
and this file holds token hashes and the full audit history.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
DB_ENV = "SERMON_SHORTS_DB"
DB_NAME = "sermon_shorts.db"

SCHEMA_VERSION = 5

_SCHEMA_V1 = """
CREATE TABLE users (
    id          INTEGER PRIMARY KEY,
    login       TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    role        TEXT NOT NULL CHECK (role IN ('reviewer', 'approver', 'owner')),
    is_root     INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    created_by  INTEGER REFERENCES users(id),
    removed_at  TEXT
);
-- At most one root account, ever.
CREATE UNIQUE INDEX one_root ON users(is_root) WHERE is_root = 1;

CREATE TABLE tokens (
    id          TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    sha256      TEXT NOT NULL UNIQUE,
    created_at  TEXT NOT NULL,
    revoked_at  TEXT
);

CREATE TABLE invites (
    id          TEXT PRIMARY KEY,
    code_sha256 TEXT NOT NULL UNIQUE,
    role        TEXT NOT NULL CHECK (role IN ('reviewer', 'approver', 'owner')),
    note        TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    created_by  INTEGER NOT NULL REFERENCES users(id),
    expires_at  TEXT NOT NULL,
    accepted_at TEXT,
    accepted_by INTEGER REFERENCES users(id),
    revoked_at  TEXT
);

CREATE TABLE audit (
    seq     INTEGER PRIMARY KEY AUTOINCREMENT,
    at      TEXT NOT NULL,
    actor   TEXT NOT NULL,
    via     TEXT NOT NULL,
    action  TEXT NOT NULL,
    service TEXT,
    clip    TEXT,
    data    TEXT NOT NULL,
    prev    TEXT NOT NULL,
    hash    TEXT NOT NULL
);
CREATE INDEX audit_service ON audit(service, seq);
CREATE INDEX audit_action ON audit(action, seq);
-- The log is append-only: the database itself refuses edits and deletes.
-- (The hash chain catches anyone who drops these triggers to get around it.)
CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit
    BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit
    BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
"""

# v2: what the pipeline produces, so the review UI can show it.
# clips.json / published.json stay the files the pipeline itself reads;
# these tables are kept in step with them.
_SCHEMA_V2 = """
CREATE TABLE services (
    id             TEXT PRIMARY KEY,   -- recording file stem, e.g. Live_2026-10-18_7am
    title          TEXT,
    date           TEXT,               -- YYYY-MM-DD
    speaker        TEXT,
    source         TEXT,               -- 'subsplash', 'file' or 'import'
    recording_file TEXT,
    sermon_file    TEXT,
    clips_dir      TEXT,
    sermon_title   TEXT,
    sermon_start   REAL,               -- seconds in the recording, as cut
    sermon_end     REAL,
    sermon_reason  TEXT,
    labeler        TEXT,
    summary        TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE TABLE runs (
    id            TEXT PRIMARY KEY,
    kind          TEXT NOT NULL CHECK (kind IN ('weekly', 'sermon', 'clips', 'rerender')),
    args          TEXT NOT NULL,       -- JSON list, the command line
    status        TEXT NOT NULL CHECK (status IN ('running', 'succeeded', 'failed', 'skipped')),
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    started_by    TEXT NOT NULL,       -- a login, 'schedule', or 'shell:<os user>'
    via           TEXT NOT NULL,       -- 'cli' or 'github'
    service_id    TEXT REFERENCES services(id),
    github_run_id TEXT,
    log_path      TEXT,
    pid           INTEGER,
    host          TEXT,
    outcome       TEXT,                -- one line for the history screen
    error         TEXT
);
-- One pipeline run at a time (a crashed run is marked failed on the next start).
CREATE UNIQUE INDEX one_running ON runs(status) WHERE status = 'running';

CREATE TABLE run_events (
    id     INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id),
    t      TEXT NOT NULL,
    step   TEXT NOT NULL,              -- fetch, transcribe, find_sermon, trim, select, render, done
    state  TEXT NOT NULL,              -- start, done, skip, fail
    data   TEXT NOT NULL               -- JSON
);
CREATE INDEX run_events_run ON run_events(run_id, id);

CREATE TABLE candidates (
    service_id TEXT NOT NULL REFERENCES services(id),
    n          INTEGER NOT NULL,       -- the number the review step printed
    run_id     TEXT REFERENCES runs(id),
    title      TEXT NOT NULL,
    start_s    REAL NOT NULL,
    end_s      REAL NOT NULL,
    pick_score INTEGER,
    verdict    TEXT,                   -- keep, adjust, reject; NULL if not reviewed
    standalone INTEGER,
    note       TEXT,
    outcome    TEXT NOT NULL,          -- rendered, below_bar, rejected, too_short, over_count, unreviewed, out_of_range
    clip_file  TEXT,
    PRIMARY KEY (service_id, n)
);

CREATE TABLE clips (
    service_id  TEXT NOT NULL REFERENCES services(id),
    file        TEXT NOT NULL,
    n           INTEGER NOT NULL,
    title       TEXT NOT NULL,
    description TEXT NOT NULL,
    start_s     REAL NOT NULL,
    end_s       REAL NOT NULL,
    score       INTEGER,
    fingerprint TEXT NOT NULL,
    rendered_at TEXT NOT NULL,
    run_id      TEXT REFERENCES runs(id),
    PRIMARY KEY (service_id, file)
);
"""

# v3: people's decisions about clips, and what was posted where.
_SCHEMA_V3 = """
CREATE TABLE decisions (
    id              INTEGER PRIMARY KEY,
    service_id      TEXT NOT NULL REFERENCES services(id),
    file            TEXT NOT NULL,
    decision        TEXT NOT NULL CHECK (decision IN ('approve', 'reject')),
    fingerprint     TEXT NOT NULL,     -- exactly what was approved (records.fingerprint)
    note            TEXT NOT NULL DEFAULT '',
    by_login        TEXT NOT NULL,
    via             TEXT NOT NULL,
    at              TEXT NOT NULL,
    backfilled_from TEXT               -- when recorded after the fact: when it really happened
);
CREATE INDEX decisions_clip ON decisions(service_id, file, id);

CREATE TABLE notes (
    id         INTEGER PRIMARY KEY,
    service_id TEXT NOT NULL REFERENCES services(id),
    file       TEXT NOT NULL,
    text       TEXT NOT NULL,
    by_login   TEXT NOT NULL,
    via        TEXT NOT NULL,
    at         TEXT NOT NULL
);

-- Decisions and notes are history: a new decision supersedes, nothing is edited.
CREATE TRIGGER decisions_no_update BEFORE UPDATE ON decisions
    BEGIN SELECT RAISE(ABORT, 'decisions are append-only'); END;
CREATE TRIGGER decisions_no_delete BEFORE DELETE ON decisions
    BEGIN SELECT RAISE(ABORT, 'decisions are append-only'); END;
CREATE TRIGGER notes_no_update BEFORE UPDATE ON notes
    BEGIN SELECT RAISE(ABORT, 'notes are append-only'); END;
CREATE TRIGGER notes_no_delete BEFORE DELETE ON notes
    BEGIN SELECT RAISE(ABORT, 'notes are append-only'); END;

CREATE TABLE posts (
    service_id  TEXT NOT NULL REFERENCES services(id),
    file        TEXT NOT NULL,
    platform    TEXT NOT NULL CHECK (platform IN ('youtube', 'instagram', 'facebook')),
    post_id     TEXT,
    url         TEXT,
    at          TEXT NOT NULL,
    by_login    TEXT,                  -- NULL for posts imported from before accounts
    via         TEXT NOT NULL,         -- 'cli', 'web', 'manual' or 'import'
    manual      INTEGER NOT NULL DEFAULT 0,
    cover       TEXT,                  -- 'set', 'failed', or NULL (none attempted / unknown)
    fingerprint TEXT,                  -- the version that was posted
    PRIMARY KEY (service_id, file, platform)  -- the no-double-posting rule
);
"""

# v4: which code produced each run (core/version.py), for tracing a clip back
# to the release and commit that rendered it.
_SCHEMA_V4 = """
ALTER TABLE runs ADD COLUMN code_version TEXT;
ALTER TABLE runs ADD COLUMN code_commit TEXT;
"""

# v5: owners' controls over the Sunday schedule (core/schedule.py).
_SCHEMA_V5 = """
CREATE TABLE schedule_state (
    id         INTEGER PRIMARY KEY CHECK (id = 1),   -- one row
    paused     INTEGER NOT NULL DEFAULT 0,
    reason     TEXT NOT NULL DEFAULT '',
    changed_by TEXT,
    changed_at TEXT
);
CREATE TABLE schedule_skips (
    date     TEXT PRIMARY KEY,                     -- YYYY-MM-DD in the church's timezone
    reason   TEXT NOT NULL DEFAULT '',
    by_login TEXT NOT NULL,
    at       TEXT NOT NULL
);
"""

MIGRATIONS = {1: _SCHEMA_V1, 2: _SCHEMA_V2, 3: _SCHEMA_V3, 4: _SCHEMA_V4, 5: _SCHEMA_V5}


def db_path() -> Path:
    return Path(os.environ.get(DB_ENV) or PROJECT_DIR / DB_NAME).expanduser()


def connect(path: Path | None = None, any_thread: bool = False) -> sqlite3.Connection:
    """Open (creating if needed) the store, with the schema in place.

    `any_thread`: the API's per-request connection may be opened and closed on
    different worker threads (FastAPI runs a dependency's setup and teardown
    separately), which sqlite3 refuses by default. It is still used by one
    request at a time, never shared."""
    path = path or db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # isolation_level=None: we open transactions ourselves (see `write`), so a
    # state change and its audit event always commit together.
    conn = sqlite3.connect(path, isolation_level=None, timeout=30,
                           check_same_thread=not any_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")  # the API reads while the CLI writes
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for target in range(version + 1, SCHEMA_VERSION + 1):
        conn.executescript(f"BEGIN; {MIGRATIONS[target]} PRAGMA user_version = {target}; COMMIT;")
    if version > SCHEMA_VERSION:
        raise RuntimeError(f"{path} was written by a newer sermon-shorts "
                           f"(schema {version}); update this checkout")
    return conn


@contextmanager
def write(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """One write transaction. IMMEDIATE takes the write lock up front, so two
    writers can't both read the audit chain's tail and append after it."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
