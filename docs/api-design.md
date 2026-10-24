# API and CLI layer: design

Status: proposal (2026-10-24). Nothing here is built yet.

## Goal

Give the review UI (the "Sermon Shorts Studio" design: This week, Review,
Publish, History & audit) a backend it can drive, and give the CLI the same
operations, so that:

- a person can review, approve, publish and follow up on clips from a browser
  or phone, without `sprite exec`;
- every run, decision and post is recorded with who, when and exactly what,
  in a log that can't be quietly edited;
- nothing can be posted that a named person has not approved in its current
  form.

Non-goals for now: multi-church tenancy, editing cut points in the browser,
a public (non-org) URL, replacing the GitHub Actions trigger.

## What exists today

| Concern | Where it lives now | Persisted? |
|---|---|---|
| Recording metadata (title, date, speaker) | `subsplash.latest_recording()` | No, printed only |
| Sermon window | `<video>.sermon-windows.json` | Yes (raw window per labeler) |
| Clip candidates and Claude's review scores/notes | `highlights.review_clips()` | No, printed only; dropped candidates vanish |
| Rendered clips | `<sermon>_clips/clips.json` + `NN_slug.mp4/.jpg/.txt` | Yes |
| Posts | `<clips folder>/published.json` (`file -> platform -> {id,url,at}`) | Yes, but no "who", no manual posts, no failures |
| Runs | GitHub Actions + `~/logs/weekly-*.log` on the sprite | Text log only |
| Approvals | Someone saying so in chat | No |

So the UI's "Needs you", "Below the bar", per-clip record, run steps and
audit log all depend on data that is not written today. Phase 1 below fixes
that before any server exists.

## Shape

```
            +------------------+      +-------------------+
 browser -> | Caddy :8080      | ---> | uvicorn :8000     |
 (org auth  |  /files/* static |      |  /api/v1/*        |
  via the   |  /api/*  proxy   |      |  (FastAPI)        |
  sprite)   +------------------+      +---------+---------+
                                                |
 sprite exec / laptop shell -> sermon_shorts CLI|
                                                v
                                 sermon_shorts/core/   (one library)
                                   services.py  runs.py  decisions.py
                                   posts.py     audit.py  store.py
                                                |
                                     files under the data root
```

- **One core library, two front ends.** `sermon_shorts/core/` holds every
  operation (list services, approve, plan a publish, start a run...). The
  CLI and the HTTP API are thin wrappers over it, so they cannot disagree on
  the rules. Today's `cli.main()` and `publish.main()` move their logic
  behind it without changing their flags or output.
- **Files stay the source of truth.** The project already keeps everything as
  files next to the video, which makes `sprite file pull` and debugging easy.
  Keep that: no database. New records are JSON/JSONL files in the same
  folders. The audit log is the one append-only file.
- **Caddy stays in front.** It already serves `~/Downloads` with byte-range
  support (iOS needs it). It gains one `reverse_proxy /api/* localhost:8000`
  line, so the UI and API share one origin and the sprite's single HTTP port.

## Data model

A **service** is one Sunday recording. Its id is the recording's file stem,
e.g. `Live_2026-10-18_7am`, which is already unique and already names its
folders.

Data root: `SERMON_SHORTS_DATA` (default `~/Downloads`, as now).

```
<data root>/
  Live_2026-10-18_7am.mp4                    (existing)
  Live_2026-10-18_7am.sermon-windows.json    (existing)
  Live_2026-10-18_7am_sermon.mp4             (existing)
  Live_2026-10-18_7am_sermon_clips/
    clips.json          (existing; each clip gains "fingerprint")
    published.json      (existing; entries gain "by", "manual", "cover")
    01_...mp4 .jpg .txt (existing)
    service.json        NEW  Subsplash metadata, sermon window, summary
    candidates.json     NEW  every candidate with verdict, scores, note, outcome
  runs/
    <run id>/run.json   NEW  kind, args, who/what started it, status, step timings
    <run id>/events.jsonl NEW  structured progress events
    <run id>/log.txt    NEW  the human log (what weekly-*.log is today)
  audit.jsonl           NEW  append-only, hash-chained, all services
  users.json            NEW  (project root, gitignored) people, roles, token hashes
```

### Clip fingerprint

`fingerprint = sha256(mp4 bytes) + sha256(jpg bytes) + sha256(title + "\n" + description)`,
stored as one combined hex digest in `clips.json` at render time.

An approval names the fingerprint it covers. A `--from-manifest` re-render
or a description edit changes the fingerprint, so the old approval stops
counting and the clip shows as "Waiting for review" again. This is the rule
the Review screen states: "Re-rendering the clip clears it."

