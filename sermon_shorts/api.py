"""The HTTP API behind the review app: `python -m sermon_shorts serve`.

A thin layer over sermon_shorts/core, which holds every rule (roles, the
approval gate, the audit log), so the API and the CLI can't disagree. See
docs/api-design.md for the endpoint list and the reasoning.

Deployment: uvicorn listens on localhost only; on the sprite, Caddy proxies
/api/* to it next to the clip files it already serves, behind the sprite
URL's org auth. Identity inside the app comes from each person's API token,
sent as `Authorization: Bearer ssk_...` or held in an HttpOnly cookie set by
POST /api/v1/session. Cookie-authenticated writes must also send the header
`X-Requested-With: sermon-shorts`: browsers won't add a custom header to a
cross-site request without a CORS preflight, which this API never grants.

FastAPI and uvicorn are imported only by this module, so the rest of the
CLI works without them.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import __version__
from .core import audit, posts, records, review, runs, schedule, users, version
from .core.audit import Actor
from .core.db import PROJECT_DIR, SCHEMA_VERSION, connect, write
from .core.posts import PLATFORMS
from .core.users import AccountError, User

COOKIE = "ss_token"
CSRF_HEADER = "x-requested-with"
COOKIE_DAYS = 30
FILES_URL_ENV = "SERMON_SHORTS_FILES_URL"   # where Caddy serves the data root (default "/")
LOG_DIR_ENV = "SERMON_SHORTS_LOG_DIR"       # run logs the API may show (default ~/logs)
INSECURE_COOKIE_ENV = "SERMON_SHORTS_INSECURE_COOKIE"  # "1" for plain-http local testing
RUN_CMD_ENV = "SERMON_SHORTS_RUN_CMD"       # override how a weekly run is started
WEB_DIR = Path(__file__).resolve().parent / "web"  # the review app (static, no build step)

# The review app shows text that came from transcripts, Claude and people, so
# it never builds HTML from data; this policy backs that up by refusing any
# script or style that isn't the app's own files.
APP_CSP = ("default-src 'self'; script-src 'self'; "
           "style-src 'self' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
           "img-src 'self' data:; media-src 'self'; connect-src 'self'; "
           "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
GITHUB_REPO_ENV = "SERMON_SHORTS_GITHUB_REPO"  # e.g. pshouse/sermon-shorts, with GITHUB_TOKEN


# --- errors -----------------------------------------------------------------

class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _error_response(request: Request, e: ApiError) -> JSONResponse:
    return JSONResponse({"error": {"code": e.code, "message": e.message}}, status_code=e.status)


# --- request plumbing -------------------------------------------------------

def get_conn():
    conn = connect(any_thread=True)
    try:
        yield conn
    finally:
        conn.close()


def _token(request: Request) -> tuple[str | None, bool]:
    """(token, came from the cookie)."""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip(), False
    return request.cookies.get(COOKIE), True


def current_user(request: Request, conn=Depends(get_conn)) -> User:
    token, from_cookie = _token(request)
    user = users.authenticate(conn, token) if token else None
    if user is None:
        raise ApiError(401, "not_signed_in", "Sign in with your API token.")
    if from_cookie and request.method not in ("GET", "HEAD") and \
            request.headers.get(CSRF_HEADER) != "sermon-shorts":
        raise ApiError(403, "csrf", f"Writes need the header {CSRF_HEADER}: sermon-shorts.")
    return user


def need(permission: str):
    def check(user: User = Depends(current_user)) -> User:
        if not user.can(permission):
            raise ApiError(403, "forbidden", f"{user.login} is a {user.role}; this needs "
                           f"{users.PERMISSIONS[permission]} or above.")
        return user
    return check


def _actor(user: User) -> Actor:
    return user.actor("web")


def _me(user: User) -> dict:
    return {"login": user.login, "name": user.name, "role": user.role,
            "is_root": user.is_root,
            "can": sorted(p for p in users.PERMISSIONS if user.can(p))}


def _set_cookie(response: Response, token: str) -> None:
    response.set_cookie(COOKIE, token, max_age=COOKIE_DAYS * 86400, httponly=True,
                        secure=os.environ.get(INSECURE_COOKIE_ENV) != "1",
                        samesite="strict", path="/api")


# --- services and clips -----------------------------------------------------

def _service(conn, sid: str) -> tuple[dict, Path | None]:
    service = records.get_service(conn, sid)
    if service is None:
        raise ApiError(404, "no_service", f"No service {sid!r}.")
    folder = Path(service["clips_dir"]) if service["clips_dir"] else None
    return service, (folder if folder and (folder / "clips.json").exists() else None)


def _folder(conn, sid: str) -> tuple[dict, Path]:
    service, folder = _service(conn, sid)
    if folder is None:
        raise ApiError(409, "no_clips", f"{sid} has no clips folder yet.")
    return service, folder


def _clip_number(folder: Path, file: str) -> int:
    for n, entry in enumerate(records.load_manifest(folder), 1):
        if entry["file"] == file:
            return n
    raise ApiError(404, "no_clip", f"No clip {file!r} in {folder.name}.")


def _media_url(path: Path) -> str | None:
    """The URL Caddy serves a file at, if it's under the data root."""
    try:
        rel = path.resolve().relative_to(records.data_root().resolve())
    except ValueError:
        return None
    if not path.exists():
        return None
    prefix = os.environ.get(FILES_URL_ENV, "/").rstrip("/")
    return f"{prefix}/{quote(rel.as_posix())}"


