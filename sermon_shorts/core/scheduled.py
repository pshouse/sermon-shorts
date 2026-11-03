"""Posts that wait for their day: the queue behind `publish --spread`.

A service's clips posted together on Sunday compete with each other: each
platform shows a channel's newest short ahead of the ones before it, so
most of a batch is seen by almost nobody. Spread over the week, one clip a
day, each one gets its own day in front of the same audience.

A scheduled post is one (clip, platform) with a time. Nothing posts itself:
`publish --due` posts the ones whose time has come, through the same gate
and code as a post made by hand (so a clip re-rendered or rejected after it
was scheduled is held, never posted), attributed to the person who
scheduled it. On the sprite .github/workflows/posting.yml runs that on each
posting day; see README "Spreading posts over the week".
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import audit, posts, records, schedule, users
from .audit import Actor
from .db import write
from .users import User

# Posting days are Monday to Saturday: Sunday is the next service's day, and
# its clips are rendered that afternoon.
POSTING_DAYS = (0, 1, 2, 3, 4, 5)  # Python weekdays
# Midday in the church's timezone. People check their phones over lunch, and
# the hour matters far less than not stacking the clips; what fixes it is the
# posting workflow's cron (16:00 and 17:00 UTC), which lands on 12:00 US
# Eastern in both summer and winter time, so a post due at noon goes out at
# noon. Change both together.
POST_HOUR, POST_MINUTE = 12, 0
# A row claimed by a `publish --due` that then died (the sprite paused, a
# crash) is due again after this long; a post itself takes minutes.
CLAIM_TIMEOUT = timedelta(hours=2)

WAITING, POSTING, POSTED, FAILED, HELD, CANCELLED = (
    "waiting", "posting", "posted", "failed", "held", "cancelled")
VIA = "schedule"  # the `via` of posts made from the queue


class ScheduledError(Exception):
    pass


# --- when ---------------------------------------------------------------------

def iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def when_local(iso: str) -> str:
    """A scheduled time for people, in the church's timezone: "Wed 2026-11-05 12:00"."""
    return _parse(iso).astimezone(schedule.church_tz()).strftime("%a %Y-%m-%d %H:%M")


def post_time(day) -> datetime:
    """The posting moment on `day` (a date in the church's timezone), in UTC."""
    local = datetime(day.year, day.month, day.day, POST_HOUR, POST_MINUTE,
                     tzinfo=schedule.church_tz())
    return local.astimezone(timezone.utc)


def days_left(now: datetime | None = None) -> list:
    """The posting days still to come this week (church dates), today
    included while its posting time is still ahead, up to Saturday."""
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(schedule.church_tz()).date()
    out = []
    for i in range(7):
        d = today + timedelta(days=i)
        if i and d.weekday() == 6:
            break  # the coming Sunday ends the week
        if d.weekday() in POSTING_DAYS and post_time(d) > now:
            out.append(d)
    return out


