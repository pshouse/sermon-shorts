# Changelog

All notable changes, newest first. Versions follow [Semantic Versioning](https://semver.org/):
MAJOR for changes that break existing commands, API clients or stored data,
MINOR for new features, PATCH for fixes. The version lives in
`sermon_shorts/__init__.py`; `sermon-shorts --version`, the API's
`/api/v1/health` and the Studio's footer show it with the git commit.

## [Unreleased]

## [0.3.1] - 2026-10-26

### Fixed
- The Sunday checks no longer end an hour early after daylight saving time ends:
  the weekly workflow now runs every half hour from 16:00 to 20:30 UTC, which
  covers 12:00-15:30 US Eastern all year.

## [0.3.0] - 2026-10-26

### Added
- The Studio has a favicon.
- A clip that has to be posted to Instagram by hand shows its caption (title and
  description, as the API would post it) with a Copy caption button, and a
  Download cover button next to Download video.

### Changed
- Posting shows its progress: while it runs, the Publish page shows a panel
  with each post's stage (uploading, with a percentage on YouTube;
  processing, with Meta's percentage and the time waited; setting the
  cover), a timer, and an overall "2 of 5" bar, and the clip cards show the
  same live. The clip picker is hidden until posting finishes; a summary
  stays until dismissed, and leaving and coming back picks the progress up
  again. Uploaders' warnings (such as a refused cover) show in the Studio
  instead of only in the server's output. `publish` on the command line
  prints the same stages.
- API: posting jobs report each post's progress under `items` (the `log`
  field is gone), and `GET /api/v1/publish-jobs?service=` lists recent ones.
- The Studio's "Restart needed" notice gives the exact command to copy, with
  the dependency install step when `requirements.txt` changed too.

## [0.2.0] - 2026-10-25

Everything since the first release, which kept the 0.1.0 number until
versions were tracked.

### Added
- **The Studio** (`/app/`): a review web app for watching, approving and
  posting clips, with live run progress, the audit log and people
  management. Approved clips offer only "Withdraw approval", with a warning
  when the clip is already posted.
- **HTTP API** (`sermon-shorts serve`) behind the Studio: token or cookie
  sign-in, fingerprint-checked decisions, preview-then-post publishing,
  starting and cancelling runs, CSV export of the audit log.
- **Accounts and roles**: a root owner created once, then invite-only
  reviewers, approvers and owners (`sermon-shorts users`).
- **Approval gate**: `publish` posts only clips a named approver approved
  exactly as they are on disk; `approve`, `reject`, `note`, `mark-posted`
  and `retry-cover` commands.
- **Audit log**: append-only and hash-chained, with `sermon-shorts audit
  --verify`; every run, decision, post and account change is recorded.
- **Run records**: each run's steps, timings, starter and outcome; every
  candidate clip with Claude's verdict and note; `services` and `runs`
  commands.
- **Schedule**: the Studio's Schedule page and `sermon-shorts schedule`
  show the coming Sunday checks and recent scheduled runs; owners can pause
  all scheduled runs or skip specific dates (Run now and manual runs still
  go ahead). Days and times stay in `.github/workflows/weekly.yml`.
- **Version tracking**: this changelog, the version and commit recorded on
  every run, and a Studio notice when the server needs a restart to load
  newer code.
- **Publishing** to YouTube Shorts, Instagram Reels and Facebook Reels
  (`publish`), with a ledger so nothing posts twice.
- **Fly.io sprite** hosting: the weekly run started by a GitHub Actions
  schedule, and the clips served for review.
- **Subsplash**: `--latest` fetches the newest service; `--weekly` runs the
  whole Sunday routine and skips services already done.
- **Jev** as an alternative sermon finder (`--labeler jev`).
- Designed cover thumbnails, a re-based transcript for `--sermon-only`,
  automatic caption placement from the speaker's face.

### Changed
- Clips are cut on whole ideas: quote-anchored cuts and a second,
  stranger's-eye review of each candidate, on Fable 5.1.
- Speaker tracking with YuNet, cuts on real pauses, two-pass audio leveling.
- Clip bounds snap to sentence boundaries, with bounded drift.

### Fixed
- Sprite setup no longer reports Arial Black missing when it is installed.
- An API request could fail when its database connection was closed on a
  different thread.
- One unpunctuated stretch of transcript no longer derails the sermon cut.
- A service counts as done by its date, so a re-uploaded video isn't
  processed twice.

## [0.1.0] - 2026-07-12

### Added
- First release: a church service recording in, vertical (1080x1920)
  captioned clips out, with the moments chosen by Claude; `--sermon-only`
  trimming, a church profile, and `--speaker`.

[Unreleased]: https://github.com/pshouse/sermon-shorts/compare/v0.3.1...HEAD
[0.3.1]: https://github.com/pshouse/sermon-shorts/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/pshouse/sermon-shorts/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/pshouse/sermon-shorts/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/pshouse/sermon-shorts/releases/tag/v0.1.0
