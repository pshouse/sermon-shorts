#!/bin/bash
# Scheduled entry point for the weekly sermon-shorts run (called by launchd).
#
# Runs `python -m sermon_shorts --weekly`, which fetches the newest service
# from Subsplash, trims it to the sermon, and cuts clips — and exits quietly
# if that service's clips already exist. caffeinate keeps the Mac awake for
# the duration. Output goes to ~/Library/Logs/sermon-shorts.log, and a
# notification pops when new clips are ready (or when the run fails).
set -u
DIR="$(cd "$(dirname "$0")/.." && pwd)"
LOG="$HOME/Library/Logs/sermon-shorts.log"
mkdir -p "$(dirname "$LOG")"

cd "$DIR"
echo "=== weekly run started $(date) ===" >> "$LOG"
/usr/bin/caffeinate -i "$DIR/.venv/bin/python" -m sermon_shorts --weekly \
    --whisper-model medium >> "$LOG" 2>&1
status=$?
echo "=== weekly run finished $(date) (exit $status) ===" >> "$LOG"

if [ $status -ne 0 ]; then
    /usr/bin/osascript -e 'display notification "See ~/Library/Logs/sermon-shorts.log" with title "Sermon Shorts failed"' || true
elif tail -50 "$LOG" | grep -q "clip(s) rendered"; then
    /usr/bin/osascript -e 'display notification "This week'"'"'s clips are ready in Downloads" with title "Sermon Shorts"' || true
fi
exit $status