### Candidates

`review_clips()` returns every review, not just the kept clips, and the
caller writes `candidates.json`:

```json
{"candidates": [
  {"n": 6, "title": "<title>", "start": 0.0, "end": 0.0, "pick_score": 0,
   "verdict": "adjust", "standalone": 65, "note": "As cut, 'Imagine this scene'...",
   "outcome": "below_bar", "clip_file": null},
  {"n": 2, "title": "Be Ready - God Wants to Use You", "pick_score": 80, "verdict": "keep",
   "standalone": 72, "outcome": "rendered", "clip_file": "01_be-ready-god-wants-to-use-you.mp4"}
]}
```

`outcome` is one of `rendered`, `below_bar` (standalone < `MIN_STANDALONE`),
`rejected`, `too_short`, `over_count` (passed but more than `--clips`).

### Audit log

`audit.jsonl`, one event per line, never rewritten:

```json
{"seq": 41, "at": "2026-10-24T14:14:03Z", "actor": {"name": "Patrick S.", "via": "web"},
 "action": "clip.approved", "service": "Live_2026-10-18_7am",
 "clip": "01_be-ready-god-wants-to-use-you.mp4",
 "data": {"fingerprint": "9f2c...", "note": ""},
 "prev": "c0a1...", "hash": "77be..."}
```

- `hash = sha256(prev + canonical JSON of the event without hash)`. Editing
  or deleting a line breaks every hash after it, and `audit verify` reports
  the first broken `seq`. This is tamper-evident, not tamper-proof: someone
  with shell access could rewrite the whole file. The tail hash is printed
  in each weekly run's log line and kept in the GitHub Actions log, which
  gives an outside copy to check against.
- Writes take an `fcntl` lock on `audit.jsonl.lock` so the API and a CLI
  command running at the same time can't interleave.
- Actions: `run.started`, `run.step`, `run.finished`, `run.failed`,
  `sermon.found`, `clip.rendered`, `clip.approved`, `clip.rejected`,
  `publish.planned`, `post.created`, `post.failed`, `post.skipped`,
  `post.marked_manual`, `cover.failed`, `cover.set`, `user.added`,
  `token.revoked`.
- Every write that matters goes to the audit log **and** to its own file
  (`published.json`, `clips.json`). The audit log is the history, and the
  per-folder files are the current state the rest of the code already reads.

## Rules the core enforces

1. **Publish needs a current approval.** For each clip, the latest
   `clip.approved`/`clip.rejected` event must be an approval whose
   fingerprint equals the clip's current fingerprint. Otherwise the clip is
   left out of the publish plan with the reason.
2. **What you confirm is what gets posted.** Publishing has two steps: a
   *plan* (the exact list of clip, platform pairs, plus what is skipped and
   why) and an *execute* step that takes the plan's id. If anything changed
   in between (a new approval, a re-render, a post already made from the
   CLI), the plan id no longer matches and execute returns `409`. This keeps
   today's "About to post PUBLICLY... [y/N]" guarantee for the browser.
3. **No double posts.** `published.json` stays the ledger, checked inside
   the same lock as the write.
4. **One render run at a time.** A `runs/active` lock file; a second start
   returns `409` with the active run's id. (The GitHub workflow's
   concurrency group already does this for scheduled runs.)
5. **Rejections need a note.** Approvals may have one.
6. **Instagram over `IG_MAX_SECONDS`** is planned as `skipped: manual_required`
   and appears in "Needs you" until someone records a manual post.

## Identity and roles

The sprite URL already needs Fly org auth, but that tells the app nothing
about *which* person is calling. Each person gets an API token:

```json
{"users": [
  {"name": "Patrick S.", "login": "pshouse", "roles": ["admin"],
   "tokens": [{"id": "t_8c1f", "sha256": "...", "created": "2026-10-24", "revoked": null}]}
]}
```

| Role | Can |
|---|---|
| `viewer` | read everything, export the audit log |
| `approver` | + approve/reject clips |
| `publisher` | + plan/execute publishes, mark manual posts, retry covers |
| `operator` | + start/cancel runs |
| `admin` | all of the above + manage users and tokens |

- The API takes `Authorization: Bearer <token>`. Only the sha256 is stored.
  The browser UI keeps the token in an HttpOnly cookie set by a one-time
  `/login?token=...` link from `users add`.
- The CLI acts as `SERMON_SHORTS_USER` (a `login` from `users.json`), or
  `--as <login>`. Approve, reject, publish and mark-posted refuse to run
  without one. Shell access is already full trust, so the CLI does not ask
  for a token; it records `"via": "cli"` so the log shows the difference.
