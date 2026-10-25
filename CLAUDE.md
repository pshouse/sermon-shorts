# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Python CLI that turns a full church service recording into vertical (1080x1920), captioned social clips, and optionally posts reviewed clips to YouTube Shorts / Instagram Reels / Facebook Reels. Everything runs locally except the Claude API call (text transcript only) and the publish/Subsplash network calls. README.md is the user-facing manual and documents every flag and the one-time YouTube/Meta setup.

## Commands

```
source .venv/bin/activate
pip install -r requirements.txt

python -m sermon_shorts <video.mp4> --clips 3            # full clip pipeline
python -m sermon_shorts <video.mp4> --sermon-only        # trim service to just the sermon
python -m sermon_shorts --weekly                         # Subsplash fetch → sermon trim → clips (idempotent)
python -m sermon_shorts <video.mp4> --from-manifest --only 2   # re-render one clip from clips.json, no Claude call
python -m sermon_shorts publish [<clips folder>] 1 3 --dry-run # preview what would be posted
```

There is no linter or build step. `python -m unittest discover tests` runs the unit tests (standard library only; they cover `sermon_shorts/core/`). To check a pipeline change, run the pipeline on a real recording: `--whisper-model tiny` makes a quick transcription pass, and once a transcript is cached, `--from-manifest --only N` re-renders a single clip (reframe, captions, audio, thumbnail) without spending a Claude call. Use `publish --dry-run` when touching `publish.py`. Without `--dry-run`, `publish` posts **publicly and immediately**, so never run it to test.

## Architecture

`cli.main()` orchestrates a linear pipeline across modules; each module owns one stage:

