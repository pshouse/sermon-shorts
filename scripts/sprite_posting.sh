#!/bin/bash
# Posts the queued clips whose day has come, on the Fly.io sprite (started by
# .github/workflows/posting.yml; see scripts/sprite_weekly.sh for the shape).
#
# A sprite pauses when it looks idle, and Meta can take minutes to process a
# video, so a heartbeat renews a short task hold for as long as this runs.
set -uo pipefail
DIR="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="$HOME/logs"
LOG="$LOG_DIR/posting-$(date +%F-%H%M).log"
mkdir -p "$LOG_DIR"
cd "$DIR"

hold() { sprite-env curl -s -X PUT /v1/tasks -d '{"name":"posting","expire":"5m"}' >/dev/null; }
hold
( while sleep 60; do hold; done ) &
HEARTBEAT=$!
trap 'kill $HEARTBEAT 2>/dev/null; sprite-env curl -s -X DELETE /v1/tasks/posting >/dev/null' EXIT

# Pick up any pushed code changes first, as the weekly run does.
git pull -q --ff-only || echo "warning: git pull failed; running the checked-out code"
.venv/bin/pip install -q -r requirements.txt

.venv/bin/python -m sermon_shorts --version | tee "$LOG"
.venv/bin/python -u -m sermon_shorts publish --due 2>&1 | tee -a "$LOG"
status=$?
echo "=== exit $status ===" | tee -a "$LOG"
exit $status