- Optional setting `"require_two_people": true` in `church.json`: the person
  who publishes a clip must not be the person who approved it. Off by
  default, since today one person does both.

## HTTP API

Base `/api/v1`, JSON in and out. Errors are
`{"error": {"code": "approval_stale", "message": "..."}}` with a 4xx status.
Long jobs (runs, publishes) return `202` with a run id. Clients follow them
through `GET /runs/{id}` or the event stream.

### Services and clips

| Method | Path | Returns / does |
|---|---|---|
| GET | `/me` | name, roles |
| GET | `/services?limit=10` | newest first: id, title, date, speaker, counts, `needs_you` count |
| GET | `/services/{sid}` | metadata, sermon window, summary, clips (with status, fingerprint, latest decision, posts), `needs_you` items, latest run |
| GET | `/services/{sid}/candidates` | `candidates.json` |
| GET | `/services/{sid}/clips/{file}` | one clip, plus its audit events |
| POST | `/services/{sid}/clips/{file}/decisions` | body `{"decision":"approve"\|"reject","note":"","fingerprint":"..."}`. `409 approval_stale` if the fingerprint isn't current (the clip changed while you watched it) |

Media is not served by the API: each clip carries `video_url`,
`cover_url`, which are `/files/...` paths Caddy serves with range support.

`needs_you` is computed, not stored. It covers clips waiting for review,
approved clips not yet posted, manual posts still owed, covers that failed,
and a failed latest run.

### Publishing

| Method | Path | Returns / does |
|---|---|---|
| POST | `/services/{sid}/publish-plans` | body `{"clips": [...], "platforms": [...]?}`. Returns `{"plan_id", "posts": [{clip, platform}], "skipped": [{clip, platform, reason}]}`. Writes `publish.planned`. Posts nothing |
| POST | `/services/{sid}/publish` | body `{"plan_id": "..."}`. `409 plan_changed` if stale, otherwise `202 {"run_id"}`. Posts go out in a background job, one `post.created`/`post.failed` event each |
| POST | `/services/{sid}/clips/{file}/posts/{platform}/manual` | body `{"url": "...", "note": ""}`. Records a post made by hand (Instagram over 65s) |
| POST | `/services/{sid}/clips/{file}/posts/youtube/cover` | Retries the cover upload; `cover.set` or `cover.failed` |

`plan_id = sha256(posts + skipped + every involved fingerprint + ledger state)`,
so it needs no server-side storage and expires itself when anything moves.

### Runs

| Method | Path | Returns / does |
|---|---|---|
| POST | `/runs` | body `{"kind": "weekly", "labeler": "claude", "force": false}` or `{"kind": "rerender", "service": sid, "clip": file}`. `202 {"run_id"}`; `409 run_active` |
| GET | `/runs?limit=20` | sprite runs merged with GitHub Actions runs of `weekly.yml` (see below) |
| GET | `/runs/{id}` | `run.json`: status, steps with durations, outcome |
| GET | `/runs/{id}/events` | `events.jsonl`; with `Accept: text/event-stream`, a live stream for the progress view |
| GET | `/runs/{id}/log` | `log.txt` as text |
| POST | `/runs/{id}/cancel` | SIGTERM to the process group, `run.failed` with `reason: cancelled` |

**GitHub runs.** The Oct 18 failures never reached the sprite (the token
was missing), so the sprite has no record of them. `GET /runs` therefore
also reads `weekly.yml` runs from the GitHub API, using a read-only
`GITHUB_TOKEN` in `.env`, and joins them to sprite runs through
`run.json.github_run_id`. The workflow passes the id as
`SERMON_SHORTS_GITHUB_RUN=${{ github.run_id }}`. A GitHub run with no
sprite run shows as "failed before reaching the sprite".

### Audit