def _clip_view(conn, sid: str, folder: Path, n: int, entry: dict, posted: dict) -> dict:
    fp = records.manifest_fingerprint(folder, entry)
    state, d = review.status(conn, sid, entry["file"], fp)
    mp4 = folder / entry["file"]
    return {
        "n": n, "file": entry["file"], "title": entry["title"],
        "description": entry.get("description", ""),
        "start": entry["start"], "end": entry["end"],
        "seconds": round(entry["end"] - entry["start"], 1), "score": entry.get("score"),
        "fingerprint": fp, "review_state": state, "review": review.describe(state, d),
        "decision": d, "posts": posted.get(entry["file"], {}),
        "notes": [h for h in review.history(conn, sid, entry["file"]) if h["kind"] == "note"],
        "video_url": _media_url(mp4), "cover_url": _media_url(mp4.with_suffix(".jpg")),
        "instagram_needs_manual": (entry["end"] - entry["start"]) > posts.IG_MAX_SECONDS,
    }


def _service_view(conn, service: dict, folder: Path | None) -> dict:
    sid = service["id"]
    posted = posts.posts_for(conn, sid)
    clips = ([_clip_view(conn, sid, folder, n, e, posted)
              for n, e in enumerate(records.load_manifest(folder), 1)] if folder else [])
    last = conn.execute("SELECT * FROM runs WHERE service_id = ? ORDER BY started_at DESC "
                        "LIMIT 1", (sid,)).fetchone()
    return {**service, "clips": clips,
            "needs_you": posts.needs_you(conn, sid, folder) if folder else [],
            "latest_run": dict(last) if last else None}


# --- publishing jobs --------------------------------------------------------
# Posting can take minutes (Meta processes each video), so it runs in a
# thread; the posts themselves are recorded durably as they happen. Job
# status lives in memory: after a restart, the service's posts are the record.

_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def _sprite_hold(name: str, stop: threading.Event) -> None:
    """Keep a sprite from pausing while background work runs (no-op elsewhere)."""
    if not shutil.which("sprite-env"):
        return
    while not stop.is_set():
        subprocess.run(["sprite-env", "curl", "-s", "-X", "PUT", "/v1/tasks", "-d",
                        json.dumps({"name": name, "expire": "5m"})],
                       capture_output=True, check=False)
        stop.wait(60)
    subprocess.run(["sprite-env", "curl", "-s", "-X", "DELETE", f"/v1/tasks/{name}"],
                   capture_output=True, check=False)


def _run_publish_job(job_id: str, user: User, sid: str, folder: Path, jobs: list[dict]) -> None:
    from .publish import post_jobs  # Google/Meta clients, only when posting

    job = _jobs[job_id]
    stop = threading.Event()
    threading.Thread(target=_sprite_hold, args=(f"publish-{job_id}", stop), daemon=True).start()

    def progress(i: int, stage: str, **info) -> None:
        # Only this thread writes; readers get a snapshot via _job_view.
        item = job["items"][i]
        now = time.time()
        if stage == "start":
            item.update(state="starting", started=now)
        elif stage in ("done", "failed"):
            item.update(state=stage, finished=now, percent=100 if stage == "done" else item["percent"],
                        url=info.get("url"), cover=info.get("cover"), error=info.get("error"))
        elif stage == "note":
            item["notes"].append(info.get("detail", ""))
        else:
            if stage != item["state"]:
                item.update(state=stage, percent=None, stage_started=now)
            if info.get("percent") is not None:
                item["percent"] = max(0, min(100, int(info["percent"])))
        job["updated"] = now

    conn = connect()
    try:
        job["results"] = post_jobs(conn, _actor(user), sid, folder, jobs, progress=progress)
        job["status"] = "failed" if any(r["error"] for r in job["results"]) else "succeeded"
    except Exception as e:  # recorded so the app can show it; posts made so far are kept
        job["status"], job["error"] = "failed", f"{type(e).__name__}: {e}"
    finally:
        job["finished_at"] = audit.now()
        stop.set()
        conn.close()


