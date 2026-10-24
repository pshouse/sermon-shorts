"""People, roles, invites and API tokens.

Roles escalate: reviewer < approver < owner. A shell admin bootstraps the
first owner (the root account) once, on an empty store; from then on only
owners add people, by invite. The root account is always an owner and can't
be demoted or removed by anyone, so the service can never lose its last
owner. Every change here writes an audit event in the same transaction.

Codes and tokens are shown once and stored only as SHA-256 hashes.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import audit
from .audit import Actor
from .db import write

ROLES = ("reviewer", "approver", "owner")
RANK = {role: n for n, role in enumerate(ROLES)}

# What each permission needs, at minimum. The HTTP API and the CLI both
# check against this table, so the two can't drift apart.
PERMISSIONS = {
    "view": "reviewer",          # services, clips, runs, the audit log
    "note": "reviewer",          # leave review notes on a clip (no decision)
    "approve": "approver",       # approve / reject clips
    "publish": "approver",       # post, mark manual posts, retry covers
    "run": "owner",              # start / cancel pipeline runs
    "manage_people": "owner",    # invite, change roles, remove, other people's tokens
}

INVITE_DAYS = 7
_LOGIN = re.compile(r"^[a-z0-9][a-z0-9._-]{1,31}$")


class AccountError(Exception):
    pass


@dataclass(frozen=True)
class User:
    id: int
    login: str
    name: str
    role: str
    is_root: bool
    created_at: str
    removed_at: str | None

    def can(self, permission: str) -> bool:
        return self.removed_at is None and RANK[self.role] >= RANK[PERMISSIONS[permission]]

    def actor(self, via: str) -> Actor:
        return Actor(self.login, via)


def _user(row: sqlite3.Row | None) -> User | None:
    if row is None:
        return None
    return User(row["id"], row["login"], row["name"], row["role"], bool(row["is_root"]),
                row["created_at"], row["removed_at"])


def _sha(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _require(user: User, permission: str) -> None:
    if not user.can(permission):
        need = PERMISSIONS[permission]
        raise AccountError(f"{user.login} is a{'n' if user.role[0] in 'aeiou' else ''} "
                           f"{user.role}; this needs {need} or above")


def _check_role(role: str) -> None:
    if role not in ROLES:
        raise AccountError(f"role must be one of: {', '.join(ROLES)}")


def _check_login(conn: sqlite3.Connection, login: str) -> None:
    if not _LOGIN.match(login):
        raise AccountError("login must be 2-32 characters: lowercase letters, digits, "
                           ". _ - (starting with a letter or digit)")
    if conn.execute("SELECT 1 FROM users WHERE login = ?", (login,)).fetchone():
        raise AccountError(f"login {login!r} is taken")


def _issue_token(conn: sqlite3.Connection, user_id: int) -> tuple[str, str]:
    token = "ssk_" + secrets.token_urlsafe(32)
    token_id = "t_" + secrets.token_hex(4)
    conn.execute("INSERT INTO tokens (id, user_id, sha256, created_at) VALUES (?, ?, ?, ?)",
                 (token_id, user_id, _sha(token), audit.now()))
    return token_id, token


# --- reading ----------------------------------------------------------------

def get(conn: sqlite3.Connection, login: str) -> User | None:
    """An active (not removed) user."""
    return _user(conn.execute(
        "SELECT * FROM users WHERE login = ? AND removed_at IS NULL", (login,)).fetchone())


def list_users(conn: sqlite3.Connection, include_removed: bool = False) -> list[User]:
    sql = "SELECT * FROM users" + ("" if include_removed else " WHERE removed_at IS NULL")
    return [_user(r) for r in conn.execute(sql + " ORDER BY id")]


def list_invites(conn: sqlite3.Connection, pending_only: bool = True) -> list[dict]:
    sql = ("SELECT i.*, u.login AS created_by_login FROM invites i "
           "JOIN users u ON u.id = i.created_by")
    if pending_only:
        sql += " WHERE i.accepted_at IS NULL AND i.revoked_at IS NULL AND i.expires_at > ?"
        rows = conn.execute(sql + " ORDER BY i.created_at", (audit.now(),))
    else:
        rows = conn.execute(sql + " ORDER BY i.created_at")
    return [dict(r) for r in rows]


def list_tokens(conn: sqlite3.Connection, login: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT t.id, t.created_at, t.revoked_at FROM tokens t JOIN users u "
        "ON u.id = t.user_id WHERE u.login = ? ORDER BY t.created_at", (login,))]


def authenticate(conn: sqlite3.Connection, token: str) -> User | None:
    """The active user a bearer token belongs to, or None."""
    return _user(conn.execute(
        "SELECT u.* FROM tokens t JOIN users u ON u.id = t.user_id "
        "WHERE t.sha256 = ? AND t.revoked_at IS NULL AND u.removed_at IS NULL",
        (_sha(token),)).fetchone())


# --- the root owner ---------------------------------------------------------

def init_root(conn: sqlite3.Connection, login: str, name: str, admin: str) -> tuple[User, str]:
    """Create the root owner. Only works on a store that has never had a user,
    so it can't be used later to slip in a second owner."""
    with write(conn):
        if conn.execute("SELECT 1 FROM users LIMIT 1").fetchone():
            raise AccountError("the root owner already exists; owners add people with invites")
        _check_login(conn, login)
        cur = conn.execute(
            "INSERT INTO users (login, name, role, is_root, created_at) "
            "VALUES (?, ?, 'owner', 1, ?)", (login, name.strip() or login, audit.now()))
        token_id, token = _issue_token(conn, cur.lastrowid)
        audit.record(conn, Actor(f"admin:{admin}", "cli"), "user.root_created",
                     login=login, name=name, role="owner", token=token_id)
    return get(conn, login), token