def spread(count: int, now: datetime | None = None) -> list[datetime]:
    """`count` posting times, one clip a day: spread evenly over the posting
    days left this week, and when there are more clips than days, the rest
    continue into the next week, still one a day."""
    now = now or datetime.now(timezone.utc)
    left = days_left(now)
    if count <= len(left):
        days = [left[i * len(left) // count] for i in range(count)]
    else:
        days, d = list(left), (left[-1] if left else now.astimezone(schedule.church_tz()).date())
        while len(days) < count:
            d += timedelta(days=1)
            if d.weekday() in POSTING_DAYS:
                days.append(d)
    return [post_time(d) for d in days]


# --- the queue ------------------------------------------------------------------

def _rows(conn: sqlite3.Connection, sql: str, args=()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, args)]


def waiting(conn: sqlite3.Connection, service_id: str) -> dict[str, dict[str, dict]]:
    """file -> platform -> the row waiting (or being posted right now)."""
    out: dict[str, dict[str, dict]] = {}
    for r in _rows(conn, "SELECT * FROM scheduled_posts WHERE service_id = ? AND status IN (?, ?)",
                   (service_id, WAITING, POSTING)):
        out.setdefault(r["file"], {})[r["platform"]] = r
    return out


def latest(conn: sqlite3.Connection, service_id: str) -> dict[str, dict[str, dict]]:
    """file -> platform -> the newest row, for rows still worth showing: one
    waiting, or one that was held or failed and has not been superseded."""
    out: dict[str, dict[str, dict]] = {}
    for r in _rows(conn, "SELECT * FROM scheduled_posts WHERE service_id = ? ORDER BY id",
                   (service_id,)):
        if r["status"] in (WAITING, POSTING, HELD, FAILED):
            out.setdefault(r["file"], {})[r["platform"]] = r
        else:
            out.get(r["file"], {}).pop(r["platform"], None)
    return {f: p for f, p in out.items() if p}


def upcoming(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    """Every post waiting, across services, soonest first, with the clip's title."""
    return _rows(conn, """
        SELECT s.*, c.title, c.n, v.title AS service_title, v.date AS service_date
        FROM scheduled_posts s
        LEFT JOIN clips c ON c.service_id = s.service_id AND c.file = s.file
        LEFT JOIN services v ON v.id = s.service_id
        WHERE s.status IN (?, ?) ORDER BY s.post_at, s.id LIMIT ?""", (WAITING, POSTING, limit))


def schedule_posts(conn: sqlite3.Connection, user: User, service_id: str, jobs: list[dict],
                   times: dict[int, datetime], via: str = "cli",
                   now: datetime | None = None) -> list[dict]:
    """Queue the planned jobs (from posts.plan, already through the gate) at
    `times[n]` for each clip number. Returns the rows."""
    if not user.can("publish"):
        raise ScheduledError(f"{user.login} is a {user.role}; scheduling posts needs "
                             "approver or above")
    now = now or datetime.now(timezone.utc)
    ids = []
    with write(conn):
        for job in jobs:
            at = times[job["n"]]
            if at <= now:
                raise ScheduledError(f"{when_local(iso(at))} has already passed")
            file, platform = job["entry"]["file"], job["platform"]
            if conn.execute("SELECT 1 FROM scheduled_posts WHERE service_id = ? AND file = ? "
                            "AND platform = ? AND status IN (?, ?)",
                            (service_id, file, platform, WAITING, POSTING)).fetchone():
                raise ScheduledError(f"{job['entry']['title']} is already scheduled on {platform}")
            cur = conn.execute(
                "INSERT INTO scheduled_posts (service_id, file, platform, fingerprint, post_at, "
                "by_login, via, created_at, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (service_id, file, platform, job["fingerprint"], iso(at), user.login, via,
                 audit.now(), WAITING))
            ids.append(cur.lastrowid)
            audit.record(conn, user.actor(via), "post.scheduled", service=service_id, clip=file,
                         platform=platform, post_at=iso(at), title=job["entry"]["title"],
                         fingerprint=job["fingerprint"])
    marks = ",".join("?" * len(ids))
    return _rows(conn, f"SELECT * FROM scheduled_posts WHERE id IN ({marks}) ORDER BY id", ids)


def cancel(conn: sqlite3.Connection, user: User, service_id: str, file: str,
           platform: str | None = None, reason: str = "", via: str = "cli") -> list[dict]:
    """Take a clip out of the queue (one platform, or all of them). Returns
    the rows cancelled; nothing waiting is an error."""
    if not user.can("publish"):
        raise ScheduledError(f"{user.login} is a {user.role}; this needs approver or above")
    sql, args = ("SELECT * FROM scheduled_posts WHERE service_id = ? AND file = ? AND status = ?",
                 [service_id, file, WAITING])
    if platform:
        sql, args = sql + " AND platform = ?", args + [platform]
    rows = _rows(conn, sql, args)
    if not rows:
        raise ScheduledError(f"{file} has no scheduled post"
                             + (f" on {platform}" if platform else "") + " waiting")
    with write(conn):
        for r in rows:
            conn.execute("UPDATE scheduled_posts SET status = ?, outcome = ?, done_at = ? "
                         "WHERE id = ? AND status = ?",
                         (CANCELLED, reason.strip() or None, audit.now(), r["id"], WAITING))
            audit.record(conn, user.actor(via), "post.schedule_cancelled", service=service_id,
                         clip=file, platform=r["platform"], post_at=r["post_at"],
                         reason=reason.strip() or None)
    return rows


# --- posting what is due --------------------------------------------------------

def due(conn: sqlite3.Connection, now: datetime | None = None) -> list[dict]:
    """Rows whose time has come, soonest first, plus claims that went stale."""
    now = now or datetime.now(timezone.utc)
    stale = iso(now - CLAIM_TIMEOUT)
    return _rows(conn, """
        SELECT * FROM scheduled_posts
        WHERE (status = ? AND post_at <= ?) OR (status = ? AND claimed_at <= ?)
        ORDER BY post_at, id""", (WAITING, iso(now), POSTING, stale))


def _finish(conn: sqlite3.Connection, row: dict, status: str, outcome: str | None,
            actor: Actor | None = None) -> None:
    """Close a claimed row. A hold is its own audited event; posted and
    failed rows mirror the post.created / post.failed event post_jobs wrote."""
    with write(conn):
        conn.execute("UPDATE scheduled_posts SET status = ?, outcome = ?, done_at = ? WHERE id = ?",
                     (status, outcome, audit.now(), row["id"]))
        if status == HELD:
            audit.record(conn, actor or Actor(row["by_login"], VIA), "post.schedule_held",
                         service=row["service_id"], clip=row["file"], platform=row["platform"],
                         post_at=row["post_at"], reason=outcome)


def post_due(conn: sqlite3.Connection, now: datetime | None = None, poster=None,
             progress=None) -> list[dict]:
    """Post every row that is due, one at a time. Each goes through
    posts.plan again as the person who scheduled it: a clip that changed,
    was rejected or was posted elsewhere since is held with the reason.
    Returns one {row, status, outcome} per row. `poster` replaces
    publish.post_jobs (tests)."""
    results = []
    for row in due(conn, now):
        # Claim it first: a second --due running at the same time skips it.
        with write(conn):
            claimed = conn.execute(
                "UPDATE scheduled_posts SET status = ?, claimed_at = ? WHERE id = ? "
                "AND status IN (?, ?)", (POSTING, audit.now(), row["id"], WAITING, POSTING)).rowcount
        if not claimed:
            continue
        status, outcome = _post_one(conn, row, poster, progress)
        results.append({"row": row, "status": status, "outcome": outcome})
    return results


def _post_one(conn: sqlite3.Connection, row: dict, poster, progress) -> tuple[str, str | None]:
    sid, file, platform = row["service_id"], row["file"], row["platform"]
    user = users.get(conn, row["by_login"])
    if user is None or not user.can("publish"):
        why = (f"scheduled by {row['by_login']}, who is no longer a member"
               if user is None else f"scheduled by {user.login}, who can no longer publish")
        _finish(conn, row, HELD, why)
        return HELD, why
    actor = user.actor(VIA)
    service = records.get_service(conn, sid)
    folder = Path(service["clips_dir"]) if service and service["clips_dir"] else None
    if folder is None or not (folder / "clips.json").exists():
        why = f"the clips folder of {sid} is gone"
        _finish(conn, row, HELD, why, actor)
        return HELD, why
    n = next((i for i, e in enumerate(records.load_manifest(folder), 1) if e["file"] == file), None)
    if n is None:
        why = f"{file} is no longer in clips.json"
        _finish(conn, row, HELD, why, actor)
        return HELD, why
    jobs, skipped = posts.plan(conn, sid, folder, [n], [platform], ignore_scheduled=True)
    if not jobs:
        why = "; ".join(f"{s['kind'].replace('_', ' ')}: {s['reason']}" for s in skipped) or "not allowed"
        _finish(conn, row, HELD, why, actor)
        return HELD, why
    if poster is None:
        from ..publish import post_jobs  # the Google and Meta clients, only when posting
        poster = post_jobs
    result = poster(conn, actor, sid, folder, jobs, progress=progress)[0]
    if result["error"]:
        _finish(conn, row, FAILED, result["error"], actor)
        return FAILED, result["error"]
    _finish(conn, row, POSTED, result["url"], actor)
    return POSTED, result["url"]


# --- the posting workflow ------------------------------------------------------

def posting_overview(conn: sqlite3.Connection, now: datetime | None = None) -> dict:
    """When scheduled posts go out: the posting time, the workflow that posts
    what is due, its next check, and how many posts wait."""
    now = now or datetime.now(timezone.utc)
    crons = schedule.crons(schedule.POSTING_WORKFLOW)
    checks = schedule.fire_times(now, days=8, cron_lines=crons)
    count = conn.execute("SELECT COUNT(*) FROM scheduled_posts WHERE status IN (?, ?)",
                         (WAITING, POSTING)).fetchone()[0]
    return {"workflow": str(schedule.POSTING_WORKFLOW.relative_to(schedule.PROJECT_DIR)),
            "crons": crons, "time": f"{POST_HOUR:02d}:{POST_MINUTE:02d}",
            "days": [("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")[d] for d in POSTING_DAYS],
            "next_check": iso(checks[0]) if checks else None, "waiting": count}