def _job_view(job: dict) -> dict:
    """A posting job as the app shows it: each post's stage, percentage when
    known, and seconds spent so far, measured here so clocks can't disagree."""
    now = time.time()
    items = []
    for it in job["items"]:
        end = it["finished"] or now
        items.append({**{k: v for k, v in it.items() if k not in ("started", "stage_started", "finished")},
                      "elapsed": int(end - it["started"]) if it["started"] else 0,
                      "stage_elapsed": int(end - it["stage_started"]) if it["stage_started"] else 0})
    done = sum(1 for it in items if it["state"] in ("done", "failed"))
    return {**{k: v for k, v in job.items() if k not in ("items", "updated")},
            "items": items, "done": done, "total": len(items),
            "failed": sum(1 for it in items if it["state"] == "failed")}


# --- runs -------------------------------------------------------------------

_spawned: dict[str, subprocess.Popen] = {}  # runs this process started, by run id


def _run_command(labeler: str, force: bool) -> list[str]:
    extra = ["--labeler", labeler] + (["--force"] if force else [])
    if os.environ.get(RUN_CMD_ENV):
        return os.environ[RUN_CMD_ENV].split() + extra
    script = PROJECT_DIR / "scripts" / "sprite_weekly.sh"
    if shutil.which("sprite-env") and script.exists():
        # The same path as the Sunday trigger: task hold, git pull, log file.
        return [str(script), *extra]
    return [sys.executable, "-m", "sermon_shorts", "--weekly", *extra]


def _starting(run_id: str) -> dict | None:
    """A run this process spawned that hasn't opened its database row yet
    (sprite_weekly.sh pulls and installs first) or died before it could."""
    proc = _spawned.get(run_id)
    if proc is None:
        return None
    code = proc.poll()
    return {"id": run_id, "status": "starting" if code is None else "failed",
            "error": None if code is None else f"exited with status {code} before starting",
            "pid": proc.pid}


def _log_dir() -> Path:
    return Path(os.environ.get(LOG_DIR_ENV) or Path.home() / "logs").expanduser().resolve()


_github_cache: dict = {"at": 0.0, "runs": []}


def _github_runs() -> list[dict]:
    """weekly.yml runs from GitHub, cached a minute. Runs that failed before
    reaching the sprite (like a missing SPRITES_TOKEN) exist only here."""
    token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get(GITHUB_REPO_ENV)
    if not (token and repo):
        return []
    if time.monotonic() - _github_cache["at"] < 60:
        return _github_cache["runs"]
    import httpx
    try:
        r = httpx.get(f"https://api.github.com/repos/{repo}/actions/workflows/weekly.yml/runs",
                      params={"per_page": 20}, timeout=10,
                      headers={"Authorization": f"Bearer {token}",
                               "Accept": "application/vnd.github+json"})
        r.raise_for_status()
        found = [{"github_run_id": str(w["id"]), "event": w["event"], "status": w["status"],
                  "conclusion": w["conclusion"], "created_at": w["created_at"],
                  "url": w["html_url"], "actor": (w.get("triggering_actor") or {}).get("login")}
                 for w in r.json().get("workflow_runs", [])]
    except Exception:  # GitHub being down shouldn't break the history page
        return _github_cache["runs"]
    _github_cache.update(at=time.monotonic(), runs=found)
    return found


# --- request bodies ---------------------------------------------------------

class SessionIn(BaseModel):
    token: str


class InviteIn(BaseModel):
    role: Literal["reviewer", "approver", "owner"]
    note: str = ""
    days: int = Field(users.INVITE_DAYS, ge=1, le=30)


class AcceptIn(BaseModel):
    code: str
    login: str
    name: str


class RoleIn(BaseModel):
    role: Literal["reviewer", "approver", "owner"]


class DecisionIn(BaseModel):
    decision: Literal["approve", "reject"]
    fingerprint: str = Field(..., description="the version the person watched")
    note: str = ""


class NoteIn(BaseModel):
    text: str


class PlanIn(BaseModel):
    clips: list[str] = Field(..., min_length=1, description="clip files")
    platforms: list[Literal["youtube", "instagram", "facebook"]] | None = None


class PublishIn(PlanIn):
    plan_id: str


class ManualPostIn(BaseModel):
    url: str
    note: str = ""


class PauseIn(BaseModel):
    reason: str = ""


class SkipIn(BaseModel):
    date: str = Field(..., description="YYYY-MM-DD, in the church's timezone")
    reason: str = ""


