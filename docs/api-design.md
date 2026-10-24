# API and CLI layer: design

Status: proposal (2026-10-24). Built so far: step 0, accounts, roles and the audit log
(`sermon_shorts/core/`, `sermon-shorts users`, `sermon-shorts audit`).

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
- **SQLite for records, files for media.** People, tokens, invites,
  decisions, posts, runs and the audit log live in one SQLite database
  (`sermon_shorts.db`, standard library `sqlite3`, WAL mode so the API reads
  while the CLI writes). A change and its audit event commit in the same
  transaction. Videos, covers and transcripts stay as files next to the
  recording, as now. The database sits in the project root next to `.env`,
  not in `~/Downloads`, because the `clips` file server publishes
  `~/Downloads`.
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
  runs/<run id>.log     NEW  the human log (what weekly-*.log is today)

<project root>/sermon_shorts.db   (gitignored; SERMON_SHORTS_DB overrides)
  users, tokens, invites           BUILT  (step 0, below)
  audit                            BUILT  append-only, hash-chained
  services                         NEW  Subsplash metadata, sermon window, summary
  candidates                       NEW  every candidate: verdict, scores, note, outcome
  clips                            NEW  mirrors clips.json + fingerprint
  decisions                        NEW  approve/reject with fingerprint and note
  posts                            NEW  mirrors published.json + who, manual, cover state
  runs, run_events                 NEW  kind, args, starter, status, step timings