# --- invites ----------------------------------------------------------------

def invite(conn: sqlite3.Connection, by: User, role: str, note: str = "",
           days: int = INVITE_DAYS, via: str = "cli") -> tuple[str, str]:
    """Create a one-time invite for `role`. Returns (invite id, code); the code
    is shown once. Only owners invite, and they may invite at any role up to
    and including owner."""
    _require(by, "manage_people")
    _check_role(role)
    if not 1 <= days <= 30:
        raise AccountError("invites last 1-30 days")
    code = "ssi_" + secrets.token_urlsafe(18)
    invite_id = "i_" + secrets.token_hex(4)
    expires = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(
        timespec="seconds").replace("+00:00", "Z")
    with write(conn):
        conn.execute(
            "INSERT INTO invites (id, code_sha256, role, note, created_at, created_by, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (invite_id, _sha(code), role, note, audit.now(), by.id, expires))
        audit.record(conn, by.actor(via), "invite.created",
                     invite=invite_id, role=role, note=note, expires_at=expires)
    return invite_id, code


def accept_invite(conn: sqlite3.Connection, code: str, login: str, name: str,
                  via: str = "cli") -> tuple[User, str]:
    """Turn an invite code into an account with the invite's role, plus its
    first API token (returned once)."""
    with write(conn):
        row = conn.execute("SELECT * FROM invites WHERE code_sha256 = ?",
                           (_sha(code.strip()),)).fetchone()
        if row is None:
            raise AccountError("that invite code isn't valid")
        if row["accepted_at"]:
            raise AccountError("that invite has already been used")
        if row["revoked_at"]:
            raise AccountError("that invite was withdrawn")
        if row["expires_at"] <= audit.now():
            raise AccountError("that invite has expired; ask an owner for a new one")
        _check_login(conn, login)
        cur = conn.execute(
            "INSERT INTO users (login, name, role, created_at, created_by) VALUES (?, ?, ?, ?, ?)",
            (login, name.strip() or login, row["role"], audit.now(), row["created_by"]))
        conn.execute("UPDATE invites SET accepted_at = ?, accepted_by = ? WHERE id = ?",
                     (audit.now(), cur.lastrowid, row["id"]))
        token_id, token = _issue_token(conn, cur.lastrowid)
        inviter = conn.execute("SELECT login FROM users WHERE id = ?",
                               (row["created_by"],)).fetchone()["login"]
        audit.record(conn, Actor(login, via), "user.joined", login=login, name=name,
                     role=row["role"], invite=row["id"], invited_by=inviter, token=token_id)
    return get(conn, login), token


def revoke_invite(conn: sqlite3.Connection, by: User, invite_id: str, via: str = "cli") -> None:
    _require(by, "manage_people")
    with write(conn):
        cur = conn.execute("UPDATE invites SET revoked_at = ? WHERE id = ? "
                           "AND accepted_at IS NULL AND revoked_at IS NULL",
                           (audit.now(), invite_id))
        if cur.rowcount == 0:
            raise AccountError(f"no open invite {invite_id}")
        audit.record(conn, by.actor(via), "invite.revoked", invite=invite_id)


# --- changing people --------------------------------------------------------

def _target(conn: sqlite3.Connection, login: str) -> User:
    user = get(conn, login)
    if user is None:
        raise AccountError(f"no active user {login!r}")
    return user


def set_role(conn: sqlite3.Connection, by: User, login: str, role: str,
             via: str = "cli") -> User:
    _require(by, "manage_people")
    _check_role(role)
    with write(conn):
        target = _target(conn, login)
        if target.is_root:
            raise AccountError(f"{login} is the root account and always an owner")
        if target.role == role:
            raise AccountError(f"{login} is already {role}")
        conn.execute("UPDATE users SET role = ? WHERE id = ?", (role, target.id))
        audit.record(conn, by.actor(via), "user.role_changed",
                     login=login, before=target.role, after=role)
    return get(conn, login)


def remove(conn: sqlite3.Connection, by: User, login: str, via: str = "cli") -> None:
    """Deactivate an account and revoke its tokens. The row stays, so the
    audit log's names keep pointing at someone."""
    _require(by, "manage_people")
    with write(conn):
        target = _target(conn, login)
        if target.is_root:
            raise AccountError(f"{login} is the root account and can't be removed")
        at = audit.now()
        conn.execute("UPDATE users SET removed_at = ? WHERE id = ?", (at, target.id))
        revoked = conn.execute("UPDATE tokens SET revoked_at = ? WHERE user_id = ? "
                               "AND revoked_at IS NULL", (at, target.id)).rowcount
        audit.record(conn, by.actor(via), "user.removed", login=login,
                     role=target.role, tokens_revoked=revoked)


# --- tokens -----------------------------------------------------------------

def new_token(conn: sqlite3.Connection, by: User, login: str, via: str = "cli") -> tuple[str, str]:
    """Issue another token: for yourself, or (owners) for anyone."""
    if login != by.login:
        _require(by, "manage_people")
    with write(conn):
        target = _target(conn, login)
        token_id, token = _issue_token(conn, target.id)
        audit.record(conn, by.actor(via), "token.created", login=login, token=token_id)
    return token_id, token


def revoke_token(conn: sqlite3.Connection, by: User, token_id: str, via: str = "cli") -> None:
    with write(conn):
        row = conn.execute("SELECT t.*, u.login FROM tokens t JOIN users u "
                           "ON u.id = t.user_id WHERE t.id = ?", (token_id,)).fetchone()
        if row is None or row["revoked_at"]:
            raise AccountError(f"no active token {token_id}")
        if row["login"] != by.login:
            _require(by, "manage_people")
        conn.execute("UPDATE tokens SET revoked_at = ? WHERE id = ?", (audit.now(), token_id))
        audit.record(conn, by.actor(via), "token.revoked", login=row["login"], token=token_id)