1. **`transcribe.py`**: faster-whisper with word-level timestamps, cached as `<video>.transcript-<model>.json` next to the video. Also owns all transcript-time logic: `snap_to_sentences` (moves Claude's rough bounds to sentence boundaries derived from word punctuation, with bounded drift and a fallback to segment boundaries) and `cut_windows` (the range between neighbouring words where a cut may land).
2. **`highlights.py`**: Claude structured-output calls into Pydantic models, all going through `_parse_call`. Each call is streamed on Fable 5.1 (`MODEL`/`EFFORT`) with the server-side refusal fallback. `find_sermon` returns a `SermonWindow`. `select_highlights` asks for twice the clips needed, then `review_clips` makes a second call that shows each candidate exactly as it will be cut, with 60s of text before and 45s after, and keeps, adjusts or rejects it. The selection prompt defines a clip as one whole idea: setup, development, landing. The prompts live here, and so do the church-profile context and footer injection and the punctuation normalization of descriptions.
   **Cuts are placed on quoted words, not on timestamps.** Claude returns each clip's verbatim opening words (`hook`) and last words (`ending`). `transcribe.locate_quote` finds where they are spoken, and the cut goes there. Sentence snapping (`snap_to_sentences`) is only a fallback, because whisper often drops periods for a run-on speaker, and snapping then reaches into the neighbouring thought.
   **`jev.py`** is the experimental alternative to `find_sermon`, selected with `--labeler jev`. TypeSafe's Jev model labels ~45s transcript windows by service part, and the sermon is the longest run of sermon windows, with short gaps bridged. Its key is `JEV_API_KEY`, not the SDK's default `TYPESAFE_API_KEY`. Each labeler's raw window is recorded in `<video>.sermon-windows.json` so the two can be compared.
3. **`render.py`**: the ffmpeg-based stages, using the binary bundled by `imageio-ffmpeg` (no system ffmpeg): `quietest_point` refines each cut inside the `cut_windows` range from the audio itself, `render_clip` runs the two-pass loudness chain plus crop, captions and denoise/sharpen, and `trim_video` handles `--sermon-only`. Audio/video tuning constants sit at the top of the file.
4. **`reframe.py`**: YuNet face detection (bundled ONNX in `sermon_shorts/models/`, needs OpenCV 4.x). `track_speaker` samples faces and `choose_speaker` picks the speaker by on-screen persistence, not face size. `build_pan_keyframes` produces hold-then-pan keyframes and `crop_filter` turns them into an ffmpeg crop expression. `SpeakerTrack.face_band` feeds caption auto-positioning.
5. **`captions.py`**: writes an ASS subtitle file (2–3 word pop chunks). `render.py` references it by bare filename with ffmpeg's cwd set to the temp dir, which avoids Windows path escaping in the subtitles filter.
6. **`thumbnail.py`**: picks a frame from the speaker track and renders a caption-free cover `.jpg` with the title.
7. **`subsplash.py`**: anonymous guest-token fetch of the newest recording from a church's public Subsplash feed (`--latest` / `--weekly`).
8. **`publish.py`**: a separate subcommand, dispatched from the top of `cli.main()` when `argv[0] == "publish"` so it can never ride along with a render run. It posts only what `core/posts.plan()` allows: clips whose latest decision (`core/review.py`) approves their current fingerprint, never twice, Instagram only up to `IG_MAX_SECONDS`. It needs a named person (`--as`/`SERMON_SHORTS_USER`, approver or above) except for `--dry-run`, and records every post in the clips folder's `published.json` and the `posts` table. `approve`/`reject`/`note`/`mark-posted`/`retry-cover` live in `review_cli.py`.

Key cross-cutting facts:
- **All clip times are seconds in the source video passed to that run.** `--sermon-only` writes a re-based transcript cache for the trimmed `_sermon.mp4` (`save_shifted_transcript`) so the next clips run skips transcription. `--weekly` simply recurses into `main()` twice (sermon trim, then clips on the sermon file).
- **Outputs** go in `<video>_clips/`: `NN_<slug>.mp4`, a `.txt` with title and description, a `.jpg` cover, `clips.json` (the manifest that `--from-manifest` and `publish` read), and `published.json`.
- **`--weekly` idempotence** is decided by an existing `clips.json`, including any `*<service date>*_clips/` folder, because Subsplash can swap the video file after the fact.
- **Config and secrets** all sit in the project root and are gitignored: `.env` (`ANTHROPIC_API_KEY`, `META_*`; `publish --connect meta` writes into it), `church.json` (church_name, speaker, footer, subsplash), `youtube_client_secret.json`, `.youtube_token.json`. `church.json` is looked up in the cwd first, then the project root.
- Heavy or optional dependencies (faster_whisper, anthropic, httpx, Google client) are imported inside functions so that unrelated modes stay fast and still work when an optional dependency is missing.
- The Claude model ID is the `MODEL` constant in `highlights.py`. `--from-manifest` reuses the saved cut times exactly: it does no re-snapping and no quote lookup.
- **Accounts and audit** (`sermon_shorts/core/`, standard library only): SQLite store `sermon_shorts.db` in the project root (gitignored, `SERMON_SHORTS_DB` overrides; never under `~/Downloads`, which the sprite's file server publishes). Roles escalate reviewer < approver < owner via the `PERMISSIONS` table in `users.py`; `users init` creates the undeletable root owner once, then owners invite. Every state change writes its `audit.record()` event inside the same `db.write()` transaction; the audit table is append-only (triggers) and hash-chained (`audit --verify`). Secrets are stored as SHA-256 and never logged. `users`/`audit`/`services`/`runs` are dispatched in `__main__.py` before the pipeline imports. `cli.main()` wraps `_pipeline()` in a run (`core/runs.py`: one at a time, nested `--weekly` calls join it, dead runs reaped); `_pipeline` records steps with `run.step()` and mirrors services, candidates (`select_highlights(report=...)`) and fingerprinted clips into the database (`core/records.py`) next to the unchanged clips.json. **`api.py`** (`serve`, FastAPI, the only module importing it) is a thin HTTP layer over `core/`: bearer or cookie sign-in (cookie writes need `X-Requested-With: sermon-shorts`), decisions checked against the fingerprint the client watched, publishing as preview (`posts.plan_id`) then post (re-planned, 409 if changed) in a thread via `publish.post_jobs`, runs spawned through `sprite_weekly.sh` with `SERMON_SHORTS_RUN_ID`/`SERMON_SHORTS_USER` and cancelled with SIGTERM. `scripts/Caddyfile` routes `/api/*` to it on the sprite. **`web/`** is the Studio app it serves at `/app/`: plain JS with no build step, every value inserted with `h()` (textContent, never innerHTML) under a strict CSP (`APP_CSP`), so keep it that way. `serve --files` serves the data root too for trying it without Caddy. `docs/api-design.md` is the plan and records where the build differs.
- `scripts/weekly_cron.sh` is the macOS launchd entry point: it polls Subsplash on Sunday afternoons, logs to `~/Library/Logs/sermon-shorts.log`, sends a notification, and shuts down an idle Mac. It parses the CLI's `Latest service` and `clip(s) rendered` output lines, so keep those strings stable.

## Cloud (Fly.io sprite)

The weekly run and `publish` also run on a Fly.io sprite named `sermon-shorts` (Ubuntu, a checkout at `/home/sprite/sermon-shorts`, outputs in `/home/sprite/Downloads`).
- **Trigger:** sprites have no scheduler, so `.github/workflows/weekly.yml` wakes it on Sunday afternoons and stays attached while `scripts/sprite_weekly.sh` runs there. The workflow needs the `SPRITES_TOKEN` repository secret.
- **The run script:** it renews a 5-minute task hold every minute because a hold lasts at most an hour. It runs `git pull` before every job, so pushing to `main` deploys. It sets `HF_HUB_OFFLINE=1` because the sprite's shared IP gets rate-limited by the Hugging Face Hub, and the medium model is pre-cached in `~/.cache/huggingface`. Logs go to `~/logs/`.
- **Provisioning:** `scripts/sprite_setup.sh` installs everything the sprite needs and is safe to re-run: apt libraries for OpenCV, Arial Black, the venv and the pre-cached model. Without the font, libass silently renders captions and cover titles as nothing.
- **Review:** a sprite service named `clips` runs Caddy on port 8080 with `scripts/Caddyfile`: `/api/*` and `/app*` go to the `api` service (`serve`, localhost:8000), everything else is the read-only file server over `~/Downloads`. It is reached through the sprite URL, which requires org auth. Caddy was chosen because it supports byte-range requests, which iOS needs to play MP4s.
- **Secrets:** `.env`, `church.json` and the YouTube files are copied in with `sprite file push`; they are not in git.
- **Commands:** `sprite exec -s sermon-shorts --no-stdin -- bash -lc '<cmd>'` runs things there, and `sprite file pull` fetches outputs.

## Conventions

- The code comments explain *why* a tuning value exists, often citing the real service or measurement that motivated it (e.g. `IG_MAX_SECONDS`, `_STYLE_PROMPT`). Keep that style when changing heuristics.
- Commit messages are a single imperative sentence describing the behavioural outcome (see `git log`).
- **Versions** are semver in `sermon_shorts/__init__.py` (MAJOR breaks commands, API clients or stored data; MINOR adds features; PATCH fixes). Add each change under `## [Unreleased]` in `CHANGELOG.md`; to release, rename that section to the new version with the date, bump `__version__` to match (`tests/test_version.py` fails if they disagree), and tag `vX.Y.Z` on `main` after merging. `core/version.py` adds the git commit: every run records both (`runs.code_version`/`code_commit`), and the API's `/health` reports when the running process is behind Python code on disk so the Studio can tell owners to restart the `api` service.
- Descriptions and titles must stay plain keyboard punctuation (no em dashes or smart quotes) because they get pasted into upload forms.
