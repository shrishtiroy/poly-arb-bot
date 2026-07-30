#!/bin/zsh
# Daily live collection, driven by launchd (not cron: cron does not fire while
# the Mac is asleep, which is why collection silently stopped after 2026-07-28).
#
# Responsibilities beyond just invoking the collector:
#   - single-instance lock, so an overrunning previous collect cannot race the
#     next one into "database is locked"
#   - caffeinate, so the run is not suspended mid-collection
#   - log rotation, so logs cannot grow to hundreds of MB again
set -u

PROJECT=/Users/shrishtiroy/Documents/projects/poly-arb-bot
DATA=/Users/shrishtiroy/polyarb-data
LOG=$DATA/collect.log
LOCK=$DATA/collect.lock
HOURS=${POLYARB_HOURS:-23}   # < 24 so the run always ends before the next start

cd "$PROJECT" || exit 1

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') [wrapper] $1" >> "$LOG"; }

# Rotate once the log passes 50MB; keep one generation.
if [[ -f $LOG ]] && [[ $(stat -f%z "$LOG") -gt 52428800 ]]; then
  mv -f "$LOG" "$LOG.1"
fi

# Single instance. shlock writes a pid file and fails if the holder is alive.
if ! /usr/bin/shlock -f "$LOCK" -p $$; then
  log "previous collect still running (pid $(cat "$LOCK" 2>/dev/null)); skipping this run"
  exit 0
fi
trap 'rm -f "$LOCK"' EXIT INT TERM

log "starting collect --hours $HOURS"
/usr/bin/caffeinate -ims "$PROJECT/.venv/bin/polyarb" collect \
  --hours "$HOURS" \
  --config "$DATA/pm.yaml" \
  --db "$DATA/live.sqlite" >> "$LOG" 2>&1
rc=$?
log "collect exited rc=$rc"
exit $rc
