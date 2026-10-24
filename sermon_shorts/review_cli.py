"""`sermon-shorts approve | reject | note | mark-posted | retry-cover`.

Each acts as a named person (--as LOGIN or SERMON_SHORTS_USER) and names
clips the way publish does: an optional clips folder or service id, then
clip numbers. Without a folder, the newest *_clips folder is used, the same
one publish would pick.

Standard library only, except retry-cover, which needs the YouTube client.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime

from .core import posts, records, review, users
from .core.db import connect
from .core.posts import PLATFORMS

USER_ENV = "SERMON_SHORTS_USER"
COMMANDS = ("approve", "reject", "note", "mark-posted", "retry-cover")


def _parser(cmd: str) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=f"sermon-shorts {cmd}")
    p.add_argument("--as", dest="as_user", metavar="LOGIN",
                   help=f"who is acting (default: ${USER_ENV})")
    if cmd in ("approve", "reject"):
        p.description = (f"{cmd.capitalize()} clips as they are on disk now. A later "
                         "re-render or edit needs a new decision.")
        p.add_argument("targets", nargs="+", metavar="[SERVICE] N")
        p.add_argument("--note", default="", help="why (required to reject)")
        if cmd == "approve":
            p.add_argument("--backfilled-from", metavar="WHEN",
                           help="record an approval given before it could be recorded, "
                                "e.g. 2026-10-24T14:14:00Z; it is logged as backfilled")
    elif cmd == "note":
        p.description = "Leave a review note on a clip (any role; decides nothing)."
        p.add_argument("targets", nargs="+", metavar="[SERVICE] N")
        p.add_argument("text")
    elif cmd == "mark-posted":
        p.description = "Record a post made by hand in the platform's app."
        p.add_argument("targets", nargs="+", metavar="[SERVICE] N PLATFORM")
        p.add_argument("--url", required=True, help="the post's link")
        p.add_argument("--note", default="")
    else:
        p.description = "Try setting a YouTube cover again after it failed."
        p.add_argument("targets", nargs="+", metavar="[SERVICE] N")
    return p


def _split(conn, targets: list[str]) -> tuple:
    """(folder, the remaining arguments)."""
    ref = targets[0] if targets and not targets[0].isdigit() else None
    rest = targets[1:] if ref else targets
    try:
        folder = records.resolve_folder(conn, ref)
    except FileNotFoundError as e:
        sys.exit(f"Not done: {e}")
    if ref is None:
        print(f"Clips folder: {folder}")
    return folder, rest


def _numbers(args: list[str]) -> list[int]:
    if not args or not all(a.isdigit() for a in args):
        sys.exit("Name clip numbers, e.g. 1 3")
    return [int(a) for a in dict.fromkeys(args)]


def main(cmd: str, argv: list[str]) -> int:
    args = _parser(cmd).parse_args(argv)
    conn = connect()
    login = args.as_user or os.environ.get(USER_ENV)
    if not login:
        sys.exit(f"Say who is acting: --as LOGIN, or set {USER_ENV}.")
    user = users.get(conn, login)
    if user is None:
        sys.exit(f"No active user {login!r} (see: sermon-shorts users list).")
    folder, rest = _split(conn, args.targets)
    try:
        return _run(cmd, args, conn, user, folder, rest)
    except (review.ReviewError, users.AccountError) as e:
        sys.exit(f"Not done: {e}")


def _run(cmd, args, conn, user, folder, rest) -> int:
    if cmd in ("approve", "reject"):
        for n in _numbers(rest):
            d = review.decide(conn, user, folder, n, cmd, note=args.note,
                              backfilled_from=getattr(args, "backfilled_from", None))
            title = review.clip_at(folder, n)["title"]
            print(f"  {n}. {title}: {'approved' if cmd == 'approve' else 'rejected'} "
                  f"(fingerprint {d['fingerprint'][:12]})")
        return 0

    if cmd == "note":
        for n in _numbers(rest):
            review.add_note(conn, user, folder, n, args.text)
            print(f"  note added to {n}. {review.clip_at(folder, n)['title']}")
        return 0

    if not user.can("publish"):
        sys.exit(f"Not done: {user.login} is a {user.role}; this needs approver or above.")
    service_id = records.ensure_service(conn, user.actor("cli"), folder)

    if cmd == "mark-posted":
        if len(rest) != 2 or not rest[0].isdigit() or rest[1] not in PLATFORMS:
            sys.exit(f"Usage: mark-posted [SERVICE] N {{{','.join(PLATFORMS)}}} --url URL")
        n, platform = int(rest[0]), rest[1]
        entry = review.clip_at(folder, n)
        ledger = posts.load_ledger(folder)
        if platform in ledger.get(entry["file"], {}) or platform in posts.posts_for(
                conn, service_id).get(entry["file"], {}):
            sys.exit(f"Not done: {entry['title']} is already recorded on {platform}.")
        fp = records.manifest_fingerprint(folder, entry)
        state, _ = review.status(conn, service_id, entry["file"], fp)
        if state != review.APPROVED:
            # It's already public; the record has to say so either way.
            print(f"  warning: this clip is {state}, not approved; recording the post anyway")
        posts.write_ledger_entry(folder, ledger, entry["file"], platform, {
            "id": None, "url": args.url, "manual": True, "by": user.login,
            "at": datetime.now().isoformat(timespec="seconds")})  # as publish writes it
        posts.record_post(conn, user.actor("cli"), service_id, entry["file"], platform, None,
                          args.url, fp, manual=True, note=args.note)
        print(f"  recorded: {entry['title']} on {platform} (by hand) — {args.url}")
        return 0

    # retry-cover
    from .publish import PublishError, set_youtube_cover
    for n in _numbers(rest):
        entry = review.clip_at(folder, n)
        post = posts.posts_for(conn, service_id).get(entry["file"], {}).get("youtube")
        ledger_entry = posts.load_ledger(folder).get(entry["file"], {}).get("youtube")
        video_id = (post or {}).get("post_id") or (ledger_entry or {}).get("id")
        if not video_id:
            sys.exit(f"Not done: {entry['title']} isn't on YouTube.")
        jpg = (folder / entry["file"]).with_suffix(".jpg")
        if not jpg.exists():
            sys.exit(f"Not done: {jpg.name} is missing.")
        try:
            set_youtube_cover(video_id, jpg)
        except PublishError as e:
            posts.record_cover(conn, user.actor("cli"), service_id, entry["file"], "youtube",
                               False, str(e))
            print(f"  {entry['title']}: cover still not set ({e})")
            return 1
        posts.record_cover(conn, user.actor("cli"), service_id, entry["file"], "youtube", True)
        print(f"  {entry['title']}: cover set")
    return 0
