"""What was posted where, the publish plan (the approval gate), and the list
of things waiting on a person.

published.json in each clips folder stays the ledger the publish command has
always checked; the posts table mirrors it, adds who posted and whether the
cover took, and records posts made by hand in the apps.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from . import audit, records, review
from .audit import Actor
from .db import write

PLATFORMS = ("youtube", "instagram", "facebook")
LEDGER = "published.json"
# Instagram's upload host rejects longer Reels with an opaque ProcessingFailedError,
# whatever the file size. Measured 2026-10-07: 65 s accepted, 70 s rejected.
IG_MAX_SECONDS = 65


# --- the ledger file --------------------------------------------------------

def load_ledger(folder: Path) -> dict:
    path = folder / LEDGER
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def write_ledger_entry(folder: Path, ledger: dict, file: str, platform: str, entry: dict) -> None:
    # Written after every single post, so a failure halfway through a batch
    # still leaves an accurate record of what actually went out.
    ledger.setdefault(file, {})[platform] = entry
    (folder / LEDGER).write_text(json.dumps(ledger, indent=2), encoding="utf-8")


# --- reading ----------------------------------------------------------------

def posts_for(conn: sqlite3.Connection, service_id: str) -> dict[str, dict[str, dict]]:
    """file -> platform -> post row."""
    out: dict[str, dict[str, dict]] = {}
    for r in conn.execute("SELECT * FROM posts WHERE service_id = ?", (service_id,)):
        out.setdefault(r["file"], {})[r["platform"]] = dict(r)
    return out


# --- the publish plan -------------------------------------------------------

def plan(conn: sqlite3.Connection, service_id: str, folder: Path, chosen: list[int],
         platforms: list[str], assume_approved: set[int] = frozenset()
         ) -> tuple[list[dict], list[dict]]:
    """Which (clip, platform) posts would go out now, and which are skipped and why.

    The gate: a clip is posted only if its latest decision approves its
    current fingerprint. Then, per platform: never twice (ledger or posts
    table), and Instagram's API only up to IG_MAX_SECONDS.
    Each job/skip: {n, entry, platform, fingerprint, reason?, kind?}.
    `assume_approved` previews a plan for clips about to be approved (publish
    --approve shows it before confirming); the real posting re-plans without it.
    """
    clips = records.load_manifest(folder)
    ledger, posted = load_ledger(folder), posts_for(conn, service_id)
    jobs, skipped = [], []
    for n in chosen:
        entry = clips[n - 1]
        fp = records.manifest_fingerprint(folder, entry)
        state, d = review.status(conn, service_id, entry["file"], fp)
        base = {"n": n, "entry": entry, "fingerprint": fp}
        if state != review.APPROVED and n not in assume_approved:
            skipped.append({**base, "platform": None, "kind": "not_approved",
                            "reason": review.describe(state, d)})
            continue
        for platform in platforms:
            done = (ledger.get(entry["file"], {}).get(platform)
                    or posted.get(entry["file"], {}).get(platform))
            seconds = entry["end"] - entry["start"]
            if done:
                skipped.append({**base, "platform": platform, "kind": "already",
                                "reason": done.get("url") or "posted"})
            elif platform == "instagram" and seconds > IG_MAX_SECONDS:
                skipped.append({**base, "platform": platform, "kind": "manual_required",
                                "reason": f"{seconds:.0f}s, limit {IG_MAX_SECONDS}s"})
            else:
                jobs.append({**base, "platform": platform})
    return jobs, skipped


def plan_id(jobs: list[dict], skipped: list[dict]) -> str:
    """An id for exactly this plan: the clips, platforms and versions that would
    go out and everything skipped. Shown with the preview and required to post,
    so if anything changes in between (an approval, a re-render, a post made
    elsewhere) the id no longer matches and nothing is posted."""
    key = json.dumps({
        "jobs": sorted((j["n"], j["entry"]["file"], j["platform"], j["fingerprint"]) for j in jobs),
        "skipped": sorted((s["n"], s["platform"] or "", s["kind"], s["reason"]) for s in skipped),
    }, separators=(",", ":"))
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:20]


# --- recording --------------------------------------------------------------

def record_post(conn: sqlite3.Connection, actor: Actor, service_id: str, file: str,
                platform: str, post_id: str | None, url: str, fingerprint: str | None,
                cover: str | None = None, manual: bool = False, note: str = "") -> None:
    at = audit.now()
    with write(conn):
        conn.execute(
            "INSERT INTO posts (service_id, file, platform, post_id, url, at, by_login, via, "
            "manual, cover, fingerprint) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (service_id, file, platform, post_id, url, at, actor.login,
             "manual" if manual else actor.via, int(manual), cover, fingerprint))
        audit.record(conn, actor, "post.marked_manual" if manual else "post.created",
                     service=service_id, clip=file, platform=platform, url=url,
                     post_id=post_id, fingerprint=fingerprint, cover=cover, note=note or None)
        if cover == "failed":
            audit.record(conn, actor, "cover.failed", service=service_id, clip=file,
                         platform=platform)


def record_failure(conn: sqlite3.Connection, actor: Actor, service_id: str, file: str,
                   platform: str, error: str) -> None:
    with write(conn):
        audit.record(conn, actor, "post.failed", service=service_id, clip=file,
                     platform=platform, error=error)


def record_cover(conn: sqlite3.Connection, actor: Actor, service_id: str, file: str,
                 platform: str, ok: bool, error: str | None = None) -> None:
    with write(conn):
        conn.execute("UPDATE posts SET cover = ? WHERE service_id = ? AND file = ? "
                     "AND platform = ?", ("set" if ok else "failed", service_id, file, platform))
        audit.record(conn, actor, "cover.set" if ok else "cover.failed", service=service_id,
                     clip=file, platform=platform, error=error)


def import_ledger(conn: sqlite3.Connection, actor: Actor, service_id: str, folder: Path) -> int:
    """Copy published.json entries the posts table doesn't have yet (posts made
    before the database existed). Who posted them was never recorded."""
    added = 0
    with write(conn):
        for file, platforms in load_ledger(folder).items():
            for platform, e in platforms.items():
                cur = conn.execute(
                    "INSERT OR IGNORE INTO posts (service_id, file, platform, post_id, url, at, "
                    "by_login, via, manual) VALUES (?, ?, ?, ?, ?, ?, ?, 'import', ?)",
                    (service_id, file, platform, e.get("id"), e.get("url"),
                     e.get("at") or audit.now(), e.get("by"), int(bool(e.get("manual")))))
                if cur.rowcount:
                    added += 1
                    audit.record(conn, actor, "post.imported", service=service_id, clip=file,
                                 platform=platform, url=e.get("url"), posted_at=e.get("at"))
    return added


# --- what's waiting on a person ---------------------------------------------

def needs_you(conn: sqlite3.Connection, service_id: str, folder: Path) -> list[dict]:
    """Open items for one service: clips to review, approved clips not posted,
    Instagram uploads owed by hand, covers that didn't take, a failed run.
    Each item: {kind, n, file, title, text}."""
    items = []
    posted = posts_for(conn, service_id)
    for n, entry in enumerate(records.load_manifest(folder), 1):
        file, title = entry["file"], entry["title"]
        fp = records.manifest_fingerprint(folder, entry)
        state, d = review.status(conn, service_id, file, fp)
        mine = posted.get(file, {})
        base = {"n": n, "file": file, "title": title}
        if state in (review.PENDING, review.STALE):
            items.append({**base, "kind": "review", "text": f'Review "{title}": '
                          + review.describe(state, d)})
            continue
        if state == review.REJECTED:
            continue
        if not mine:
            items.append({**base, "kind": "publish", "text": f'Post "{title}": approved, '
                          "not posted anywhere yet"})
            continue
        seconds = entry["end"] - entry["start"]
        if "instagram" not in mine and seconds > IG_MAX_SECONDS:
            items.append({**base, "kind": "manual", "text": f'Post "{title}" to Instagram '
                          f"by hand ({seconds:.0f}s is over the API's {IG_MAX_SECONDS}s), "
                          "then record it with mark-posted"})
        for platform, p in mine.items():
            if p["cover"] == "failed":
                items.append({**base, "kind": "cover", "text": f'Set the {platform} cover for '
                              f'"{title}" (the upload was rejected); try retry-cover'})
    last = conn.execute("SELECT * FROM runs WHERE service_id = ? ORDER BY started_at DESC "
                        "LIMIT 1", (service_id,)).fetchone()
    if last and last["status"] == "failed":
        items.append({"kind": "run", "n": None, "file": None, "title": None,
                      "text": f"The last run ({last['id']}) failed: {last['error']}"})
    return items
