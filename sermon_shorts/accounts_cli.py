"""`sermon-shorts users ...` and `sermon-shorts audit ...`.

Dispatched from __main__ before the pipeline's imports, so these commands
need only the standard library.

Commands that change something act as a named person: --as LOGIN, or the
SERMON_SHORTS_USER environment variable. The one exception is `users init`,
which creates that first person and is attributed to the shell account
running it.
"""

from __future__ import annotations

import argparse
import csv
import getpass
import json
import os
import sys
from dataclasses import asdict

from .core import audit, users
from .core.db import connect, db_path
from .core.users import AccountError, ROLES

USER_ENV = "SERMON_SHORTS_USER"


def _acting(conn, args) -> users.User:
    login = args.as_user or os.environ.get(USER_ENV)
    if not login:
        sys.exit(f"Say who is acting: --as LOGIN, or set {USER_ENV}.")
    user = users.get(conn, login)
    if user is None:
        sys.exit(f"No active user {login!r} (see: sermon-shorts users list).")
    return user


def _show_token(token: str, token_id: str) -> None:
    print(f"\n  API token ({token_id}): {token}\n")
    print("  This is shown once. Keep it somewhere safe (a password manager);")
    print("  it's what the web app and API will use to know who you are.")


def _users_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sermon-shorts users",
                                description="People and roles: reviewer < approver < owner.")
    # --as is accepted before or after the subcommand.
    acting = argparse.ArgumentParser(add_help=False)
    acting.add_argument("--as", dest="as_user", metavar="LOGIN", default=argparse.SUPPRESS,
                        help=f"who is acting (default: ${USER_ENV})")
    p.add_argument("--as", dest="as_user", metavar="LOGIN", default=None,
                   help=f"who is acting (default: ${USER_ENV})")
    sub = p.add_subparsers(dest="cmd", required=True)
    _add = sub.add_parser
    sub.add_parser = lambda *a, **k: _add(*a, parents=[acting], **k)

    s = sub.add_parser("init", help="create the root owner on an empty store (once)")
    s.add_argument("--login", required=True)
    s.add_argument("--name", required=True)

    s = sub.add_parser("list", help="list people")
    s.add_argument("--all", action="store_true", help="include removed people")
    s.add_argument("--json", action="store_true")

    s = sub.add_parser("invite", help="invite someone at a role (owners only)")
    s.add_argument("role", choices=ROLES)
    s.add_argument("--note", default="", help='who it is for, e.g. "Sarah, comms team"')
    s.add_argument("--days", type=int, default=users.INVITE_DAYS,
                   help=f"how long the code works (default {users.INVITE_DAYS})")

    s = sub.add_parser("invites", help="list open invites")
    s.add_argument("--all", action="store_true", help="include used, withdrawn and expired")
    s.add_argument("--json", action="store_true")

    s = sub.add_parser("revoke-invite", help="withdraw an unused invite")
    s.add_argument("invite_id")

    s = sub.add_parser("accept", help="create your account from an invite code")
    s.add_argument("code")
    s.add_argument("--login", required=True)
    s.add_argument("--name", required=True)

    s = sub.add_parser("role", help="change someone's role (owners only)")
    s.add_argument("login")
    s.add_argument("role", choices=ROLES)

    s = sub.add_parser("remove", help="remove someone and revoke their tokens (owners only)")
    s.add_argument("login")

    s = sub.add_parser("token", help="issue a new API token (yours, or anyone's for owners)")
    s.add_argument("login", nargs="?")

    s = sub.add_parser("tokens", help="list a person's tokens (ids and dates only)")
    s.add_argument("login", nargs="?")

    s = sub.add_parser("revoke-token", help="revoke an API token by id")
    s.add_argument("token_id")
    return p


def users_main(argv: list[str]) -> int:
    args = _users_parser().parse_args(argv)
    conn = connect()
    try:
        return _users(conn, args)
    except AccountError as e:
        sys.exit(f"Not done: {e}")


