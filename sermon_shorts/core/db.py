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

SCHEMA_VERSION = 1

_SCHEMA = """
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


def db_path() -> Path:
    return Path(os.environ.get(DB_ENV) or PROJECT_DIR / DB_NAME).expanduser()


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Open (creating if needed) the store, with the schema in place."""
    path = path or db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # isolation_level=None: we open transactions ourselves (see `write`), so a
    # state change and its audit event always commit together.
    conn = sqlite3.connect(path, isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")  # the API reads while the CLI writes
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version == 0:
        conn.executescript(f"BEGIN; {_SCHEMA} PRAGMA user_version = {SCHEMA_VERSION}; COMMIT;")
    elif version > SCHEMA_VERSION:
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
