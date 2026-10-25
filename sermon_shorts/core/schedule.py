"""The Sunday schedule: when scheduled runs happen, and owners' pauses and skips.

The times themselves live in .github/workflows/weekly.yml (sprites have no
scheduler, so GitHub Actions wakes the sprite). This module reads them to
show what's coming, and keeps two controls in the database that scheduled
runs obey: a pause, and specific dates to skip (a holiday, a guest
speaker). Runs started by a person (the Studio's Run now, a manual GitHub
run, the CLI) ignore both. Changing the days or times means editing
weekly.yml; the app deliberately holds no token that can rewrite workflows.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import audit
from .db import PROJECT_DIR, write
from .users import User

WORKFLOW = PROJECT_DIR / ".github" / "workflows" / "weekly.yml"
DEFAULT_TZ = "America/New_York"  # the church's; church.json "timezone" overrides
SCHEDULE_STARTER = "schedule"    # runs.started_by for runs the workflow's cron started


class ScheduleError(Exception):
    pass


# --- reading the workflow's cron lines --------------------------------------

def crons() -> list[str]:
    try:
        text = WORKFLOW.read_text(encoding="utf-8")
    except OSError:
        return []
    return re.findall(r'^\s*-\s*cron:\s*["\']([^"\']+)["\']', text, re.M)


def _field(spec: str, lo: int, hi: int) -> set[int]:
    values: set[int] = set()
    for part in spec.split(","):
        rng, _, step = part.partition("/")
        if rng == "*":
            start, end = lo, hi
        elif "-" in rng:
            start, end = (int(x) for x in rng.split("-"))
        else:
            start = end = int(rng)
        values.update(range(start, end + 1, int(step) if step else 1))
    return values


def _matcher(cron: str):
    minute, hour, dom, month, dow = cron.split()
    mins, hours = sorted(_field(minute, 0, 59)), sorted(_field(hour, 0, 23))
    doms, months = _field(dom, 1, 31), _field(month, 1, 12)
    dows = {d % 7 for d in _field(dow, 0, 7)}  # 0 and 7 are both Sunday

    def day_matches(d: date) -> bool:
        if d.month not in months:
            return False
        cron_dow = (d.weekday() + 1) % 7  # Python Monday=0; cron Sunday=0
        if dom != "*" and dow != "*":     # cron's rule: either one may match
            return d.day in doms or cron_dow in dows
        return d.day in doms and cron_dow in dows
    return day_matches, hours, mins


def fire_times(start: datetime, days: int = 60) -> list[datetime]:
    """Every time GitHub is asked to start a scheduled run (UTC), from `start`."""
    out = []
    for cron in crons():
        try:
            day_matches, hours, mins = _matcher(cron)
        except ValueError:
            continue  # a cron this parser doesn't understand; show nothing for it
        for i in range(days):
            d = (start + timedelta(days=i)).date()
            if day_matches(d):
                out += [datetime(d.year, d.month, d.day, h, m, tzinfo=timezone.utc)
                        for h in hours for m in mins]
    return sorted(t for t in set(out) if t >= start)


def church_tz() -> ZoneInfo:
    try:
        name = json.loads((PROJECT_DIR / "church.json").read_text(encoding="utf-8")).get("timezone")
    except (OSError, ValueError):
        name = None
    try:
        return ZoneInfo(name or DEFAULT_TZ)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(DEFAULT_TZ)


# --- pause and skips --------------------------------------------------------

def state(conn: sqlite3.Connection) -> dict:
    row = conn.execute("SELECT * FROM schedule_state WHERE id = 1").fetchone()
    skips = [dict(r) for r in conn.execute("SELECT * FROM schedule_skips ORDER BY date")]
    return {"paused": bool(row and row["paused"]),
            "paused_by": row["changed_by"] if row and row["paused"] else None,
            "paused_at": row["changed_at"] if row and row["paused"] else None,
            "reason": row["reason"] if row and row["paused"] else None,
            "skips": skips}


def _require_owner(user: User) -> None:
    if not user.can("run"):
        raise ScheduleError(f"{user.login} is a {user.role}; changing the schedule needs an owner")


def set_paused(conn: sqlite3.Connection, user: User, paused: bool, reason: str = "",
               via: str = "cli") -> None:
    _require_owner(user)
    if state(conn)["paused"] == paused:
        raise ScheduleError("the schedule is already " + ("paused" if paused else "running"))
    with write(conn):
        conn.execute("INSERT INTO schedule_state (id, paused, reason, changed_by, changed_at) "
                     "VALUES (1, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET paused = excluded.paused, "
                     "reason = excluded.reason, changed_by = excluded.changed_by, "
                     "changed_at = excluded.changed_at",
                     (int(paused), reason.strip(), user.login, audit.now()))
        audit.record(conn, user.actor(via), "schedule.paused" if paused else "schedule.resumed",
                     reason=reason.strip() or None)


def _parse_date(value: str) -> str:
    try:
        return date.fromisoformat(value.strip()).isoformat()
    except ValueError:
        raise ScheduleError("dates are YYYY-MM-DD, e.g. 2026-12-27")


def skip(conn: sqlite3.Connection, user: User, day: str, reason: str = "",
         via: str = "cli") -> str:
    _require_owner(user)
    day = _parse_date(day)
    if day < datetime.now(church_tz()).date().isoformat():
        raise ScheduleError(f"{day} has already passed")
    with write(conn):
        cur = conn.execute("INSERT OR IGNORE INTO schedule_skips (date, reason, by_login, at) "
                           "VALUES (?, ?, ?, ?)", (day, reason.strip(), user.login, audit.now()))
        if not cur.rowcount:
            raise ScheduleError(f"{day} is already skipped")
        audit.record(conn, user.actor(via), "schedule.date_skipped", date=day,
                     reason=reason.strip() or None)
    return day


def unskip(conn: sqlite3.Connection, user: User, day: str, via: str = "cli") -> str:
    _require_owner(user)
    day = _parse_date(day)
    with write(conn):
        if not conn.execute("DELETE FROM schedule_skips WHERE date = ?", (day,)).rowcount:
            raise ScheduleError(f"{day} isn't skipped")
        audit.record(conn, user.actor(via), "schedule.skip_removed", date=day)
    return day


def holds(conn: sqlite3.Connection, when: datetime | None = None) -> str | None:
    """Why a scheduled run starting now shouldn't go ahead, or None."""
    s = state(conn)
    if s["paused"]:
        return f"schedule paused by {s['paused_by']}" + (f" ({s['reason']})" if s["reason"] else "")
    today = (when or datetime.now(timezone.utc)).astimezone(church_tz()).date().isoformat()
    for k in s["skips"]:
        if k["date"] == today:
            return f"{today} skipped by {k['by_login']}" + (f" ({k['reason']})" if k["reason"] else "")
    return None