def _users(conn, args) -> int:
    if args.cmd == "init":
        user, token = users.init_root(conn, args.login, args.name, admin=getpass.getuser())
        print(f"Root owner created: {user.name} ({user.login}) in {db_path()}")
        _show_token(token, users.list_tokens(conn, user.login)[0]["id"])
        print(f"  Next: export {USER_ENV}={user.login}, then invite people with")
        print("  sermon-shorts users invite reviewer|approver|owner --note \"who it's for\"")
        return 0

    if args.cmd == "accept":
        user, token = users.accept_invite(conn, args.code, args.login, args.name)
        print(f"Welcome, {user.name.rstrip('.')}. You're a{'n' if user.role[0] in 'aeiou' else ''} "
              f"{user.role} (login: {user.login}).")
        _show_token(token, users.list_tokens(conn, user.login)[-1]["id"])
        return 0

    if args.cmd == "list":
        people = users.list_users(conn, include_removed=args.all)
        if args.json:
            print(json.dumps([asdict(u) for u in people], indent=2))
        elif not people:
            print("No one yet. Create the root owner: sermon-shorts users init --login ... --name ...")
        for u in [] if args.json else people:
            tags = " (root)" if u.is_root else ""
            tags += f" (removed {u.removed_at})" if u.removed_at else ""
            print(f"  {u.login:<16} {u.role:<9} {u.name}{tags}")
        return 0

    if args.cmd == "invites":
        rows = users.list_invites(conn, pending_only=not args.all)
        if args.json:
            print(json.dumps(rows, indent=2))
            return 0
        if not rows:
            print("No open invites.")
        for r in rows:
            state = ("used" if r["accepted_at"] else "withdrawn" if r["revoked_at"]
                     else "expired" if r["expires_at"] <= audit.now() else "open")
            print(f"  {r['id']}  {r['role']:<9} {state:<9} expires {r['expires_at']}  "
                  f"by {r['created_by_login']}  {r['note']}")
        return 0

    actor = _acting(conn, args)

    if args.cmd == "invite":
        invite_id, code = users.invite(conn, actor, args.role, args.note, args.days)
        print(f"Invite {invite_id}: {args.role}, works for {args.days} days, once.")
        print(f"\n  Code: {code}\n")
        print("  Send it privately. Until the web app exists they redeem it with:")
        print(f"  sermon-shorts users accept {code} --login <login> --name \"<name>\"")
        return 0
    if args.cmd == "revoke-invite":
        users.revoke_invite(conn, actor, args.invite_id)
        print(f"Invite {args.invite_id} withdrawn.")
        return 0
    if args.cmd == "role":
        user = users.set_role(conn, actor, args.login, args.role)
        print(f"{user.login} is now {user.role}.")
        return 0
    if args.cmd == "remove":
        users.remove(conn, actor, args.login)
        print(f"{args.login} removed; their tokens no longer work.")
        return 0
    if args.cmd == "token":
        token_id, token = users.new_token(conn, actor, args.login or actor.login)
        _show_token(token, token_id)
        return 0
    if args.cmd == "tokens":
        login = args.login or actor.login
        if login != actor.login and not actor.can("manage_people"):
            sys.exit("Not done: only owners can see other people's tokens.")
        for t in users.list_tokens(conn, login):
            state = f"revoked {t['revoked_at']}" if t["revoked_at"] else "active"
            print(f"  {t['id']}  created {t['created_at']}  {state}")
        return 0
    if args.cmd == "revoke-token":
        users.revoke_token(conn, actor, args.token_id)
        print(f"Token {args.token_id} revoked.")
        return 0
    return 2


def audit_main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="sermon-shorts audit",
                                description="Read, export or verify the audit log.")
    p.add_argument("--service", help="only this service")
    p.add_argument("--action", help='action or prefix, e.g. "user." or "clip.approved"')
    p.add_argument("--limit", type=int, default=50, help="newest N (default 50; 0 = all)")
    p.add_argument("--csv", action="store_true", help="write CSV to stdout")
    p.add_argument("--json", action="store_true")
    p.add_argument("--verify", action="store_true",
                   help="check the hash chain and report the tail hash")
    args = p.parse_args(argv)
    conn = connect()

    if args.verify:
        v = audit.verify(conn)
        if v.ok:
            print(f"OK: {v.events} events, chain intact. Tail hash: {v.tail}")
            return 0
        print(f"BROKEN at event {v.first_bad}: {v.problem}")
        return 1

    rows = audit.events(conn, service=args.service, action=args.action,
                        limit=args.limit or None)
    if args.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
    elif args.csv:
        out = csv.writer(sys.stdout)
        out.writerow(["seq", "at", "actor", "via", "action", "service", "clip", "data", "hash"])
        for r in rows:
            out.writerow([r["seq"], r["at"], r["actor"], r["via"], r["action"],
                          r["service"] or "", r["clip"] or "",
                          json.dumps(r["data"], ensure_ascii=False), r["hash"]])
    else:
        if not rows:
            print("The audit log is empty.")
        for r in rows:
            detail = ", ".join(f"{k}={v}" for k, v in r["data"].items())
            where = " ".join(x for x in (r["service"], r["clip"]) if x)
            print(f"  {r['seq']:>5}  {r['at']}  {r['actor']:<16} {r['action']:<20} "
                  f"{where + '  ' if where else ''}{detail}")
    return 0
