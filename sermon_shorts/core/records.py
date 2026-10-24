"""What the pipeline produced: services, their clip candidates, rendered clips.

A service is one recording, identified by the recording's file stem
(`Live_2026-10-18_7am`). The trimmed `<stem>_sermon.mp4` and its
`<stem>_sermon_clips/` folder belong to the same service.

The pipeline keeps writing clips.json, which --from-manifest and publish
read; the rows here mirror it, plus what clips.json never held (the
Subsplash metadata, every candidate and Claude's review of it, fingerprints).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from pathlib import Path

from . import audit
from .audit import Actor

_SERVICE_FIELDS = ("title", "date", "speaker", "source", "recording_file", "sermon_file",
                   "clips_dir", "sermon_title", "sermon_start", "sermon_end",
                   "sermon_reason", "labeler", "summary")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def service_id_for(path: Path) -> str:
    """The service a recording, sermon file or clips folder belongs to."""
    name = path.name if path.is_dir() or path.name.endswith("_clips") else path.stem
    for suffix in ("_clips", "_sermon"):
        name = name.removesuffix(suffix)
    return name


def date_in(name: str) -> str | None:
    m = _DATE.search(name)
    return m.group(0) if m else None


# --- fingerprints -----------------------------------------------------------

# Hashing a clip reads the whole video (tens of MB), and the review app asks
# for every clip's status on each page load; a changed file has a new size or
# modification time, so those key the cache.
_SHA_CACHE: dict[tuple, str] = {}


def _file_sha(path: Path | None) -> str:
    if path is None or not path.exists():
        return hashlib.sha256(b"").hexdigest()
    st = path.stat()
    key = (str(path.resolve()), st.st_size, st.st_mtime_ns)
    if key not in _SHA_CACHE:
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        if len(_SHA_CACHE) > 500:
            _SHA_CACHE.clear()
        _SHA_CACHE[key] = h.hexdigest()
    return _SHA_CACHE[key]


def fingerprint(mp4: Path, jpg: Path | None, title: str, description: str) -> str:
    """One digest over everything a post is made of: the video, the cover and
    the text. An approval names this, so any re-render or text edit after it
    means the approval no longer covers what would be posted."""
    text = hashlib.sha256(f"{title}\n{description}".encode("utf-8")).hexdigest()
    parts = f"{_file_sha(mp4)}:{_file_sha(jpg)}:{text}"
    return hashlib.sha256(parts.encode("ascii")).hexdigest()


def manifest_fingerprint(folder: Path, entry: dict) -> str:
    mp4 = folder / entry["file"]
    return fingerprint(mp4, mp4.with_suffix(".jpg"), entry["title"], entry.get("description", ""))


# --- services ---------------------------------------------------------------

def upsert_service(conn: sqlite3.Connection, service_id: str, **fields) -> None:
    """Create the service or update the given fields; others are left alone.
    Not audited by itself: callers record the event that explains the change."""
    unknown = set(fields) - set(_SERVICE_FIELDS)
    if unknown:
        raise ValueError(f"unknown service fields: {sorted(unknown)}")
    at = audit.now()
    fields = {k: (str(v) if isinstance(v, Path) else v) for k, v in fields.items()}
    cols = ["id", "created_at", "updated_at", *fields]
    sets = ", ".join(f"{k} = excluded.{k}" for k in ["updated_at", *fields])
    conn.execute(
        f"INSERT INTO services ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))}) "
        f"ON CONFLICT(id) DO UPDATE SET {sets}",
        (service_id, at, at, *fields.values()))


def get_service(conn: sqlite3.Connection, service_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM services WHERE id = ?", (service_id,)).fetchone()
    return dict(row) if row else None


def list_services(conn: sqlite3.Connection, limit: int | None = None) -> list[dict]:
    sql = """SELECT s.*,
                (SELECT COUNT(*) FROM clips c WHERE c.service_id = s.id) AS clip_count,
                (SELECT COUNT(*) FROM candidates c WHERE c.service_id = s.id) AS candidate_count,
                (SELECT r.status FROM runs r WHERE r.service_id = s.id
                    ORDER BY r.started_at DESC LIMIT 1) AS last_run_status
             FROM services s ORDER BY COALESCE(s.date, ''), s.created_at"""
    rows = [dict(r) for r in conn.execute(sql)]
    rows.reverse()  # newest first
    return rows[:limit] if limit else rows


# --- candidates -------------------------------------------------------------

def replace_candidates(conn: sqlite3.Connection, service_id: str, run_id: str | None,
                       candidates: list[dict]) -> None:
    """A new selection replaces the last one's candidates (the audit log keeps
    the history of what was rendered from each)."""
    conn.execute("DELETE FROM candidates WHERE service_id = ?", (service_id,))
    conn.executemany(
        "INSERT INTO candidates (service_id, n, run_id, title, start_s, end_s, pick_score, "
        "verdict, standalone, note, outcome, clip_file) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [(service_id, c["n"], run_id, c["title"], c["start"], c["end"], c.get("pick_score"),
          c.get("verdict"), c.get("standalone"), c.get("note"), c["outcome"],
          c.get("clip_file")) for c in candidates])


def candidates(conn: sqlite3.Connection, service_id: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM candidates WHERE service_id = ? ORDER BY "
        "CASE outcome WHEN 'rendered' THEN 0 ELSE 1 END, standalone DESC, n", (service_id,))]


# --- clips ------------------------------------------------------------------

def record_clips(conn: sqlite3.Connection, actor: Actor, service_id: str, folder: Path,
                 entries: list[dict], rendered: set[str], run_id: str | None,
                 event: str = "clip.rendered") -> None:
    """Make the clips table match a clips.json's `entries` (each carrying its
    "fingerprint"), and audit each clip in `rendered` (the ones this call made).
    Rows for files no longer in the manifest are dropped."""
    at = audit.now()
    keep = [e["file"] for e in entries]
    conn.execute(f"DELETE FROM clips WHERE service_id = ? AND file NOT IN "
                 f"({', '.join('?' * len(keep))})", (service_id, *keep))
    for n, e in enumerate(entries, 1):
        if e["file"] in rendered:
            conn.execute(
                "INSERT INTO clips (service_id, file, n, title, description, start_s, end_s, "
                "score, fingerprint, rendered_at, run_id) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(service_id, file) DO UPDATE SET n = excluded.n, "
                "title = excluded.title, description = excluded.description, "
                "start_s = excluded.start_s, end_s = excluded.end_s, score = excluded.score, "
                "fingerprint = excluded.fingerprint, rendered_at = excluded.rendered_at, "
                "run_id = excluded.run_id",
                (service_id, e["file"], n, e["title"], e.get("description", ""), e["start"],
                 e["end"], e.get("score"), e["fingerprint"], at, run_id))
            audit.record(conn, actor, event, service=service_id, clip=e["file"],
                         title=e["title"], seconds=round(e["end"] - e["start"], 1),
                         fingerprint=e["fingerprint"], run=run_id)
        else:
            conn.execute("UPDATE clips SET n = ? WHERE service_id = ? AND file = ?",
                         (n, service_id, e["file"]))


def clips(conn: sqlite3.Connection, service_id: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM clips WHERE service_id = ? ORDER BY n", (service_id,))]


# --- importing an existing clips folder -------------------------------------

def import_folder(conn: sqlite3.Connection, actor: Actor, folder: Path,
                  title: str | None = None, speaker: str | None = None,
                  date: str | None = None) -> tuple[str, int]:
    """Bring a clips folder made before the database existed into it: the
    service, its clips with fresh fingerprints (also written into clips.json)
    the sermon window if one was recorded, and the posts in published.json.
    Candidates from back then were never saved, so there are none to import.
    Safe to run again. Returns (service id, clips)."""
    from .db import write

    manifest_path = folder / "clips.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"no clips.json in {folder}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    service_id = service_id_for(folder)
    for e in manifest["clips"]:
        e["fingerprint"] = manifest_fingerprint(folder, e)

    fields = {"source": "import", "clips_dir": folder, "summary": manifest.get("summary"),
              "date": date or date_in(service_id)}
    if title:
        fields["title"] = title
    if speaker:
        fields["speaker"] = speaker
    recording = folder.parent / f"{service_id}.mp4"
    if recording.exists():
        fields["recording_file"] = recording
    sermon = folder.parent / f"{service_id}_sermon.mp4"
    if sermon.exists():
        fields["sermon_file"] = sermon
    windows = folder.parent / f"{service_id}.sermon-windows.json"
    if windows.exists():
        record = json.loads(windows.read_text(encoding="utf-8"))
        labeler = "claude" if "claude" in record else next(iter(record), None)
        if labeler:
            w = record[labeler]
            # The raw window, before sentence snapping and padding.
            fields.update(labeler=labeler, sermon_title=w.get("title"),
                          sermon_start=w.get("start"), sermon_end=w.get("end"),
                          sermon_reason=w.get("reason"))

    with write(conn):
        upsert_service(conn, service_id, **{k: v for k, v in fields.items() if v is not None})
        record_clips(conn, actor, service_id, folder, manifest["clips"],
                     {e["file"] for e in manifest["clips"]}, None, event="clip.imported")
        audit.record(conn, actor, "service.imported", service=service_id,
                     folder=str(folder), clips=len(manifest["clips"]))
    # After the commit, so a failed import leaves clips.json as it was.
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False),
                             encoding="utf-8")
    from .posts import import_ledger  # posts imports this module
    import_ledger(conn, actor, service_id, folder)
    return service_id, len(manifest["clips"])


# --- finding a clips folder -------------------------------------------------

DATA_ENV = "SERMON_SHORTS_DATA"


def data_root() -> Path:
    return Path(os.environ.get(DATA_ENV) or Path.home() / "Downloads").expanduser()


def newest_clips_folder() -> Path | None:
    """The most recently written *_clips folder: publish's long-standing default,
    shared so approve and publish always pick the same folder."""
    manifests = sorted(data_root().glob("*_clips/clips.json"), key=lambda p: p.stat().st_mtime)
    return manifests[-1].parent if manifests else None


def resolve_folder(conn: sqlite3.Connection, ref: str | None) -> Path:
    """A clips folder from a path, a service id, or nothing (the newest)."""
    if ref is None:
        folder = newest_clips_folder()
        if folder is None:
            raise FileNotFoundError(f"no *_clips folder in {data_root()}; name one")
        return folder
    path = Path(ref).expanduser()
    if (path / "clips.json").exists():
        return path.resolve()
    service = get_service(conn, ref)
    if service and service["clips_dir"] and (Path(service["clips_dir"]) / "clips.json").exists():
        return Path(service["clips_dir"])
    raise FileNotFoundError(f"{ref!r} is neither a clips folder nor a service with one")


def load_manifest(folder: Path) -> list[dict]:
    return json.loads((folder / "clips.json").read_text(encoding="utf-8"))["clips"]


def ensure_service(conn: sqlite3.Connection, actor: Actor, folder: Path) -> str:
    """The folder's service id, importing the folder first if the database has
    never seen it (folders made before it existed, or on another machine), and
    picking up any published.json posts the database doesn't have yet."""
    from .posts import import_ledger  # posts imports this module

    service_id = service_id_for(folder)
    if get_service(conn, service_id) is None:
        import_folder(conn, actor, folder)
    else:
        import_ledger(conn, actor, service_id, folder)
    return service_id
