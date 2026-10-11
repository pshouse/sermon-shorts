#!/bin/bash
# Weekly run on the Fly.io sprite (started by .github/workflows/weekly.yml).
#
# A sprite pauses when it looks idle, and a task hold lasts at most an hour,
# while a medium-model run can take longer — so a background heartbeat
# renews a short hold every minute for as long as this script lives. If the
# script dies, the hold simply expires and the sprite can sleep again.
#
# Extra arguments are passed through to `python -m sermon_shorts --weekly`
# (e.g. --labeler jev, --force).
set -uo pipefail
DIR="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="$HOME/logs"
LOG="$LOG_DIR/weekly-$(date +%F-%H%M).log"
mkdir -p "$LOG_DIR" "$HOME/Downloads"
cd "$DIR"

hold() { sprite-env curl -s -X PUT /v1/tasks -d '{"name":"weekly","expire":"5m"}' >/dev/null; }
hold
( while sleep 60; do hold; done ) &
HEARTBEAT=$!
trap 'kill $HEARTBEAT 2>/dev/null; sprite-env curl -s -X DELETE /v1/tasks/weekly >/dev/null' EXIT

# Pick up any pushed code changes before running.
git pull -q --ff-only || echo "warning: git pull failed; running the checked-out code"
.venv/bin/pip install -q -r requirements.txt

# The whisper model is already in the HF cache; going offline skips the Hub
# lookup, which the sprite's shared egress IP gets rate-limited on.
HF_HUB_OFFLINE=1 .venv/bin/python -u -m sermon_shorts --weekly --whisper-model medium "$@" \
    2>&1 | tee "$LOG"
status=$?
echo "=== exit $status ===" | tee -a "$LOG"
exit $status