| Method | Path | Returns / does |
|---|---|---|
| GET | `/audit?service=&action=&since=&limit=` | newest first |
| GET | `/audit.csv?...` | same, as CSV (the History screen's Export button) |
| GET | `/audit/verify` | `{"ok": true, "events": 214, "tail": "77be..."}` or the first broken `seq` |

## CLI

Everything that works today keeps working, with the same flags and the same
output lines (`scripts/weekly_cron.sh` parses `Latest service` and
`clip(s) rendered`).

`[SERVICE]` below accepts a service id, a clips folder path, or nothing,
which picks the newest. Today's `publish` already behaves this way.

| Command | Does |
|---|---|
| `sermon-shorts <video> ...`, `--weekly`, `--sermon-only`, `--from-manifest` | unchanged, but each now writes a run record, events, `service.json`, `candidates.json` and fingerprints |
| `sermon-shorts services` | list services with clip/approval/post counts and "needs you" |
| `sermon-shorts show [SERVICE]` | one service: clips, decisions, posts, needs-you items |
| `sermon-shorts approve [SERVICE] N... [--note TEXT]` | record approvals for the clips' current fingerprints |
| `sermon-shorts reject [SERVICE] N --note TEXT` | record a rejection |
| `sermon-shorts publish [SERVICE] N... [--to ...] [--dry-run] [--yes]` | as today, plus rule 1: unapproved or stale clips are listed as skipped. `--approve` records the approvals in the same step, for the one-person case, and logs them as such |
| `sermon-shorts mark-posted [SERVICE] N instagram --url URL` | record a manual post |
| `sermon-shorts retry-cover [SERVICE] N` | retry the YouTube cover |
| `sermon-shorts runs` / `runs show ID` / `runs log ID` | run history and logs |
| `sermon-shorts audit [--service S] [--csv] [--verify]` | read, export or verify the log |
| `sermon-shorts users add LOGIN --name NAME --role ROLE` / `users token LOGIN` / `users revoke TOKEN_ID` | manage people and tokens; prints a one-time login link |
| `sermon-shorts serve [--port 8000]` | run the API (imports FastAPI/uvicorn lazily, as other optional deps are) |

Every read command takes `--json`, which prints exactly the API's response
body. The UI and scripts can then rely on one schema, and the CLI doubles as
a test harness for the API.

The publish change is the one behaviour change: `publish 1 2` on a clip
nobody approved now skips it and says why. On the sprite that becomes
`publish 1 2 --yes --approve`, or an `approve` step before it.

## Progress events

The pipeline's `print()` calls become `progress.emit(step, message, **data)`.
The default sink prints the same text as today. When a run record is active,
a second sink appends to `events.jsonl`:

```json
{"t": "2026-10-24T13:52:18Z", "step": "transcribe", "state": "done",
 "message": "transcript cached ... (433 segments)", "data": {"segments": 433, "seconds": 790}}
```

Steps: `fetch`, `transcribe`, `find_sermon`, `trim`, `select`, `review`,
`render` (one per clip), `done`. The This week screen's step list and
durations come straight from these.

## Running it on the sprite

- `scripts/sprite_setup.sh` creates a second sprite service, `api`
  (`.venv/bin/python -m sermon_shorts serve --port 8000`). It changes the
  `clips` Caddy service to a small Caddyfile: `/api/*` goes to `:8000`,
  everything else goes to the file server under `/files`.
- Runs started from the API go through `scripts/sprite_weekly.sh`, so they
  get the same task-hold heartbeat, `git pull`, `HF_HUB_OFFLINE=1` and log
  file as the cron path. The script gains a `RUN_ID` env var for the run
  record. Publish jobs take the same task hold while they run.
- To verify: the sprite pauses when idle. A request to the sprite URL
  appears to wake it, which the review file server already relies on, but
  that is worth confirming for the API service before the UI depends on it.

## Build order

1. **Record what's missing (CLI only, no server).** `core/store.py` and
   `core/audit.py`, then `service.json`, `candidates.json`, fingerprints,
   run records and progress events. Backfill the Oct 18 folder: its
   approvals are logged as `clip.approved` with `"backfilled": true` and the
   publish time from `published.json`, because the real approval time was
   never recorded.
2. **Decisions and the approval gate.** `approve`, `reject`, `show`,
   `services`, `mark-posted`, `retry-cover`, `audit`, `users`, and rule 1 in
   `publish`. After this step the CLI alone is auditable.
3. **HTTP API.** `serve`, tokens, the Caddy proxy, GitHub run merging,
   the event stream.
4. **UI.** Wire the Studio screens to `/api/v1`.

Each step is shippable by itself and is checked the way the repo checks
things now: a `--whisper-model tiny` run, `--from-manifest --only N`, and
`publish --dry-run`. Step 1 is also the right time to add the first tests
(`core/` is pure file logic and easy to test without video).

## Open questions

1. **Who besides you approves or publishes?** This decides whether the roles
   and `require_two_people` are needed now or can wait.
2. **Should posting from the CLI without approval stay possible** (via
   `--approve`), or must every post go through a separate approve step?
3. **Is a tamper-evident log enough**, or do you want each day's tail hash
   pushed somewhere outside the sprite (e.g. a commit to a private repo)?
4. **Description edits in the browser:** in scope? They would change the
   fingerprint and need a re-review, which the design already handles. The
   UI just doesn't offer it yet.