class RunIn(BaseModel):
    kind: Literal["weekly"] = "weekly"
    labeler: Literal["claude", "jev"] = "claude"
    force: bool = False


# --- the app ----------------------------------------------------------------

def create_app() -> FastAPI:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_DIR / ".env")  # Meta token, GITHUB_TOKEN, etc.

    app = FastAPI(title="Sermon Shorts API", version=__version__,
                  docs_url="/api/docs", openapi_url="/api/openapi.json", redoc_url=None)
    app.add_exception_handler(ApiError, _error_response)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/app"):
            response.headers["Content-Security-Policy"] = APP_CSP
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Referrer-Policy"] = "same-origin"
            # Always fetch the newest app after a deploy (it's a few small files).
            response.headers["Cache-Control"] = "no-cache"
        return response
    v1 = "/api/v1"

    def wrap(fn):
        """Core errors become 4xx responses."""
        try:
            return fn()
        except review.StaleError as e:
            raise ApiError(409, "approval_stale", str(e))
        except (review.ReviewError, AccountError) as e:
            raise ApiError(400, "not_allowed", str(e))

    # -- session --

    running = {**version.info(), "started_at": audit.now()}
    disk_cache: dict = {"at": 0.0, "value": None}

    def on_disk() -> dict:
        """The checkout now, and whether this process is behind it. Cached a
        little: every Studio page load asks."""
        if time.monotonic() - disk_cache["at"] > 30:
            now_commit = version.commit()
            changed = (version.python_changed_since(running["commit"])
                       if running["commit"] and now_commit and now_commit != running["commit"]
                       else [])
            disk_cache.update(at=time.monotonic(), value={
                "version": version.version_on_disk(), "commit": now_commit,
                # Web files are read per request; only Python needs a restart.
                "restart_needed": bool(changed), "changed_files": changed or []})
        return disk_cache["value"]

    @app.get(f"{v1}/health")
    def health():
        return {"ok": True, "version": running["version"], "commit": running["commit"],
                "dirty": running["dirty"], "label": version.label(running),
                "started_at": running["started_at"], "schema": SCHEMA_VERSION,
                "on_disk": on_disk()}

    @app.post(f"{v1}/session")
    def sign_in(body: SessionIn, response: Response, conn=Depends(get_conn)):
        user = users.authenticate(conn, body.token)
        if user is None:
            raise ApiError(401, "bad_token", "That token isn't valid (revoked, or mistyped).")
        _set_cookie(response, body.token)
        return _me(user)

    @app.delete(f"{v1}/session")
    def sign_out(response: Response):
        response.delete_cookie(COOKIE, path="/api")
        return {"ok": True}

    @app.get(f"{v1}/me")
    def me(user: User = Depends(current_user)):
        return _me(user)

    # -- people --

    @app.get(f"{v1}/users")
    def list_people(user: User = Depends(need("view")), conn=Depends(get_conn)):
        return [{"login": u.login, "name": u.name, "role": u.role, "is_root": u.is_root,
                 "created_at": u.created_at} for u in users.list_users(conn)]

    @app.post(f"{v1}/invites", status_code=201)
    def create_invite(body: InviteIn, user: User = Depends(need("manage_people")),
                      conn=Depends(get_conn)):
        invite_id, code = wrap(lambda: users.invite(conn, user, body.role, body.note,
                                                    body.days, via="web"))
        return {"id": invite_id, "code": code, "role": body.role,
                "note": "Shown once. Send it privately."}

    @app.get(f"{v1}/invites")
    def open_invites(user: User = Depends(need("manage_people")), conn=Depends(get_conn)):
        return [{k: v for k, v in r.items() if k != "code_sha256"}
                for r in users.list_invites(conn)]

    @app.delete(f"{v1}/invites/{{invite_id}}")
    def withdraw_invite(invite_id: str, user: User = Depends(need("manage_people")),
                        conn=Depends(get_conn)):
        wrap(lambda: users.revoke_invite(conn, user, invite_id, via="web"))
        return {"ok": True}

    @app.post(f"{v1}/invites/accept", status_code=201)
    def accept_invite(body: AcceptIn, response: Response, conn=Depends(get_conn)):
        new_user, token = wrap(lambda: users.accept_invite(conn, body.code, body.login,
                                                           body.name, via="web"))
        _set_cookie(response, token)
        return {**_me(new_user), "token": token,
                "note": "Your API token is shown once; you're signed in on this browser."}

    @app.patch(f"{v1}/users/{{login}}")
    def change_role(login: str, body: RoleIn, user: User = Depends(need("manage_people")),
                    conn=Depends(get_conn)):
        changed = wrap(lambda: users.set_role(conn, user, login, body.role, via="web"))
        return {"login": changed.login, "role": changed.role}

    @app.delete(f"{v1}/users/{{login}}")
    def remove_person(login: str, user: User = Depends(need("manage_people")),
                      conn=Depends(get_conn)):
        wrap(lambda: users.remove(conn, user, login, via="web"))
        return {"ok": True}

    # -- services and clips --

    @app.get(f"{v1}/services")
    def list_services(limit: int = 20, user: User = Depends(need("view")),
                      conn=Depends(get_conn)):
        out = []
        for s in records.list_services(conn, limit=limit):
            _, folder = _service(conn, s["id"])
            out.append({**s, "needs_you": len(posts.needs_you(conn, s["id"], folder))
                        if folder else 0})
        return out

    @app.get(f"{v1}/services/{{sid}}")
    def get_service(sid: str, user: User = Depends(need("view")), conn=Depends(get_conn)):
        service, folder = _service(conn, sid)
        return _service_view(conn, service, folder)

    @app.get(f"{v1}/services/{{sid}}/candidates")
    def get_candidates(sid: str, user: User = Depends(need("view")), conn=Depends(get_conn)):
        _service(conn, sid)
        return records.candidates(conn, sid)

    @app.get(f"{v1}/services/{{sid}}/clips/{{file}}")
    def get_clip(sid: str, file: str, user: User = Depends(need("view")),
                 conn=Depends(get_conn)):
        _, folder = _folder(conn, sid)
        n = _clip_number(folder, file)
        entry = records.load_manifest(folder)[n - 1]
        view = _clip_view(conn, sid, folder, n, entry, posts.posts_for(conn, sid))
        view["history"] = review.history(conn, sid, file)
        view["audit"] = [e for e in audit.events(conn, service=sid) if e["clip"] == file]
        return view

    @app.post(f"{v1}/services/{{sid}}/clips/{{file}}/decisions", status_code=201)
    def decide(sid: str, file: str, body: DecisionIn, user: User = Depends(need("approve")),
               conn=Depends(get_conn)):
        _, folder = _folder(conn, sid)
        n = _clip_number(folder, file)
        d = wrap(lambda: review.decide(conn, user, folder, n, body.decision, note=body.note,
                                       via="web", expected_fingerprint=body.fingerprint))
        entry = records.load_manifest(folder)[n - 1]
        return {"decision": d, "clip": _clip_view(conn, sid, folder, n, entry,
                                                  posts.posts_for(conn, sid))}

    @app.post(f"{v1}/services/{{sid}}/clips/{{file}}/notes", status_code=201)
    def add_note(sid: str, file: str, body: NoteIn, user: User = Depends(need("note")),
                 conn=Depends(get_conn)):
        _, folder = _folder(conn, sid)
        n = _clip_number(folder, file)
        wrap(lambda: review.add_note(conn, user, folder, n, body.text, via="web"))
        return {"ok": True}

    # -- publishing --

    def _plan(conn, sid: str, body: PlanIn):
        from .publish import _configured
        _, folder = _folder(conn, sid)
        chosen = [_clip_number(folder, f) for f in dict.fromkeys(body.clips)]
        platforms = list(body.platforms or _configured())
        if not platforms:
            raise ApiError(409, "no_platforms", "No platforms are connected on the server.")
        jobs, skipped = posts.plan(conn, sid, folder, chosen, platforms)
        return folder, platforms, jobs, skipped, posts.plan_id(jobs, skipped)

    def _plan_view(jobs, skipped, pid, platforms) -> dict:
        def row(j):
            return {"n": j["n"], "file": j["entry"]["file"], "title": j["entry"]["title"],
                    "platform": j["platform"], "fingerprint": j["fingerprint"]}
        return {"plan_id": pid, "platforms": platforms, "posts": [row(j) for j in jobs],
                "skipped": [{**row(s), "kind": s["kind"], "reason": s["reason"]}
                            for s in skipped]}

    @app.post(f"{v1}/services/{{sid}}/publish-plans")
    def plan_publish(sid: str, body: PlanIn, user: User = Depends(need("publish")),
                     conn=Depends(get_conn)):
        folder, platforms, jobs, skipped, pid = _plan(conn, sid, body)
        with write(conn):
            audit.record(conn, _actor(user), "publish.planned", service=sid, plan=pid,
                         posts=[f"{j['entry']['file']}:{j['platform']}" for j in jobs])
        return _plan_view(jobs, skipped, pid, platforms)

    @app.post(f"{v1}/services/{{sid}}/publish", status_code=202)
    def publish(sid: str, body: PublishIn, user: User = Depends(need("publish")),
                conn=Depends(get_conn)):
        folder, platforms, jobs, skipped, pid = _plan(conn, sid, body)
        if pid != body.plan_id:
            raise ApiError(409, "plan_changed", "Something changed since the preview "
                           "(an approval, a re-render or another post). Preview again.")
        if not jobs:
            raise ApiError(409, "nothing_to_post", "Nothing in this plan can be posted.")
        job_id = "p_" + runs.new_id()[2:]
        with _jobs_lock:
            if any(j["status"] == "running" and j["service"] == sid for j in _jobs.values()):
                raise ApiError(409, "publish_active", "A publish for this service is running.")
            _jobs[job_id] = {
                "id": job_id, "service": sid, "plan_id": pid, "by": user.login,
                "status": "running", "started_at": audit.now(), "finished_at": None,
                "results": [], "error": None, "updated": time.time(),
                "items": [{"n": j["n"], "file": j["entry"]["file"], "title": j["entry"]["title"],
                           "platform": j["platform"], "state": "queued", "percent": None,
                           "started": None, "stage_started": None, "finished": None,
                           "url": None, "cover": None, "error": None, "notes": []}
                          for j in jobs]}
        threading.Thread(target=_run_publish_job, args=(job_id, user, sid, folder, jobs),
                         daemon=True).start()
        return {"job_id": job_id}

    @app.get(f"{v1}/publish-jobs")
    def publish_jobs(service: str | None = None, user: User = Depends(need("view"))):
        """Recent posting jobs, newest first: the Publish page picks a running
        one back up after a reload or a visit to another page."""
        found = [j for j in _jobs.values() if service is None or j["service"] == service]
        found.sort(key=lambda j: j["started_at"], reverse=True)
        return [_job_view(j) for j in found[:5]]

    @app.get(f"{v1}/publish-jobs/{{job_id}}")
    def publish_job(job_id: str, user: User = Depends(need("view"))):
        job = _jobs.get(job_id)
        if job is None:
            raise ApiError(404, "no_job", "No such publish job (the server may have restarted;"
                           " the service's posts show what went out).")
        return _job_view(job)

    @app.post(f"{v1}/services/{{sid}}/clips/{{file}}/posts/{{platform}}/manual", status_code=201)
    def mark_posted(sid: str, file: str, platform: str, body: ManualPostIn,
                    user: User = Depends(need("publish")), conn=Depends(get_conn)):
        if platform not in PLATFORMS:
            raise ApiError(404, "no_platform", f"Platforms are {', '.join(PLATFORMS)}.")
        if not body.url.startswith(("https://", "http://")):
            raise ApiError(400, "bad_url", "Give the post's link.")
        _, folder = _folder(conn, sid)
        n = _clip_number(folder, file)
        entry = records.load_manifest(folder)[n - 1]
        ledger = posts.load_ledger(folder)
        if platform in ledger.get(file, {}) or platform in posts.posts_for(conn, sid).get(file, {}):
            raise ApiError(409, "already_posted", f"{entry['title']} is already on {platform}.")
        fp = records.manifest_fingerprint(folder, entry)
        from datetime import datetime
        posts.write_ledger_entry(folder, ledger, file, platform, {
            "id": None, "url": body.url, "manual": True, "by": user.login,
            "at": datetime.now().isoformat(timespec="seconds")})
        posts.record_post(conn, _actor(user), sid, file, platform, None, body.url, fp,
                          manual=True, note=body.note)
        return {"ok": True}

    @app.post(f"{v1}/services/{{sid}}/clips/{{file}}/posts/youtube/cover")
    def retry_cover(sid: str, file: str, user: User = Depends(need("publish")),
                    conn=Depends(get_conn)):
        from .publish import PublishError, set_youtube_cover
        _, folder = _folder(conn, sid)
        _clip_number(folder, file)
        post = posts.posts_for(conn, sid).get(file, {}).get("youtube")
        if not post or not post["post_id"]:
            raise ApiError(409, "not_on_youtube", "That clip isn't on YouTube.")
        jpg = (folder / file).with_suffix(".jpg")
        try:
            set_youtube_cover(post["post_id"], jpg)
        except PublishError as e:
            posts.record_cover(conn, _actor(user), sid, file, "youtube", False, str(e))
            raise ApiError(502, "cover_failed", f"YouTube still refused the cover: {e}")
        posts.record_cover(conn, _actor(user), sid, file, "youtube", True)
        return {"ok": True}

    # -- runs --

    @app.get(f"{v1}/runs")
    def list_runs(limit: int = 20, user: User = Depends(need("view")), conn=Depends(get_conn)):
        mine = runs.list_runs(conn, limit)
        github = {g["github_run_id"]: g for g in _github_runs()}
        for r in mine:
            r["github"] = github.pop(r["github_run_id"], None) if r["github_run_id"] else None
        starting = [s for rid, _ in list(_spawned.items())
                    if (s := _starting(rid)) and not runs.get_run(conn, rid)]
        # GitHub runs with no sprite run never reached the sprite.
        orphans = [{**g, "source": "github", "reached_sprite": False} for g in github.values()]
        return {"runs": starting + mine, "github_only": orphans}

    @app.post(f"{v1}/runs", status_code=202)
    def start_run(body: RunIn, user: User = Depends(need("run")), conn=Depends(get_conn)):
        active = runs.running(conn)
        pending = [rid for rid in _spawned if (s := _starting(rid)) and s["status"] == "starting"
                   and not runs.get_run(conn, rid)]
        if active or pending:
            rid = active["id"] if active else pending[0]
            raise ApiError(409, "run_active", f"Run {rid} is in progress.")
        run_id = runs.new_id()
        env = {**os.environ, runs.RUN_ID_ENV: run_id, runs.USER_ENV: user.login}
        env.pop(runs.STARTED_BY_ENV, None)
        env.pop(runs.GITHUB_RUN_ENV, None)
        _spawned[run_id] = subprocess.Popen(
            _run_command(body.labeler, body.force), cwd=PROJECT_DIR, env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)  # outlives an API restart; cancel signals its group
        with write(conn):
            audit.record(conn, _actor(user), "run.requested", run=run_id, kind=body.kind,
                         labeler=body.labeler, force=body.force)
        return {"run_id": run_id}

    def _run_or_404(conn, run_id: str) -> dict:
        run = runs.get_run(conn, run_id) or _starting(run_id)
        if run is None:
            raise ApiError(404, "no_run", f"No run {run_id!r}.")
        return run

    @app.get(f"{v1}/runs/{{run_id}}")
    def get_run(run_id: str, user: User = Depends(need("view")), conn=Depends(get_conn)):
        run = _run_or_404(conn, run_id)
        return {**run, "events": runs.events(conn, run_id)}

    @app.get(f"{v1}/runs/{{run_id}}/events")
    async def run_events(run_id: str, request: Request, user: User = Depends(need("view"))):
        """JSON, or with Accept: text/event-stream a live stream that ends with the run."""
        conn = connect()
        try:
            _run_or_404(conn, run_id)
            if "text/event-stream" not in request.headers.get("accept", ""):
                return runs.events(conn, run_id)
        finally:
            conn.close()

        async def stream():
            seen = 0
            while not await request.is_disconnected():
                c = connect()
                try:
                    evs = runs.events(c, run_id)
                    run = runs.get_run(c, run_id) or _starting(run_id) or {}
                finally:
                    c.close()
                for e in evs[seen:]:
                    yield f"event: step\ndata: {json.dumps(e)}\n\n"
                seen = len(evs)
                if run.get("status") not in ("running", "starting"):
                    yield f"event: end\ndata: {json.dumps({'status': run.get('status')})}\n\n"
                    return
                await asyncio.sleep(2)
        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache"})

    @app.get(f"{v1}/runs/{{run_id}}/log", response_class=PlainTextResponse)
    def run_log(run_id: str, lines: int = 200, user: User = Depends(need("view")),
                conn=Depends(get_conn)):
        run = _run_or_404(conn, run_id)
        path = Path(run.get("log_path") or "").expanduser()
        # Only files in the run log folder, whatever the row says.
        if not run.get("log_path") or not path.resolve().is_relative_to(_log_dir()) \
                or not path.is_file():
            raise ApiError(404, "no_log", "No log file for this run.")
        text = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(text[-max(1, min(lines, 5000)):]) + "\n"

    @app.post(f"{v1}/runs/{{run_id}}/cancel", status_code=202)
    def cancel_run(run_id: str, user: User = Depends(need("run")), conn=Depends(get_conn)):
        runs.running(conn)  # close runs whose process is gone, so a stale pid is never signalled
        run = _run_or_404(conn, run_id)
        if run["status"] not in ("running", "starting"):
            raise ApiError(409, "not_running", f"Run {run_id} is {run['status']}.")
        proc = _spawned.get(run_id)
        try:
            if proc is not None and proc.poll() is None:
                os.killpg(proc.pid, signal.SIGTERM)  # the script and the pipeline under it
            elif run.get("host") == socket.gethostname() and run.get("pid"):
                os.kill(run["pid"], signal.SIGTERM)
            else:
                raise ApiError(409, "not_here", "That run is on another machine.")
        except ProcessLookupError:
            pass  # already gone; the next start reaps it
        with write(conn):
            audit.record(conn, _actor(user), "run.cancel_requested", run=run_id,
                         service=run.get("service_id"))
        return {"ok": True}

    # -- the Sunday schedule --

    def sched(fn):
        try:
            return fn()
        except schedule.ScheduleError as e:
            raise ApiError(400, "not_allowed", str(e))

    @app.get(f"{v1}/schedule")
    def get_schedule(user: User = Depends(need("view")), conn=Depends(get_conn)):
        recent = [r for r in runs.list_runs(conn, 200)
                  if r["started_by"] == schedule.SCHEDULE_STARTER][:12]
        missed = [{**g, "reached_sprite": False} for g in _github_runs()
                  if g["event"] == "schedule"
                  and not conn.execute("SELECT 1 FROM runs WHERE github_run_id = ?",
                                       (g["github_run_id"],)).fetchone()]
        return {**schedule.overview(conn), "recent": recent, "never_reached_sprite": missed}

    @app.post(f"{v1}/schedule/pause")
    def pause_schedule(body: PauseIn, user: User = Depends(need("run")), conn=Depends(get_conn)):
        sched(lambda: schedule.set_paused(conn, user, True, body.reason, via="web"))
        return schedule.overview(conn)

    @app.post(f"{v1}/schedule/resume")
    def resume_schedule(user: User = Depends(need("run")), conn=Depends(get_conn)):
        sched(lambda: schedule.set_paused(conn, user, False, via="web"))
        return schedule.overview(conn)

    @app.post(f"{v1}/schedule/skips", status_code=201)
    def skip_date(body: SkipIn, user: User = Depends(need("run")), conn=Depends(get_conn)):
        sched(lambda: schedule.skip(conn, user, body.date, body.reason, via="web"))
        return schedule.overview(conn)

    @app.delete(f"{v1}/schedule/skips/{{day}}")
    def unskip_date(day: str, user: User = Depends(need("run")), conn=Depends(get_conn)):
        sched(lambda: schedule.unskip(conn, user, day, via="web"))
        return schedule.overview(conn)

    # -- audit --

    @app.get(f"{v1}/audit")
    def audit_log(service: str | None = None, action: str | None = None, limit: int = 100,
                  user: User = Depends(need("view")), conn=Depends(get_conn)):
        return audit.events(conn, service=service, action=action, limit=min(limit, 5000))

    @app.get(f"{v1}/audit.csv", response_class=PlainTextResponse)
    def audit_csv(service: str | None = None, action: str | None = None,
                  user: User = Depends(need("view")), conn=Depends(get_conn)):
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["seq", "at", "actor", "via", "action", "service", "clip", "data", "hash"])
        for r in audit.events(conn, service=service, action=action):
            w.writerow([r["seq"], r["at"], r["actor"], r["via"], r["action"], r["service"] or "",
                        r["clip"] or "", json.dumps(r["data"], ensure_ascii=False), r["hash"]])
        return Response(out.getvalue(), media_type="text/csv", headers={
            "Content-Disposition": 'attachment; filename="sermon-shorts-audit.csv"'})

    @app.get(f"{v1}/audit/verify")
    def audit_verify(user: User = Depends(need("view")), conn=Depends(get_conn)):
        v = audit.verify(conn)
        return {"ok": v.ok, "events": v.events, "tail": v.tail, "first_bad": v.first_bad,
                "problem": v.problem or None}

    # -- the review app --

    from fastapi.staticfiles import StaticFiles

    @app.get("/app", include_in_schema=False)
    def app_root():
        return RedirectResponse("/app/")

    app.mount("/app", StaticFiles(directory=WEB_DIR, html=True), name="app")
    return app


def serve(argv: list[str]) -> int:
    import argparse

    import uvicorn

    p = argparse.ArgumentParser(prog="sermon-shorts serve",
                                description="Run the review app's API (behind Caddy).")
    p.add_argument("--host", default="127.0.0.1",
                   help="default 127.0.0.1: only Caddy on the same machine reaches it")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--files", action="store_true",
                   help="also serve the clip files (the data root) at /, for trying the app "
                        "without Caddy; on the sprite Caddy serves them")
    args = p.parse_args(argv)
    app = create_app()
    if args.files:
        from fastapi.staticfiles import StaticFiles
        app.mount("/", StaticFiles(directory=records.data_root()), name="files")
        print(f"Serving {records.data_root()} at / and the app at "
              f"http://{args.host}:{args.port}/app/")
    uvicorn.run(app, host=args.host, port=args.port, proxy_headers=True,
                forwarded_allow_ips="127.0.0.1")
    return 0
