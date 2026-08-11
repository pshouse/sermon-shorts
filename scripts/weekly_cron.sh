#!/bin/bash
# Scheduled entry point for the weekly sermon-shorts run (called by launchd).
#
# On Sunday afternoons this does the full unattended cycle: keep checking
# Subsplash until today's service appears (the upload usually lands late
# morning), process it, then shut the iMac back down so it isn't left
# running. Any other trigger (the login catch-up during the week) runs once
# and leaves the machine alone.
#
# The shutdown is guarded twice: it only fires when the Mac has gone 15+
# minutes without keyboard/mouse input (i.e. it woke itself up and nobody is
# here), and macOS itself will hold it if an app has unsaved work.
set -u
DIR="$(cd "$(dirname "$0")/.." && pwd)"
PY="$DIR/.venv/bin/python"
LOG="$HOME/Library/Logs/sermon-shorts.log"
mkdir -p "$(dirname "$LOG")"
cd "$DIR"

TODAY=$(date +%F)
notify() { /usr/bin/osascript -e "display notification \"$2\" with title \"$1\"" || true; }
sunday_afternoon() { [ "$(date +%u)" = 7 ] && [ "$(date +%H)" -ge 12 ] && [ "$(date +%H)" -lt 20 ]; }

echo "=== weekly run started $(date) ===" >> "$LOG"
while :; do
    /usr/bin/caffeinate -i "$PY" -m sermon_shorts --weekly --whisper-model medium >> "$LOG" 2>&1
    status=$?
    latest=$(tail -60 "$LOG" | grep 'Latest service' | tail -1 \
             | grep -oE '2[0-9]{3}-[0-9]{2}-[0-9]{2}' || true)
    # Keep polling only when we're still waiting on today's upload: Sunday
    # afternoon, no error, feed still showing a previous week. Give up at 3pm
    # and assume there's no recording this week.
    if [ $status -ne 0 ] || [ "$latest" = "$TODAY" ] || ! sunday_afternoon \
       || [ "$(date +%H)" -ge 15 ]; then
        break
    fi
    echo "  today's service not up yet — checking again in 15 min" >> "$LOG"
    sleep 900
done
echo "=== weekly run finished $(date) (exit $status) ===" >> "$LOG"

if [ $status -ne 0 ]; then
    notify "Sermon Shorts failed" "See ~/Library/Logs/sermon-shorts.log"
elif tail -50 "$LOG" | grep -q "clip(s) rendered"; then
    notify "Sermon Shorts" "This week's clips are ready in Downloads"
fi

# Unattended-Sunday shutdown. HIDIdleTime is seconds since the last physical
# keyboard/mouse input; a self-woken Mac that nobody touched has been "idle"
# since boot, while a Mac someone is using never gets past a few minutes.
idle=$(ioreg -c IOHIDSystem | awk '/HIDIdleTime/ {print int($NF/1000000000); exit}')
if sunday_afternoon && [ "${idle:-0}" -gt 900 ]; then
    echo "unattended Sunday run complete — shutting down (idle ${idle}s)" >> "$LOG"
    /usr/bin/osascript -e 'tell application "System Events" to shut down' || true
fi
exit $status