```

`clips.json` and `published.json` keep being written exactly as now, because
`--from-manifest`, `publish` and `--weekly` idempotence read them. The
database rows are written alongside them.

### Clip fingerprint

`fingerprint = sha256(mp4 bytes) + sha256(jpg bytes) + sha256(title + "\n" + description)`,
stored as one combined hex digest in `clips.json` at render time.

An approval names the fingerprint it covers. A `--from-manifest` re-render
or a description edit changes the fingerprint, so the old approval stops
counting and the clip shows as "Waiting for review" again. This is the rule
the Review screen states: "Re-rendering the clip clears it."

### Candidates

`review_clips()` returns every review, not just the kept clips, and the
caller writes them to the `candidates` table (shown here as JSON):

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

The `audit` table (`sermon_shorts/core/audit.py`, built):

| seq | at | actor | via | action | service | clip | data | prev | hash |
|---|---|---|---|---|---|---|---|---|---|
| 41 | 2026-10-24T14:14:03Z | pshouse | web | clip.approved | Live_2026-10-18_7am | 01_be-ready-...mp4 | `{"fingerprint":"9f2c...","note":""}` | c0a1... | 77be... |

- Three layers of protection. Triggers make the table refuse `UPDATE` and
  `DELETE`. Each row's `hash` covers the previous row's hash, so a row
  changed after dropping the triggers breaks the chain from there on. And
  `AUTOINCREMENT`'s high-water mark shows rows cut off the end.
  `sermon-shorts audit --verify` checks all three and prints the tail hash.
- This makes tampering evident, not impossible: someone with shell access
  could rebuild the whole chain. The weekly run prints the tail hash into
  the GitHub Actions log, which keeps an outside copy to check against.
- Writes use `BEGIN IMMEDIATE`, so two writers can't both append after the
  same tail, and the state change commits together with its event.
- Actions: `user.root_created`, `invite.created`, `invite.revoked`,
  `user.joined`, `user.role_changed`, `user.removed`, `token.created`,
  `token.revoked` (built); `run.started`, `run.finished`, `run.failed`,
  `sermon.found`, `clip.rendered`, `clip.noted`, `clip.approved`,
  `clip.rejected`, `publish.planned`, `post.created`, `post.failed`,
  `post.skipped`, `post.marked_manual`, `cover.failed`, `cover.set` (to come).
- Secrets never enter the log: events name token and invite *ids*, never
  the token or code.

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
3. **No double posts.** A unique `(clip, platform)` key on the `posts` table,
   checked in the same transaction as the write (and `published.json` still
   updated for the existing CLI).
4. **One render run at a time.** A partial unique index on `runs` where
   `status = 'running'`; a second start returns `409` with the active run's id. (The GitHub workflow's
   concurrency group already does this for scheduled runs.)
5. **Rejections need a note.** Approvals may have one.
6. **Instagram over `IG_MAX_SECONDS`** is planned as `skipped: manual_required`
   and appears in "Needs you" until someone records a manual post.

## Identity and roles

Built in `sermon_shorts/core/users.py` (step 0).

The sprite URL already needs Fly org auth, but that tells the app nothing
about *which* person is calling, so every person has an account with one
role. Roles escalate, and each includes everything below it:

| Role | Can |
|---|---|
| `reviewer` | see everything (services, clips, runs, audit log), leave review notes |
| `approver` | + approve/reject clips, publish, record manual posts, retry covers |
| `owner` | + start/cancel runs, invite people, change roles, remove people, manage anyone's tokens |

The table is `PERMISSIONS` in `users.py`. The CLI and the API both check
against it.

**Bootstrapping.** An admin with shell access on the sprite runs
`sermon-shorts users init --login L --name N` once. It creates the **root
owner** and that person's first API token, and only works on a database that
has never had a user, so it can't later be used to add a second owner by the
back door. The audit event is attributed to `admin:<shell user>`.

**Invites.** From then on people join only by invite, and only owners
invite. An owner can invite at any role, including owner.
`users invite approver --note "Sarah, comms"` prints a one-time code that
works for 7 days (1-30 with `--days`). Redeeming it (`users accept CODE
--login --name`, later a web page) creates the account with the invite's
role and its first token. Codes can be withdrawn while unused.

**Changing people.** Owners promote and demote with `users role LOGIN ROLE`
and remove with `users remove LOGIN`. Removing someone revokes their tokens
but keeps their row, so audit entries still name a real person, and their
login can't be reused. The **root account is always an owner and can't be
demoted or removed**, by anyone, including other owners. That also means
the service can never lose its last owner.

**Tokens.** `ssk_...` bearer tokens, shown once, stored as SHA-256. Anyone
can issue or revoke their own tokens; owners can do it for anyone.

- The API takes `Authorization: Bearer <token>`. The browser UI will keep it
  in an HttpOnly cookie set by a one-time login link.
- The CLI acts as `SERMON_SHORTS_USER`, or `--as <login>`. Commands that
  change something refuse to run without one. Shell access is already full
  trust, so the CLI does not ask for a token; it records `via = "cli"` so
  the log shows the difference.
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
| GET | `/services/{sid}/candidates` | the service's candidates |
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
| GET | `/runs/{id}` | the run: status, steps with durations, outcome |
| GET | `/runs/{id}/events` | the run's progress events; with `Accept: text/event-stream`, a live stream for the progress view |
| GET | `/runs/{id}/log` | `log.txt` as text |
| POST | `/runs/{id}/cancel` | SIGTERM to the process group, `run.failed` with `reason: cancelled` |

**GitHub runs.** The Oct 18 failures never reached the sprite (the token
was missing), so the sprite has no record of them. `GET /runs` therefore
also reads `weekly.yml` runs from the GitHub API, using a read-only
`GITHUB_TOKEN` in `.env`, and joins them to sprite runs through
the run's `github_run_id`. The workflow passes the id as
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
| `sermon-shorts <video> ...`, `--weekly`, `--sermon-only`, `--from-manifest` | unchanged, but each now records the run, its events, the service, its candidates and clip fingerprints |
| `sermon-shorts services` | list services with clip/approval/post counts and "needs you" |
| `sermon-shorts show [SERVICE]` | one service: clips, decisions, posts, needs-you items |
| `sermon-shorts approve [SERVICE] N... [--note TEXT]` | record approvals for the clips' current fingerprints |
| `sermon-shorts reject [SERVICE] N --note TEXT` | record a rejection |
| `sermon-shorts publish [SERVICE] N... [--to ...] [--dry-run] [--yes]` | as today, plus rule 1: unapproved or stale clips are listed as skipped. `--approve` records the approvals in the same step, for the one-person case, and logs them as such |
| `sermon-shorts mark-posted [SERVICE] N instagram --url URL` | record a manual post |
| `sermon-shorts retry-cover [SERVICE] N` | retry the YouTube cover |
| `sermon-shorts runs` / `runs show ID` / `runs log ID` | run history and logs |
| `sermon-shorts audit [--service S] [--csv] [--verify]` | read, export or verify the log |
| `sermon-shorts users init / invite / invites / revoke-invite / accept / list / role / remove / token / tokens / revoke-token` | people, roles and tokens (built) |
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
a second sink writes rows to `run_events`:

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

0. **Accounts and the audit log (built).** SQLite store, root owner,
   invites, reviewer/approver/owner, tokens, hash-chained audit, tests in
   `tests/` (`python -m unittest discover tests`).
1. **Record what's missing (CLI only, no server).** Services, candidates,
   fingerprints, run records and progress events. Backfill the Oct 18 folder: its
   approvals are logged as `clip.approved` with `"backfilled": true` and the
   publish time from `published.json`, because the real approval time was
   never recorded.
2. **Decisions and the approval gate.** `approve`, `reject`, `show`,
   `services`, `mark-posted`, `retry-cover`, and rule 1 in
   `publish`. After this step the CLI alone is auditable.
3. **HTTP API.** `serve`, tokens, the Caddy proxy, GitHub run merging,
   the event stream.
4. **UI.** Wire the Studio screens to `/api/v1`.

Each step is shippable by itself and is checked the way the repo checks
things now: a `--whisper-model tiny` run, `--from-manifest --only N`, and
`publish --dry-run`. The `core/` layer gets unit tests in `tests/`
(standard library `unittest`, no video needed), as step 0 did.

## Open questions

1. **Should approvers publish, or only owners?** The current table lets
   approvers publish; moving `publish` to owner is a one-line change in
   `PERMISSIONS`. Related: is `require_two_people` (approver and publisher
   must differ) wanted now, or later?
2. **Should posting from the CLI without approval stay possible** (via
   `--approve`), or must every post go through a separate approve step?
3. **Is a tamper-evident log enough**, or do you want each day's tail hash
   pushed somewhere outside the sprite (e.g. a commit to a private repo)?
4. **Description edits in the browser:** in scope? They would change the
   fingerprint and need a re-review, which the design already handles. The
   UI just doesn't offer it yet.