# --- what's coming ----------------------------------------------------------

def upcoming(conn: sqlite3.Connection, count: int = 6, now: datetime | None = None) -> list[dict]:
    """The next `count` days with scheduled checks, each one window of times:
    {date, first, last, checks, held_by} with times in UTC."""
    now = now or datetime.now(timezone.utc)
    tz, s = church_tz(), state(conn)
    skips = {k["date"]: k for k in s["skips"]}
    days: dict[str, list[datetime]] = {}
    for t in fire_times(now):
        days.setdefault(t.astimezone(tz).date().isoformat(), []).append(t)
    out = []
    for day, times in list(days.items())[:count]:
        held = ("paused" if s["paused"] else "skipped" if day in skips else None)
        out.append({"date": day, "first": _iso(times[0]), "last": _iso(times[-1]),
                    "checks": len(times), "held_by": held,
                    "skip": skips.get(day)})
    return out


def _iso(t: datetime) -> str:
    return t.isoformat(timespec="seconds").replace("+00:00", "Z")


def overview(conn: sqlite3.Connection, count: int = 6) -> dict:
    return {**state(conn), "crons": crons(), "timezone": church_tz().key,
            "workflow": str(WORKFLOW.relative_to(PROJECT_DIR)),
            "upcoming": upcoming(conn, count)}

